"""image_download 工序 ACT（详设-v0.5 §6.1）。

纯代码工序（reason: none，无 LLM 调用，零 token 成本）：
按 URL 域名路由 connector 获取图片。

路由规则：
- xiaohongshu.com -> xhs connector
- goofish.com -> xianyu connector
- 其他 http 图片 -> http_image connector

输入：ImageDownloadInput{url, batch_id, source?}
输出：ImagePack{paths[], count, desc, tags, author_id, day_dir, source, url}
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from engine.core.context import EngineContext
from models.workers import ImageDownloadInput, ImagePack

logger = logging.getLogger(__name__)


def _detect_source(url: str) -> str:
    """按域名自动识别来源。"""
    host = urlparse(url).hostname or ""
    if "xiaohongshu.com" in host or "xhslink.com" in host:
        return "xhs"
    if "goofish.com" in host or "2.taobao.com" in host:
        return "xianyu"
    return "crm"  # 默认通用图片


def run(inputs: ImageDownloadInput, ctx: EngineContext) -> ImagePack:
    """按 URL 域名路由 connector 获取图片。"""
    url = inputs.url
    batch_id = inputs.batch_id
    source = inputs.source or _detect_source(url)

    # 获取 connector
    connectors = ctx.connectors if ctx.connectors else {}
    connector = connectors.get(source)

    if connector is None:
        return ImagePack(
            paths=[],
            count=0,
            source=source,
            url=url,
            note=f"connector '{source}' 未注入（ctx.connectors 无此 key）",
        )

    # 检查 connector 可用性
    if not connector.available:
        return ImagePack(
            paths=[],
            count=0,
            source=source,
            url=url,
            note=f"connector '{source}' 不可用（vendor 未配置或依赖未安装）",
        )

    # 同步调用 connector 主方法（connector 内部处理 vendor/浏览器/http）
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    connector.download(url, batch_id=batch_id),
                )
                result = future.result(timeout=120)
        else:
            result = loop.run_until_complete(
                connector.download(url, batch_id=batch_id)
            )
    except Exception as e:
        return ImagePack(
            paths=[],
            count=0,
            source=source,
            url=url,
            note=f"connector '{source}' 调用异常: {e}",
        )

    if not result.ok:
        return ImagePack(
            paths=[],
            count=0,
            source=source,
            url=url,
            note=result.note,
        )

    data = result.data or {}
    return ImagePack(
        paths=data.get("paths", []),
        count=data.get("count", 0),
        desc=data.get("desc", ""),
        tags=data.get("tags", []),
        author_id=data.get("author_id", ""),
        day_dir=data.get("day_dir", ""),
        source=data.get("source", source),
        url=url,
        note=result.note,
    )
