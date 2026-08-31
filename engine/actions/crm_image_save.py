"""crm.image 消费者（详设-v0.5 §6.4）。

链完成由消费者 crm_image_save 经写接口 PATCH /api/biz/crm/message-images/{id}
落下载状态 + ocr_text（决策 26 接口化）。

消费者契约（R21 契约依赖）：async (proposal: dict, **注入) -> ConsumeOutcome；
audit_lookup / whitelist / biz_client / registry 全部由调用方注入（R12，
测试零网络零真服务）。
"""

from __future__ import annotations

import logging
from typing import Any

from .tm_proposal import ConsumeOutcome

logger = logging.getLogger(__name__)


async def consume_crm_image_save(
    data: dict,
    *,
    biz_client: Any = None,
    **kwargs: Any,
) -> ConsumeOutcome:
    """crm.image 消费者：经写接口 PATCH message-image 落下载状态 + ocr_text。

    data 结构：ImageSaveResult（updated[{message_image_id, ok, note}]）
    """
    if biz_client is None:
        logger.error("crm.image 消费者：biz_client 未注入")
        return ConsumeOutcome(ok=False, error="biz_client 未注入")

    updated = data.get("updated", [])
    if not updated:
        logger.info("crm.image 消费者：无更新项")
        return ConsumeOutcome(ok=True, note="无更新项")

    success_count = 0
    fail_count = 0
    for item in updated:
        message_image_id = item.get("message_image_id")
        ok = item.get("ok", False)
        note = item.get("note", "")

        if not message_image_id:
            continue

        if not ok:
            # 下载失败，标记 status=failed
            try:
                resp = await biz_client.post(
                    f"/crm/message-images/{message_image_id}",
                    {"status": "failed", "ocr_text": note},
                )
                if resp.status_code == 200:
                    success_count += 1
                else:
                    fail_count += 1
                    logger.warning(
                        "更新图片状态失败: id=%s, status=%s",
                        message_image_id,
                        resp.status_code,
                    )
            except Exception as e:
                fail_count += 1
                logger.exception("更新图片状态异常: id=%s", message_image_id)
        else:
            # 下载成功，ocr_text 已在 data 中，状态已更新
            success_count += 1

    if fail_count > 0:
        return ConsumeOutcome(
            ok=False,
            error=f"部分更新失败: {fail_count}/{success_count + fail_count}",
        )

    return ConsumeOutcome(
        ok=True,
        note=f"更新完成: {success_count}/{success_count + fail_count} 成功",
    )