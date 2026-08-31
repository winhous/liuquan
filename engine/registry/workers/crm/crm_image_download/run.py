"""crm_image_download 工序（详设-v0.5 §6.2）。

reason: none（纯代码，不调 LLM）
输入：ImageDownloadInputCRM（message_image_ids[]）
输出：ImageDownloadResult（downloaded[{message_image_id, local_path, ok, note}]）

逻辑：
1. 从 context_data 拿 crm.message_images provider 返回的图片记录（id/url/local_path/status）
2. 过滤出 status=pending 的记录
3. 对每条记录调用 http_image connector 下载到 storage_dir/crm/<message_id>/
4. 输出下载结果（ok/note/local_path）
"""

from __future__ import annotations

import logging
from pathlib import Path

from engine.core.context import EngineContext
from models.workers import ImageDownloadInputCRM, ImageDownloadResult

logger = logging.getLogger(__name__)


async def run(inputs: ImageDownloadInputCRM, ctx: EngineContext) -> ImageDownloadResult:
    """crm_image_download：下载 pending 状态的图片。"""
    # 从 context_data 拿 provider 返回的图片记录
    context_data = ctx.context_data.get("crm.message_images", {})
    if not context_data or "images" not in context_data:
        return ImageDownloadResult(note="无图片记录（provider 未返回数据）")

    images = context_data["images"]  # [{id, message_id, url, local_path, status}]
    if not images:
        return ImageDownloadResult(note="无图片记录")

    # 过滤出 pending 状态且在 input 中的记录
    pending_ids = set(inputs.message_image_ids)
    pending_images = [
        img for img in images
        if img["id"] in pending_ids and img.get("status") == "pending"
    ]

    if not pending_images:
        return ImageDownloadResult(note="无 pending 状态的图片需要下载")

    # 获取 http_image connector
    http_image_connector = ctx.connectors.get("http_image") if ctx.connectors else None
    if http_image_connector is None:
        # 降级：标记所有图片为 failed
        downloaded = [
            {
                "message_image_id": img["id"],
                "local_path": None,
                "ok": False,
                "note": "http_image connector 未配置",
            }
            for img in pending_images
        ]
        return ImageDownloadResult(downloaded=downloaded, note="http_image connector 未配置")

    # 获取 storage_dir（从 config 或默认值）
    storage_dir = ctx.config.get("storage_dir", "/opt/liuquan/scrape/")
    storage_path = Path(storage_dir)

    downloaded = []
    for img in pending_images:
        message_id = img["message_id"]
        url = img["url"]
        message_dir = storage_path / "crm" / str(message_id)
        message_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 调用 connector 下载
            result = await http_image_connector.download(
                url,
                batch_id=f"crm-{message_id}",
                dest_dir=str(message_dir),
            )
            if result.ok:
                # 相对路径
                local_path = str(Path(result.data.get("path", "")).relative_to(storage_path))
                downloaded.append({
                    "message_image_id": img["id"],
                    "local_path": local_path,
                    "ok": True,
                    "note": result.note or "下载成功",
                })
            else:
                downloaded.append({
                    "message_image_id": img["id"],
                    "local_path": None,
                    "ok": False,
                    "note": result.note or "下载失败",
                })
        except Exception as e:
            logger.exception("下载图片失败: %s", url)
            downloaded.append({
                "message_image_id": img["id"],
                "local_path": None,
                "ok": False,
                "note": f"下载异常: {e}",
            })

    return ImageDownloadResult(
        downloaded=downloaded,
        note=f"下载完成: {sum(1 for d in downloaded if d['ok'])}/{len(downloaded)} 成功",
    )