"""crm_image_scan 工序（详设-v0.5 §6.1）。

reason: none（纯代码，不调 LLM）
输入：ImageScanInput（message_id）
输出：ImageScanResult（urls[]）

逻辑：
1. 经 provider crm.message_images 拿消息的图片记录
2. 过滤出 status=pending 的记录
3. 返回 url 列表
"""

from __future__ import annotations

import logging

from engine.core.context import EngineContext
from models.workers import ImageScanInput, ImageScanResult

logger = logging.getLogger(__name__)


async def run(inputs: ImageScanInput, ctx: EngineContext) -> ImageScanResult:
    """crm_image_scan：提取消息中的图片链接。"""
    # 从 context_data 拿 provider 返回的图片记录
    context_data = ctx.context_data.get("crm.message_images", {})
    if not context_data or "images" not in context_data:
        return ImageScanResult(note="无图片记录（provider 未返回数据）")

    images = context_data["images"]  # [{id, message_id, url, local_path, status}]
    if not images:
        return ImageScanResult(note="无图片记录")

    # 过滤出 pending 状态的记录
    pending_images = [
        img for img in images
        if img.get("status") == "pending"
    ]

    if not pending_images:
        return ImageScanResult(note="无 pending 状态的图片")

    # 提取 url 列表
    urls = [img["url"] for img in pending_images if img.get("url")]

    return ImageScanResult(
        urls=urls,
        note=f"提取到 {len(urls)} 个图片链接",
    )