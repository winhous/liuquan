"""crm_image_save 工序（详设-v0.5 §6.2）。

reason: none（纯代码，不调 LLM）
输入：ImageSaveInput（captions[{message_image_id, text, note}]）
输出：ImageSaveResult（updated[{message_image_id, ok, note}]）

逻辑：
1. 从 captions 中提取每个图片的 message_image_id 和 text
2. 经写接口 PATCH /api/biz/crm/message-images/{id} 更新 ocr_text + status=downloaded
3. 输出更新结果
"""

from __future__ import annotations

import logging

from engine.core.context import EngineContext
from models.workers import ImageSaveInput, ImageSaveResult

logger = logging.getLogger(__name__)


async def run(inputs: ImageSaveInput, ctx: EngineContext) -> ImageSaveResult:
    """crm_image_save：保存识图结果。"""
    # 获取 biz_client（engine/actions/biz_client.py 的 BizApiClient）
    biz_client = getattr(ctx, "_biz_client", None)
    if biz_client is None:
        return ImageSaveResult(
            note="biz_client 未注入，无法保存识图结果",
            updated=[{"message_image_id": c.message_image_id, "ok": False, "note": "biz_client 未注入"}
                     for c in inputs.captions],
        )

    updated = []
    for caption in inputs.captions:
        try:
            # 经写接口 PATCH 更新 ocr_text + status=downloaded
            payload = {
                "ocr_text": caption.text,
                "status": "downloaded" if caption.text else "skipped",
            }
            resp = await biz_client.post(
                f"/crm/message-images/{caption.message_image_id}",
                payload,
            )
            if resp.status_code == 200:
                updated.append({
                    "message_image_id": caption.message_image_id,
                    "ok": True,
                    "note": caption.note or "保存成功",
                })
            else:
                updated.append({
                    "message_image_id": caption.message_image_id,
                    "ok": False,
                    "note": f"接口返回 {resp.status_code}",
                })
        except Exception as e:
            logger.exception("保存识图结果失败: message_image_id=%s", caption.message_image_id)
            updated.append({
                "message_image_id": caption.message_image_id,
                "ok": False,
                "note": f"保存异常: {e}",
            })

    return ImageSaveResult(
        updated=updated,
        note=f"保存完成: {sum(1 for u in updated if u['ok'])}/{len(updated)} 成功",
    )