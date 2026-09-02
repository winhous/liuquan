"""engine/connectors/http_image.py：普通图片 URL 下载（详设-v0.5 §5.5）。

新增连接器，CRM 对话图片 / 通用 HTTP 图片用。
照广成 image-download 的 http 下载模式。

连接器特点：
- 普通 http(s) 图片 URL 下载（httpx + UA 伪装）
- 落盘 {storage_dir}/crm/<message_id>/ 或 {storage_dir}/crm/<batch_id>/
- 非图片/下载失败 → 降级 ok=False + note
- httpx 未安装时 fallback 到 urllib（标准库）
"""

from __future__ import annotations

import os
import re
import urllib.request
from pathlib import Path
from typing import Any

from engine.connectors import ConnectorResult, register_connector

__all__ = ["HTTPImageConnector"]

_CONNECTOR_ID = "http_image"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class HTTPImageConnector:
    """普通图片 URL 下载连接器（CRM 对话图片 / 通用 HTTP 图片）。

    real 调用：httpx（优先）或 urllib（fallback）+ UA 伪装
    测试：fake connector 注入（零网络），httpx/urllib 都是标准/常见依赖
    """

    def __init__(self, storage_dir: str | None = None) -> None:
        self._storage_dir = storage_dir or os.environ.get(
            "SCRAPE_STORAGE_DIR", "/opt/liuquan/scrape/"
        ).strip()

    @property
    def available(self) -> bool:
        """httpx 或 urllib 总有一个可用。"""
        return True

    async def download(
        self,
        url: str,
        *,
        message_id: int | None = None,
        batch_id: str | None = None,
        storage_dir: str | None = None,
    ) -> ConnectorResult:
        """下载普通图片 URL。"""
        if not self.available:
            return ConnectorResult(
                ok=False,
                note="HTTP 图片下载不可用",
            )

        target_dir = storage_dir or self._storage_dir
        sub_dir = str(message_id) if message_id else (batch_id or "unknown")
        save_dir = Path(target_dir) / "crm" / sub_dir
        save_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 尝试 httpx
            try:
                import httpx
                headers = {"User-Agent": _USER_AGENT}
                resp = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
                content_type = resp.headers.get("content-type", "")
                body = resp.content
            except ImportError:
                # fallback 到 urllib
                req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    content_type = resp.headers.get("Content-Type", "")
                    body = resp.read()

            # 检查是否是图片
            if not any(t in content_type for t in ("image/", "octet-stream")):
                return ConnectorResult(
                    ok=False,
                    note=f"URL 返回非图片内容（Content-Type: {content_type}）: {url}",
                    data={"url": url},
                )

            if len(body) < 1000:  # <1KB 大概率不是有效图片
                return ConnectorResult(
                    ok=False,
                    note=f"下载内容过小（{len(body)} bytes），可能不是有效图片: {url}",
                    data={"url": url},
                )

            # 猜扩展名
            ext = self._guess_ext(content_type)
            file_path = save_dir / f"1{ext}"
            file_path.write_bytes(body)

            return ConnectorResult(
                ok=True,
                note="HTTP 图片下载完成",
                data={
                    "paths": [str(file_path)],
                    "count": 1,
                    "desc": "",
                    "tags": [],
                    "author_id": "",
                    "day_dir": "",
                    "source": "crm",
                    "url": url,
                },
            )

        except Exception as e:
            return ConnectorResult(
                ok=False,
                note=f"HTTP 图片下载异常: {e}",
                data={"url": url},
            )

    def _guess_ext(self, content_type: str) -> str:
        """根据 Content-Type 猜扩展名。"""
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
        if "gif" in content_type:
            return ".gif"
        return ".jpg"


def _factory(ctx: Any, *, storage_dir: str | None = None) -> HTTPImageConnector:
    """工厂函数：注册到 CONNECTORS 注册表（v0.6 §5.5：storage_dir 可注入落盘根）。"""
    return HTTPImageConnector(storage_dir=storage_dir)


# 模块加载时注册
register_connector(_CONNECTOR_ID, _factory)
