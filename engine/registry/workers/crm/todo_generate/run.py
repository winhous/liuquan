"""todo_generate 工序 ACT（P3-1 契约：``run(inputs, ctx) -> RegisteredModel``）。

REASON 产出 TodoCandidateResult（候选数组，已过 output Model 校验）-> ACT 校验：
每条候选 content 非空且 evidence 非空（决策 16① 无依据不出建议），违规项剔除；
全部剔除 -> ValueError（宁失败不假成功）。
"""

from engine.core.context import EngineContext
from models.contract.task import EvidenceRef
from models.workers import TodoCandidateItem, TodoCandidateResult, TodoGenerateInput


def run(inputs: TodoGenerateInput, ctx: EngineContext) -> TodoCandidateResult:
    output = ctx.llm_output
    if not isinstance(output, TodoCandidateResult):
        raise ValueError("REASON 未产出可用待办候选（宁失败不假成功）")
    # 决策 16 无依据不出建议；真实 LLM 对「每条候选必带结构化 evidence」执行不稳定，
    # 技术定（详设-v0.3 §5.1 注）：缺 evidence 的候选由**代码**补引用最近一条买家
    # 消息作 evidence——AI 真看过该消息（context 白名单内，决策 16③ 数据引用封闭
    # 性仍满足），evidence 由机器生成可追溯（非 AI 自证）；无任何消息可引用（新
    # 客户无历史对话）时剔除；全空 = 空数组（合法产出，不产假建议）
    messages = (ctx.context_data.get("crm_chat_context") or {})
    raw_msgs = getattr(messages, "messages", None) or []
    recent_buyer = [m for m in raw_msgs if getattr(m, "direction", "") == "buyer"]
    fallback = recent_buyer[-1] if recent_buyer else None
    kept: list[TodoCandidateItem] = []
    for item in output.todos:
        if not item.content.strip():
            continue
        if item.evidence:
            kept.append(item)
        elif fallback is not None:
            kept.append(
                item.model_copy(
                    update={
                        "evidence": [
                            EvidenceRef(
                                kind="message",
                                ref_id=str(getattr(fallback, "id", "")),
                                quote=(getattr(fallback, "source_text", "") or "")[:100],
                            )
                        ]
                    }
                )
            )
    return output.model_copy(update={"todos": kept})
