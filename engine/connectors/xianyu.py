"""engine/connectors/xianyu.py：闲鱼扒图（详设-v0.5 §5.4）。

照广成 image-download：
- playwright 无头 + UA 伪装 + 滚轮懒加载
- 主图/详情/全页三路选择器兜底
- page.request.get 下载（≥15KB 过滤）
- 落盘 {storage_dir}/xianyu/<商品id>/01.jpg…
- 标题/卖家 ID 尽力（闲鱼无标签，tags 恒空）
- 失效页删垃圾图；90s 全局截止

测试：fake connector 注入（零网络），playwright 未安装时降级 ok=False
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from engine.connectors import ConnectorResult, register_connector

__all__ = ["XianyuConnector"]

_CONNECTOR_ID = "xianyu"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 主图/详情/全页三路选择器（照广成）
_MAIN_IMG_SELECTORS = [
    "img.main-image",
    ".item-img img",
    ".item-main img",
]
_DETAIL_IMG_SELECTORS = [
    ".item-detail img",
    ".detail-image img",
    ".desc-image img",
]
_ALL_IMG_SELECTORS = _MAIN_IMG_SELECTORS + _DETAIL_IMG_SELECTORS + [
    "img[src*='img.alicdn.com']",
    "img[src*='goofish']",
]


class XianyuConnector:
    """闲鱼扒图连接器（照广成 playwright 方案）。

    real 调用：playwright 无头浏览器 + 三路选择器兜底 + ≥15KB 过滤
    测试：fake connector 注入（零网络），playwright 未安装时降级 ok=False
    """

    def __init__(self, storage_dir: str | None = None) -> None:
        self._storage_dir = storage_dir or os.environ.get(
            "SCRAPE_STORAGE_DIR", "/opt/liuquan/scrape/"
        ).strip()

    @property
    def available(self) -> bool:
        """检查连接器是否可用（playwright 已安装）。"""
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
            return True
        except ImportError:
            return False

    async def download(
        self,
        url: str,
        *,
        batch_id: str,
        storage_dir: str | None = None,
    ) -> ConnectorResult:
        """下载闲鱼商品图片（照广成 playwright 方案）。"""
        if not self.available:
            return ConnectorResult(
                ok=False,
                note="闲鱼扒图 playwright 未配置（chromium 未安装）",
            )

        target_dir = storage_dir or self._storage_dir
        item_id = self._extract_item_id(url)
        if not item_id:
            return ConnectorResult(
                ok=False,
                note=f"无法从 URL 提取闲鱼商品 ID: {url}",
                data={"url": url, "batch_id": batch_id},
            )

        item_dir = Path(target_dir) / "xianyu" / item_id
        item_dir.mkdir(parents=True, exist_ok=True)

        try:
            from playwright.sync_api import sync_playwright

            title = ""
            seller_id = ""
            paths = []

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=_USER_AGENT)
                page = context.new_page()
                page.set_default_timeout(90_000)  # 90s 截止

                try:
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)

                    # 滚轮懒加载
                    for _ in range(5):
                        page.mouse.wheel(0, 800)
                        page.wait_for_timeout(500)

                    # 尝试提取标题
                    try:
                        title_el = page.query_selector("h1, .item-title, .title")
                        if title_el:
                            title = title_el.inner_text().strip()[:200]
                    except Exception:
                        pass

                    # 尝试提取卖家 ID
                    try:
                        seller_el = page.query_selector(".seller-name, .user-name")
                        if seller_el:
                            seller_id = seller_el.inner_text().strip()[:50]
                    except Exception:
                        pass

                    # 三路选择器兜底收集图片 URL
                    img_urls = []
                    for selector in _ALL_IMG_SELECTORS:
                        try:
                            elements = page.query_selector_all(selector)
                            for el in elements:
                                src = el.get_attribute("src") or ""
                                if src and src.startswith("http"):
                                    img_urls.append(src)
                        except Exception:
                            continue

                    # 去重
                    img_urls = list(dict.fromkeys(img_urls))

                    # 下载（≥15KB 过滤）
                    for i, img_url in enumerate(img_urls, 1):
                        try:
                            resp = page.request.get(img_url)
                            if resp.status == 200:
                                body = resp.body()
                                if len(body) >= 15_000:  # ≥15KB
                                    ext = self._guess_ext(resp.headers.get("content-type", ""))
                                    file_path = item_dir / f"{i:02d}{ext}"
                                    file_path.write_bytes(body)
                                    paths.append(str(file_path))
                        except Exception:
                            continue

                finally:
                    browser.close()

            if not paths:
                # 失效页删垃圾图
                self._cleanup_empty_dir(item_dir)
                return ConnectorResult(
                    ok=False,
                    note=f"闲鱼页面未找到有效图片（可能已失效）: {url}",
                    data={"url": url, "batch_id": batch_id, "item_id": item_id},
                )

            return ConnectorResult(
                ok=True,
                note="闲鱼下载完成",
                data={
                    "paths": paths,
                    "count": len(paths),
                    "desc": title,
                    "tags": [],  # 闲鱼无标签
                    "author_id": seller_id,
                    "day_dir": "",
                    "source": "xianyu",
                    "url": url,
                },
            )

        except Exception as e:
            return ConnectorResult(
                ok=False,
                note=f"闲鱼下载异常: {e}",
                data={"url": url, "batch_id": batch_id, "item_id": item_id},
            )

    def _extract_item_id(self, url: str) -> str | None:
        """从 URL 提取闲鱼商品 ID。"""
        match = re.search(r"id=(\d+)", url)
        if match:
            return match.group(1)
        match = re.search(r"item/(\d+)", url)
        if match:
            return match.group(1)
        return None

    def _guess_ext(self, content_type: str) -> str:
        """根据 Content-Type 猜扩展名。"""
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
        if "gif" in content_type:
            return ".gif"
        return ".jpg"

    def _cleanup_empty_dir(self, item_dir: Path) -> None:
        """清理空目录（失效页删垃圾图）。"""
        try:
            if item_dir.exists() and not any(item_dir.iterdir()):
                item_dir.rmdir()
        except Exception:
            pass


def _factory(ctx: Any) -> XianyuConnector:
    """工厂函数：注册到 CONNECTORS 注册表。"""
    return XianyuConnector()


# 模块加载时注册
register_connector(_CONNECTOR_ID, _factory)
