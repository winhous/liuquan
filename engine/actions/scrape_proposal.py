"""scrape_proposal 消费者（详设-v0.5 §6.4）。

scrape_suggest_chain 完成 → 消费者 scrape.suggest（action_id=scrape.suggest）：
SuggestionResult → TaskProposal(domain=scrape) → POST /api/biz/tm/proposals。

职责：
1. 契约归一（dict -> SuggestionResult，R2）；
2. 遍历 proposals 构造 TaskProposal；
3. 复用 tm_proposal 转交器核心逻辑（禁幻觉三件套 + risk 标注 + HTTP 写接口）；
4. 不 import web 任何代码（P3-2）。

注入式设计（R12）：registry / audit_lookup / whitelist / biz_client 全部可注入。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection

from pydantic import ValidationError

from engine.actions.biz_client import BizApiClient
from engine.actions.tm_proposal import ConsumeOutcome, consume_task_proposal
from models.contract.task import TaskProposal
from models.workers import SuggestionResult

logger = logging.getLogger(__name__)


async def consume_scrape_proposal(
    deliverable: dict,
    *,
    registry: object | None = None,
    audit_lookup: Callable[[list[str]], bool] | None = None,
    whitelist: Collection[str] | None = None,
    biz_client: BizApiClient | None = None,
    source: dict | None = None,
    **_kwargs,
) -> ConsumeOutcome:
    """扒图选品提案消费者：SuggestionResult → TaskProposal → tm 转交器落库。

    source（SourceTrace：chain_id/engine_task_id/worker_id/audit_ids）由调用方
    （engine/server.py 转交钩子）注入——LLM 输出的 proposals 不含 source 追溯，
    禁幻觉三件套（audit_ids 可查 + ref_id 白名单）依赖它（v0.6 批 4 修通：
    suggest 链此前未接转交分发，断点 6）。
    """
    # ---- 0. 契约归一 ----
    try:
        result = SuggestionResult.model_validate(deliverable)
    except ValidationError as exc:
        return ConsumeOutcome(
            "rejected", reason=f"选品结果未过 SuggestionResult 契约校验：{exc}"
        )

    if not result.proposals:
        return ConsumeOutcome("accepted", reason="无选品建议产出（proposals 为空）")

    # ---- 1. 遍历 proposals 构造 TaskProposal（source 追溯注入）----
    accepted = 0
    rejected = 0
    for prop_dict in result.proposals:
        try:
            merged = {**prop_dict}
            if source is not None and "source" not in merged:
                merged["source"] = source
            proposal = TaskProposal(**merged)
            outcome = await consume_task_proposal(
                proposal.model_dump(),
                registry=registry,
                audit_lookup=audit_lookup,
                whitelist=whitelist,
                biz_client=biz_client,
            )
            if outcome.status == "accepted":
                accepted += 1
            else:
                rejected += 1
                logger.warning("选品提案被拒: %s", outcome.reason)
        except Exception as e:
            rejected += 1
            logger.warning("选品提案处理异常: %s", e)

    return ConsumeOutcome(
        "accepted",
        reason=f"选品提案处理完成：{accepted} 接受，{rejected} 拒绝",
    )
