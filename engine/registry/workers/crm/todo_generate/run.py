"""todo_generate 工序 ACT（P3-1 契约：``run(inputs, ctx) -> RegisteredModel``）。

REASON 产出 TodoCandidateResult（候选数组，已过 output Model 校验）-> ACT 校验：
每条候选 content 非空且 evidence 非空（决策 16① 无依据不出建议），违规项剔除；
全部剔除 -> ValueError（宁失败不假成功）。
"""

from engine.core.context import EngineContext
from models.workers import TodoCandidateResult, TodoGenerateInput


def run(inputs: TodoGenerateInput, ctx: EngineContext) -> TodoCandidateResult:
    output = ctx.llm_output
    if not isinstance(output, TodoCandidateResult):
        raise ValueError("REASON 未产出可用待办候选（宁失败不假成功）")
    kept = [
        item
        for item in output.todos
        if item.content.strip() and item.evidence
    ]
    if not kept:
        raise ValueError("待办候选全部无依据或无内容（决策 16 无依据不出建议）")
    if len(kept) != len(output.todos):
        output = output.model_copy(update={"todos": kept})
    return output
