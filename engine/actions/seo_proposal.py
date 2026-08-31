"""seo_proposal 消费者（详设-v0.5 §6.4；批 3 新增）。

seo_optimize_chain 完成 → 消费者 seo.optimize（action_id=seo.optimize）：
SeoOptimizationReport → TaskProposal(domain=seo) → POST /api/biz/tm/proposals。

职责：
1. 契约归一（dict -> SeoOptimizationReport，R2）；
2. 构造 TaskProposal（title「SEO 优化建议：<目标关键词或商品>」，detail 摘要建议，
   evidence ref_id 用报告里的关键词/输入 ref）；
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
from models.workers import SeoOptimizationReport

logger = logging.getLogger(__name__)


async def consume_seo_proposal(
    deliverable: dict,
    *,
    registry: object | None = None,
    audit_lookup: Callable[[list[str]], bool] | None = None,
    whitelist: Collection[str] | None = None,
    biz_client: BizApiClient | None = None,
    **_kwargs,
) -> ConsumeOutcome:
    """SEO 优化提案消费者：SeoOptimizationReport → TaskProposal → tm 转交器落库。"""
    # ---- 0. 契约归一 ----
    try:
        report = SeoOptimizationReport.model_validate(deliverable)
    except ValidationError as exc:
        return ConsumeOutcome(
            "rejected", reason=f"SEO 优化报告未过 SeoOptimizationReport 契约校验：{exc}"
        )

    # ---- 1. 构造 TaskProposal ----
    # title = 「SEO 优化建议：<目标关键词或商品>」
    target_desc = ""
    if report.original:
        parts = []
        if report.original.title:
            parts.append(report.original.title)
        if report.original.tags:
            parts.append(", ".join(report.original.tags[:3]))
        target_desc = " / ".join(parts) if parts else ""
    if not target_desc and report.seo_keywords:
        target_desc = ", ".join(report.seo_keywords[:3])

    title = f"SEO 优化建议：{target_desc}" if target_desc else "SEO 优化建议"

    # detail = 摘要建议
    detail_parts = []
    if report.titles:
        detail_parts.append(f"标题候选 {len(report.titles)} 个")
    if report.tags:
        detail_parts.append(f"标签 {len(report.tags)} 个")
    if report.listing_description:
        detail_parts.append("描述已重写")
    if report.seo_keywords:
        detail_parts.append(f"SEO 关键词 {len(report.seo_keywords)} 个")
    if report.note:
        detail_parts.append(f"备注：{report.note}")
    detail = "；".join(detail_parts) if detail_parts else "SEO 优化建议已生成"

    # evidence = 用报告里的关键词作为 ref_id
    evidence_refs = []
    if report.seo_keywords:
        for kw in report.seo_keywords[:5]:
            evidence_refs.append({"kind": "listing", "ref_id": kw, "quote": kw})
    if not evidence_refs and report.original and report.original.title:
        evidence_refs.append({"kind": "listing", "ref_id": report.original.title, "quote": report.original.title})

    proposal = TaskProposal(
        action_id="seo.optimize",
        title=title,
        detail=detail,
        domain="seo",
        suggested_role="运营",
        suggested_due_days=3,
        evidence=evidence_refs,
        source={
            "chain_id": "",
            "engine_task_id": "",
            "worker_id": "seo_optimize",
            "audit_ids": [],
        },
    )

    # ---- 2. 复用 tm_proposal 转交器 ----
    return await consume_task_proposal(
        proposal,
        registry=registry,
        audit_lookup=audit_lookup,
        whitelist=whitelist,
        biz_client=biz_client,
    )


__all__ = ["consume_seo_proposal"]
