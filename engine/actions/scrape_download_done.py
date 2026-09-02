"""scrape_download_done 消费者（详设-v0.6 §5.3，链完成回调）。

scrape_download_chain 完成 → 消费者 scrape.download_done：
deliverable = ScrapeBatchResult（链末输出） + kwargs 注入 task（含 task.input）：
- task.input.from_queue=true → POST /api/biz/scrape/queue/clear（清定时队列，
  详设 §5.4：定时跑全部链接建记录+处理成功完成后清空队列）
- from_queue=false → 只记审计 note（立即扒不清队列）

职责：
1. 契约归一（dict -> ScrapeBatchResult，R2）；
2. from_queue 判断（优先 kwargs.task.input.from_queue——调度器注入；
   兜底 deliverable.from_queue）；
3. 队列清空经 biz_client 写接口（决策 26：引擎零业务库连接串）；
4. 不 import web 任何代码（P3-2）。

注入式设计（R12）：biz_client / task 全部可注入，测试零网络。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from engine.actions.biz_client import BizApiClient
from engine.actions.tm_proposal import ConsumeOutcome
from models.workers import ScrapeBatchResult

logger = logging.getLogger(__name__)


async def consume_scrape_download_done(
    deliverable: dict,
    *,
    task: Any | None = None,
    biz_client: BizApiClient | None = None,
    **_kwargs,
) -> ConsumeOutcome:
    """下载链完成消费者：from_queue=true 清定时队列，其余记审计 note。"""
    try:
        result = ScrapeBatchResult.model_validate(deliverable)
    except ValidationError as exc:
        return ConsumeOutcome(
            "rejected", reason=f"下载结果未过 ScrapeBatchResult 契约校验：{exc}"
        )

    # from_queue 优先取 task.input（调度器注入；deliverable.from_queue 兜底透传）
    from_queue = bool(result.from_queue)
    if task is not None:
        task_input = getattr(task, "input", None) or {}
        if isinstance(task_input, dict):
            from_queue = bool(task_input.get("from_queue", from_queue))

    if not from_queue:
        return ConsumeOutcome(
            "accepted",
            reason=(
                f"立即扒下载链完成（batch_id={result.batch_id}，"
                f"链接 {len(result.links)} 条，图片 {len(result.image_ids)} 张），不清队列"
            ),
        )

    if biz_client is None:
        return ConsumeOutcome(
            "accepted",
            reason="定时扒链完成但 biz_client 未注入（无法清定时队列，下次定时跑重试）",
        )

    try:
        resp = await biz_client.post("/scrape/queue/clear", {})
    except Exception as exc:  # noqa: BLE001  # BizApiError 等网络/配置异常
        return ConsumeOutcome(
            "accepted",
            reason=f"定时扒链完成但清队列失败（{exc}，下次定时跑重试）",
        )
    if resp.status_code != 200:
        return ConsumeOutcome(
            "accepted",
            reason=f"定时扒链完成但清队列 HTTP {resp.status_code}（下次定时跑重试）",
        )
    return ConsumeOutcome(
        "accepted",
        reason=(
            f"定时扒链完成（batch_id={result.batch_id}，链接 {len(result.links)} 条，"
            f"图片 {len(result.image_ids)} 张），定时队列已清空"
        ),
    )
