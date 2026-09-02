"""engine/connectors/xianyu.py：闲鱼扒图（详设-v0.6 §6.2，照广成 runtime/xianyu_connector.py 原样搬回）。

playwright 无头浏览器扒 goofish.com 公开商品页图片（不登录；反爬靠无头 + 限速）。
依赖：playwright + chromium（pip install playwright && python -m playwright install chromium）。

- 主图轮播 10 套选择器（照广成 _MAIN_IMG_SELECTORS 原文，命中即停）+ 详情 6 套 +
  全页兜底 _harvest_all_imgs（JS 一次性收集 naturalWidth/Height ≥ 100 的大图，
  主图+详情 <3 张时触发）
- URL 过滤 10 个关键词（avatar/icon/logo/sprite/placeholder/loading/blank/default/
  qrcode/qr-code）+ data: 排除；文件大小过滤 _MIN_IMG_BYTES=15_000（推荐位小图 <15KB 丢弃）；
  单图下载 20s 超时（page.request.get），失败跳过
- 失效页检测 _DEAD_PAGE_KEYWORDS（宝贝被删掉/已下架/已删除/宝贝不存在/宝贝已不存在）
  扫 body innerText → 删已下载垃圾图 + dead-page 标记 + desc=「商品已失效/删除」
- 全局 90s deadline：`deadline = time.monotonic() + GLOBAL_TIMEOUT` 下载循环内逐张检查
  （非 page.set_default_timeout）
- networkidle 优先（React 渲染完）失败降级 domcontentloaded；3s 等待 + 懒加载 4×wheel(1500)+回顶
- 标题三路：page.title() 去「_闲鱼/-闲鱼/ 闲鱼/_闲鱼 - 闲不住？上闲鱼！」后缀 →
  desc 容器（[class*="itemDesc"]/[class*="desc"] 首行，排除「为你推荐」）→ h1
- 卖家两路：window.__INITIAL_STATE__/__PRELOADED_STATE__ 递归找 sellerId/userId/
  seller_id/user_id/shopId/ownerId → DOM a[href*="userId"] 等
- UA 伪装（Chrome/120）+ viewport 1280×1800；playwright 未装 → ok=False + 安装指引
- 降级标注：ok=True 时 note 记降级原因（no-images-found / download-failed / no-title /
  no-seller-id / dead-page），图已落盘不整体失败
- 闲鱼无标签（tags 恒 []）；落盘 {storage_dir}/xianyu/<商品id>/01.jpg 02.jpg…

测试：fake page 对象（只住 tests/，R12 桩只住 tests/），零网络。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

# 顶部 try import playwright：未装时 download() 返回结构化错误，不崩
try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PWTimeoutError

    _PW_AVAILABLE = True
except ImportError:  # pragma: no cover - 环境未装时走此分支
    _PW_AVAILABLE = False
    sync_playwright = None  # type: ignore[assignment]
    PWTimeoutError = Exception  # type: ignore[misc,assignment]

from engine.connectors import ConnectorResult, register_connector

__all__ = ["XianyuConnector"]

_CONNECTOR_ID = "xianyu"

# ── 常量（照广成原文） ─────────────────────────────

# 闲鱼商品链接（goofish.com 多域名形态：www / h5 / m；路径 item 或 item.htm）
_XIANYU_URL_RE = re.compile(
    r"https?://(?:www\.|h5\.|m\.)?goofish\.com/item(?:\.htm)?(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
# 商品 ID（goofish.com/item?id=xxx 或 item.htm?id=xxx，id 可能不是首参）
_XIANYU_ITEM_ID_RE = re.compile(
    r"goofish\.com/item(?:\.htm)?\?(?:[^&]*&)*?id=([A-Za-z0-9]+)",
    re.IGNORECASE,
)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# 超时
PAGE_LOAD_TIMEOUT = 30_000  # 页面加载 30s（毫秒，playwright 用 ms）
GLOBAL_TIMEOUT = 90  # 整体扒一个商品 90s 上限（秒）
IMG_DOWNLOAD_TIMEOUT = 20_000  # 单图下载 20s（毫秒）

# 主图轮播选择器（多套兜底；闲鱼页面结构可能变，实跑时按需调整）
_MAIN_IMG_SELECTORS = [
    '[class*="swiper-slide"] img',
    '[class*="carousel"] img',
    '[class*="gallery"] img',
    '[class*="mainImg"] img',
    '[class*="main-image"] img',
    '[class*="imageMain"] img',
    '[class*="picMain"] img',
    '[class*="imageBox"] img',
    '[class*="slider"] img',
    '[class*="slide"] img',
]

# 详情图选择器（多套兜底）
_DETAIL_IMG_SELECTORS = [
    '[class*="itemDesc"] img',
    '[class*="desc"] img',
    '[class*="detail"] img',
    '[class*="content"] img',
    '[class*="ImageText"] img',
    '[class*="imageText"] img',
]

# URL 过滤关键词（含这些的图跳过：小图标/logo/avatar/占位）
_FILTER_KEYWORDS = (
    "avatar",
    "icon",
    "logo",
    "sprite",
    "placeholder",
    "loading",
    "blank",
    "default",
    "qrcode",
    "qr-code",
)

# 最小图片尺寸（naturalWidth/Height 小于此值的跳过：小图标）
_MIN_IMG_SIZE = 100

# 下载后按文件大小过滤：小于此值（字节）的丢，商品主图至少几十 KB，
# 推荐位缩略图/小图标通常 < 15KB（实测 goofish 推荐位 webp 5-6KB）
_MIN_IMG_BYTES = 15_000

# 商品失效/删除页文案（命中即判失败，不落盘垃圾图）
_DEAD_PAGE_KEYWORDS = ("宝贝被删掉", "已下架", "已删除", "宝贝不存在", "宝贝已不存在")


class XianyuConnector:
    """闲鱼扒图连接器（照广成 xianyu_connector.py 原样搬回，H3）。

    real 调用：playwright 无头 + 多路选择器 + 全页兜底 + 失效页删图 + 90s 全局截止。
    测试：fake page 对象注入（零网络），playwright 未安装时降级 ok=False。
    """

    def __init__(self, storage_dir: str | None = None) -> None:
        self._storage_dir = storage_dir or os.environ.get(
            "SCRAPE_STORAGE_DIR", "/opt/liuquan/scrape/"
        ).strip()

    @property
    def available(self) -> bool:
        """playwright 装好即认为可尝试；真实失败在 download() 时以 note 反馈。"""
        return _PW_AVAILABLE

    async def download(
        self,
        url: str,
        *,
        batch_id: str,
        storage_dir: str | None = None,
    ) -> ConnectorResult:
        """下载闲鱼商品图片（照广成 playwright 方案 + 90s 全局截止 + 失效页删图）。"""
        url = (url or "").strip()
        if not _XIANYU_URL_RE.search(url):
            return ConnectorResult(
                ok=False,
                note=f"xianyu-download 未解析到闲鱼链接（需 goofish.com/item?id=xxx 商品链接）: {url}",
                data={"url": url, "batch_id": batch_id},
            )

        if not _PW_AVAILABLE:
            return ConnectorResult(
                ok=False,
                note="playwright 未安装，先 pip install playwright && python -m playwright install chromium",
                data={"url": url, "batch_id": batch_id},
            )

        item_id = _extract_item_id(url)
        if not item_id:
            return ConnectorResult(
                ok=False,
                note=f"闲鱼链接未提取到商品 id: {url}",
                data={"url": url, "batch_id": batch_id},
            )

        # 落盘根目录：storage_dir/xianyu/<商品id>/，文件按序号 01.jpg 02.jpg ...
        base_dir = Path(storage_dir or self._storage_dir) / "xianyu"
        out_dir = base_dir / item_id
        out_dir.mkdir(parents=True, exist_ok=True)

        # 整体 90s 上限（time.monotonic 逐张检查，非 set_default_timeout）
        deadline = time.monotonic() + GLOBAL_TIMEOUT
        try:
            paths, desc, author_id, note = self._scrape(url, out_dir, deadline)
        except PWTimeoutError:
            return ConnectorResult(
                ok=False,
                note=f"闲鱼扒图超时（页面加载或整体超过上限）: {url}",
                data={"url": url, "batch_id": batch_id, "item_id": item_id},
            )
        except Exception as exc:  # noqa: BLE001 - 任何意外都降级为结构化失败，不崩
            return ConnectorResult(
                ok=False,
                note=f"闲鱼扒图异常: {type(exc).__name__}: {exc}",
                data={"url": url, "batch_id": batch_id, "item_id": item_id},
            )

        if not paths:
            reason = "闲鱼扒图未抓到任何图片（页面可能需登录、或选择器失效，请人工核对 goofish 页面）。"
            if note and "dead-page" in note:
                reason = f"闲鱼商品已失效/删除（{desc}），无图可扒。"
            elif note and "no-images-found" in note:
                reason = "闲鱼扒图未抓到任何图片（页面可能需登录、或选择器失效，请人工核对 goofish 页面）。"
            return ConnectorResult(
                ok=False,
                note=reason,
                data={
                    "url": url,
                    "batch_id": batch_id,
                    "item_id": item_id,
                    "scrape_note": note,
                },
            )

        return ConnectorResult(
            ok=True,
            note=note,
            data={
                "paths": paths,
                "count": len(paths),
                "desc": desc,
                "tags": [],  # 闲鱼无标签概念
                "author_id": author_id,
                "day_dir": str(base_dir),
                "source": "xianyu",
                "url": url,
                "batch_id": batch_id,
            },
        )

    # ── 内部：扒图 + 元数据 ──────────────────────────────

    def _scrape(self, url: str, out_dir: Path, deadline: float) -> tuple[list[str], str, str, str]:
        """启动 playwright 扒图 + 读元数据。返回 (paths, desc, author_id, note)。

        note 非空表示元数据降级原因（no-images / download-failed / no-title /
        no-seller-id / dead-page），图已落盘则不让整体失败。
        """
        paths: list[str] = []
        desc = ""
        author_id = ""
        notes: list[str] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 1800},
            )
            try:
                # networkidle 等 React 渲染完成（domcontentloaded 太早，主图/标题还没渲染）
                # 个别页面 networkidle 久不触发，catch 后降级
                try:
                    page.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
                except PWTimeoutError:
                    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                # 等动态加载（反爬：headless + sleep，闲鱼 React 渲染需时间）
                page.wait_for_timeout(3000)

                # 滚动触发懒加载
                self._scroll_page(page)

                # 抓图
                img_urls = self._collect_image_urls(page)
                if not img_urls:
                    notes.append("no-images-found")
                else:
                    paths = self._download_images(page, img_urls, out_dir, deadline)
                    if not paths:
                        notes.append("download-failed")

                # 元数据（失败不阻断扒图，note 记一笔）
                desc = self._extract_title(page) or ""
                if not desc:
                    notes.append("no-title")
                author_id = self._extract_seller_id(page) or ""
                if not author_id:
                    notes.append("no-seller-id")

                # 商品失效/删除页检测：扫页面全文（不依赖 desc，失效文案可能在任意容器）
                # 命中 -> 丢弃已下载的垃圾图，标记 dead-page
                page_text = ""
                try:
                    page_text = page.evaluate("() => document.body.innerText || ''") or ""
                except Exception:  # noqa: BLE001
                    pass
                if any(kw in page_text for kw in _DEAD_PAGE_KEYWORDS):
                    for p in paths:
                        try:
                            Path(p).unlink(missing_ok=True)
                        except Exception:  # noqa: BLE001
                            pass
                    paths = []
                    notes.append("dead-page")
                    # 失效页 desc 标记一下
                    desc = "商品已失效/删除"
            finally:
                browser.close()

        return paths, desc, author_id, ";".join(notes)

    def _scroll_page(self, page) -> None:
        """滚动页面触发懒加载图片（分段滚 + 回顶）。失败忽略。"""
        try:
            for _ in range(4):
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(500)
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:  # noqa: BLE001 - 滚动失败不影响后续
            pass

    def _collect_image_urls(self, page) -> list[str]:
        """收集图片 URL：先主图轮播区、再详情区、最后全页兜底；过滤小图标/logo/avatar；去重保序。"""
        collected: list[str] = []

        # 1. 主图轮播（命中即停，避免重复抓）
        for sel in _MAIN_IMG_SELECTORS:
            got = self._imgs_from_selector(page, sel)
            if got:
                collected.extend(got)
                if len(collected) >= 20:
                    break

        # 2. 详情图
        for sel in _DETAIL_IMG_SELECTORS:
            got = self._imgs_from_selector(page, sel)
            if got:
                collected.extend(got)
                if len(collected) >= 50:
                    break

        # 3. 兜底：主图+详情都没抓够，全页 JS 收集所有较大 img
        if len(collected) < 3:
            collected.extend(self._harvest_all_imgs(page))

        # 过滤 + 去重（保序）
        seen: set[str] = set()
        result: list[str] = []
        for u in collected:
            nu = self._normalize_url(u)
            if not nu or nu in seen:
                continue
            if not self._is_valid_image_url(nu):
                continue
            seen.add(nu)
            result.append(nu)
        return result

    def _imgs_from_selector(self, page, selector: str) -> list[str]:
        """从匹配选择器的 img 元素取 src/data-src/data-original（处理懒加载）。"""
        urls: list[str] = []
        try:
            loc = page.locator(selector)
            count = loc.count()
        except Exception:  # noqa: BLE001
            return urls
        for i in range(min(count, 30)):
            try:
                el = loc.nth(i)
                src = (
                    el.get_attribute("src")
                    or el.get_attribute("data-src")
                    or el.get_attribute("data-original")
                    or ""
                )
                if src:
                    urls.append(src)
            except Exception:  # noqa: BLE001 - 单元素失败跳过
                continue
        return urls

    def _harvest_all_imgs(self, page) -> list[str]:
        """全页 JS 一次性收集 img：返回 naturalWidth/Height >= _MIN_IMG_SIZE 的大图 src 列表。"""
        try:
            data = page.evaluate(
                """() => Array.from(document.querySelectorAll('img')).map(img => ({
                    src: img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '',
                    w: img.naturalWidth || 0,
                    h: img.naturalHeight || 0,
                })).filter(i => i.src && i.w >= %d && i.h >= %d).map(i => i.src)"""
                % (_MIN_IMG_SIZE, _MIN_IMG_SIZE)
            )
        except Exception:  # noqa: BLE001
            return []
        return list(data) if data else []

    @staticmethod
    def _normalize_url(url: str) -> str:
        """规整图片 URL：协议相对（//img...）补 https:。"""
        if url.startswith("//"):
            return "https:" + url
        return url

    @staticmethod
    def _is_valid_image_url(url: str) -> bool:
        """过滤小图标/logo/avatar/占位图（按 URL 关键词；尺寸过滤在 harvest 阶段已做）。"""
        low = url.lower()
        if low.startswith("data:"):
            return False
        if any(kw in low for kw in _FILTER_KEYWORDS):
            return False
        return True

    def _download_images(self, page, img_urls: list[str], out_dir: Path, deadline: float) -> list[str]:
        """用 page.request.get 下载图片到 out_dir/01.jpg 02.jpg ... 返回落盘路径列表。

        单张失败跳过不阻断；超 deadline 停止（全局 90s 上限，逐张检查）。
        """
        paths: list[str] = []
        for idx, img_url in enumerate(img_urls, start=1):
            if time.monotonic() > deadline:
                break
            ext = _guess_ext(img_url)
            fpath = out_dir / f"{idx:02d}{ext}"
            try:
                resp = page.request.get(img_url, timeout=IMG_DOWNLOAD_TIMEOUT)
                if resp.ok:
                    body = resp.body()
                    # 按文件大小过滤：错误页小图标/占位图通常 < 5KB，商品图至少几十 KB
                    if body and len(body) >= _MIN_IMG_BYTES:
                        fpath.write_bytes(body)
                        paths.append(str(fpath))
            except Exception:  # noqa: BLE001 - 单图失败跳过，继续下一张
                continue
        return paths

    def _extract_title(self, page) -> str:
        """商品标题：闲鱼 page.title() 形如「灯工玻璃花_闲鱼」，优先取它并去后缀。
        回退：desc 类容器（实测命中真实描述）→ h1。
        """
        # 1. page.title()：闲鱼商品页 title = "<商品标题>_闲鱼" 或 "...-闲鱼"
        try:
            t = page.title().strip()
            if t:
                # 去掉闲鱼后缀
                for suf in ("_闲鱼", "-闲鱼", " 闲鱼", "_闲鱼 - 闲不住？上闲鱼！"):
                    if t.endswith(suf):
                        t = t[: -len(suf)].strip()
                        break
                # 兜底：title 含"闲鱼"但不是后缀，取"闲鱼"前部分
                if "闲鱼" in t and not t.endswith("闲鱼"):
                    t = t.split("闲鱼")[0].strip(" -_")
                if t and "闲鱼" not in t:
                    return t
        except Exception:  # noqa: BLE001
            pass
        # 2. desc 类容器（实测命中真实描述，如"灯工玻璃花\n颜色可选..."）
        for sel in ('[class*="itemDesc"]', '[class*="desc"]'):
            try:
                loc = page.locator(sel).first
                txt = loc.inner_text(timeout=2000).strip()
                # 取第一行（描述可能多行）
                first_line = txt.split("\n")[0].strip() if txt else ""
                if first_line and first_line != "为你推荐":
                    return first_line
            except Exception:  # noqa: BLE001
                continue
        # 3. h1 兜底
        try:
            loc = page.locator("h1").first
            txt = loc.inner_text(timeout=2000).strip()
            if txt:
                return txt
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _extract_seller_id(self, page) -> str:
        """卖家ID（尽力）：先从 window 全局状态找 sellerId/userId，再 DOM seller 链接。抓不到给空串。"""
        # 1. 页面 JS 全局状态（闲鱼常把初始状态挂 window）
        try:
            sid = page.evaluate(
                """() => {
                    const tryGet = (o) => {
                        if (!o || typeof o !== 'object') return '';
                        for (const k of ['sellerId','userId','seller_id','user_id','shopId','ownerId']) {
                            if (o[k]) return String(o[k]);
                        }
                        return '';
                    };
                    const s = window.__INITIAL_STATE__ || window.__PRELOADED_STATE__ || {};
                    return tryGet(s) || tryGet(s.data) || tryGet(s.item) || tryGet(s.detail) || tryGet(s.seller) || '';
                }"""
            )
            if sid:
                return str(sid)
        except Exception:  # noqa: BLE001
            pass
        # 2. DOM 里 seller 链接 / 属性
        for sel in ('a[href*="userId"]', 'a[href*="sellerId"]', "[data-seller-id]", "[data-userid]"):
            try:
                loc = page.locator(sel).first
                href = (
                    loc.get_attribute("href")
                    or loc.get_attribute("data-seller-id")
                    or loc.get_attribute("data-userid")
                    or ""
                )
                m = re.search(r"(?:userId|sellerId|userid|sellerid)=([A-Za-z0-9]+)", href, re.IGNORECASE)
                if m:
                    return m.group(1)
            except Exception:  # noqa: BLE001
                continue
        return ""


# ── 模块级辅助（照广成同风格，供 download() 复用） ────────


def _extract_xianyu_url(task: str) -> str:
    """从任务文本提取闲鱼商品链接（保留 query 含 id）。"""
    if not task:
        return ""
    m = _XIANYU_URL_RE.search(task)
    return m.group(0) if m else ""


def _extract_item_id(url: str) -> str:
    """从闲鱼 URL 提取商品 ID（item?id=xxx 的 id 参数）。"""
    m = _XIANYU_ITEM_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _guess_ext(url: str) -> str:
    """从 URL 猜图片扩展名，未知用 .jpg。"""
    low = url.lower().split("?")[0]
    for ext in IMAGE_EXTS:
        if low.endswith(ext):
            return ext
    return ".jpg"


def _factory(ctx: Any, *, storage_dir: str | None = None) -> XianyuConnector:
    """工厂函数：注册到 CONNECTORS 注册表（v0.6 §5.5：storage_dir 可注入落盘根）。"""
    return XianyuConnector(storage_dir=storage_dir)


# 模块加载时注册
register_connector(_CONNECTOR_ID, _factory)
