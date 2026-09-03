"""product_suggestion 工序 ACT（详设-v0.6 §5.2，诚实化改造 T5，2026-09-03 批 4）。

LLM 工序（reason: llm，model: default 文本模型，非 vision）：
REASON 相位真调 LLM（prompt 含图片元数据：desc/tags/author/source/宽高/水印/url，
经 provider scrape.image_context 按 image_ids 拿）→ LLM 输出 SuggestionResult →
本 run() 校验 ctx.llm_output 为 SuggestionResult（Pydantic）→
**代码侧规范化 proposals（2026-09-03 复核修复：真实 LLM 输出不可靠）** →
输出（proposals 带 evidence ref_id=image_file.id 字符串，禁幻觉三件套由消费者
scrape.suggest 沿用：白名单 = 链 input image_ids）。

v0.5 现状是纯模板拼装（不调 LLM，选品建议 = 元数据复述）——诚实化 = 真消费
REASON 相位的 LLM 输出（ctx.llm_output），不再拼装。

**2026-09-03 复核修复（集成真跑暴露）**：真实 DeepSeek 输出的 proposals 是自由格式
（实测缺 domain/action_id/suggested_role/suggested_due_days/evidence、evidence.ref_id
为数字），直接落 TaskProposal 校验必拒（提案进不了审核页，页面看起来像占位按钮）。
修复 = AI 可以提出、代码负责补全：ACT 侧 _normalize_proposals 补全领域字段（domain=
scrape/action_id=scrape.suggest/role/截止天数从工序 config 读）、evidence 规范化
（ref_id 取 LLM 引用且在勾选图片内 → 转字符串；无引用则兜底勾选全部图片——消费者
白名单校验仍兜底）、title 非空否则丢弃该条（宁缺勿滥）。

降级：ctx.llm_output 为空/非 SuggestionResult → 返回空 proposals + 降级 note
（宁缺勿滥：不产「元数据复述」假建议；链仍可 DONE，消费者无建议跳过）。
"""

from __future__ import annotations

import logging

from engine.core.context import EngineContext
from models.workers import SuggestionInput, SuggestionResult

logger = logging.getLogger(__name__)


def _normalize_proposals(
    raw_proposals: list, image_ids: list[int], config: dict
) -> list[dict] | None:
    """把真实 LLM 的自由格式建议规范化为 TaskProposal 形状（AI 提出 → 代码补全）。

    - title 非空（≤80，契约上限）否则丢弃；detail 拼 LLM 内容字段
    - domain=scrape / action_id=scrape.suggest（硬编码领域）
    - suggested_role / suggested_due_days / evidence_max 一律从工序 config（R10）读，
      零业务词字面量（P1 机器执法：config 值入自动词表后代码不得再写字面量）
    - evidence：LLM 引用且在勾选图片白名单内的 ref_id → 转字符串；
      无有效引用则兜底引用勾选全部图片（单图建议最常见）
    - config 缺 default_role（工序配置缺失）→ 返回 None（调用方降级 note，
      fail-closed：宁缺勿滥，不产无法过契约的建议）
    """
    role = config.get("default_role")
    if not role:
        return None
    due_raw = config.get("default_due_days")
    if isinstance(due_raw, (int, float)):
        due: int | None = int(due_raw)
    elif isinstance(due_raw, str) and due_raw.strip().lstrip("-").isdigit():
        due = int(due_raw)
    else:
        due = None  # TaskProposal.suggested_due_days 可空（决策 11：截止手填 + AI 建议）
    cap_raw = config.get("evidence_max")
    if isinstance(cap_raw, (int, float)):
        cap: int | None = int(cap_raw)
    elif isinstance(cap_raw, str) and cap_raw.strip().lstrip("-").isdigit():
        cap = int(cap_raw)
    else:
        cap = None  # 不设则交 TaskProposal 契约 max_length 兜底

    valid_ids = {str(i) for i in image_ids}
    out: list[dict] = []
    for p in raw_proposals:
        if not isinstance(p, dict):
            continue
        title = str(p.get("title") or p.get("标题") or "").strip()
        if not title:
            continue  # 无标题 = 无可执行建议，宁缺勿滥

        detail_parts = []
        for key in ("detail", "描述", "卖点", "target_market", "目标市场", "建议关键词", "suggestion"):
            val = p.get(key)
            if val is not None and str(val).strip():
                detail_parts.append(str(val).strip())
        detail = "\n".join(detail_parts) if detail_parts else f"选品建议：{title}"

        refs: list[dict] = []
        ev_raw = p.get("evidence")
        if isinstance(ev_raw, list):
            for e in ev_raw:
                if not isinstance(e, dict):
                    continue
                rid = e.get("ref_id")
                if rid is None:
                    continue
                if isinstance(rid, (int, float)):
                    rid_s = str(int(rid))
                else:
                    rid_s = str(rid).strip()
                if rid_s in valid_ids:
                    refs.append({"kind": "image", "ref_id": rid_s})
        if not refs:
            # LLM 未给可追溯引用 → 兜底引用勾选图片（消费者白名单仍校验）
            refs = [
                {"kind": "image", "ref_id": i}
                for i in sorted(valid_ids, key=lambda x: int(x))
            ]
        if cap is not None:
            refs = refs[:cap]
        out.append(
            {
                "title": title[:80],
                "detail": detail,
                "domain": "scrape",
                "action_id": "scrape.suggest",
                "suggested_role": role,
                "suggested_due_days": due,
                "evidence": refs,
            }
        )
    return out


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

    raw_proposals = llm_output.proposals or []
    config = ctx.config if ctx.config else {}
    normalized = _normalize_proposals(raw_proposals, inputs.image_ids, config)
    if normalized is None:
        return SuggestionResult(
            proposals=[],
            note="工序 config 缺 default_role（R10 规格缺失），建议落库被拒——降级不产建议",
        )
    proposals = normalized
    dropped = len(raw_proposals) - len(proposals)
    note_parts = [
        f"基于 {len(inputs.image_ids)} 张图片元数据由 LLM 生成 "
        f"{len(proposals)} 个选品建议（代码补全 domain/role/evidence 白名单化）"
    ]
    if dropped > 0:
        note_parts.append(f"丢弃 {dropped} 条无标题建议")
    if not proposals and raw_proposals:
        note_parts.append("全部建议未过规范化（无有效标题/证据）")
    return SuggestionResult(proposals=proposals, note="；".join(note_parts))
