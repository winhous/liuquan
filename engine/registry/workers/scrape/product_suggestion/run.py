"""product_suggestion 工序 ACT（详设-v0.6 §5.2，诚实化改造 T5，2026-09-03 批 4）。

LLM 工序（reason: llm，model: default 文本模型，非 vision）：
REASON 相位真调 LLM（prompt 含图片元数据：desc/tags/author/source/宽高/水印/url，
经 provider scrape.image_context 按 image_ids 拿）→ LLM 输出 SuggestionResult →
本 run() 校验 ctx.llm_output 为 SuggestionResult（Pydantic）→ 透传输出
（proposals 带 evidence ref_id=image_file.id，禁幻觉三件套由消费者 scrape.suggest
沿用：白名单 = 链 input image_ids）。

v0.5 现状是纯模板拼装（不调 LLM，选品建议 = 元数据复述）——诚实化 = 真消费
REASON 相位的 LLM 输出（ctx.llm_output），不再拼装。

降级：ctx.llm_output 为空/非 SuggestionResult → 返回空 proposals + 降级 note
（宁缺勿滥：不产「元数据复述」假建议；链仍可 DONE，消费者无建议跳过）。
"""

from __future__ import annotations

import logging

from engine.core.context import EngineContext
from models.workers import SuggestionInput, SuggestionResult

logger = logging.getLogger(__name__)


def run(inputs: SuggestionInput, ctx: EngineContext) -> SuggestionResult:
    """校验并透传 REASON 相位的 LLM 输出（诚实化：真接 LLM，不做模板拼装）。"""
    llm_output = ctx.llm_output

    if not isinstance(llm_output, SuggestionResult):
        reason = (
            "LLM 输出缺失或未过 SuggestionResult 校验"
            f"（实际 {type(llm_output).__name__ if llm_output is not None else 'None'}）"
        )
        logger.warning("product_suggestion: %s（降级：不产建议）", reason)
        return SuggestionResult(proposals=[], note=reason)

    proposals = llm_output.proposals or []
    return SuggestionResult(
        proposals=proposals,
        note=(
            f"基于 {len(inputs.image_ids)} 张图片元数据由 LLM 生成 "
            f"{len(proposals)} 个选品建议（evidence ref_id 白名单由消费者校验）"
        ),
    )
