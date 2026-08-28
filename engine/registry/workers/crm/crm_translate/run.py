"""crm_translate 工序 ACT（P3-1 契约：``run(inputs, ctx) -> RegisteredModel``；任务 T10）。

消费 ``ctx.llm_output``（REASON 的翻译结果，2026-08-28 集成修复）：
ACT 副作用 = 校验译文可用后原样返回，交由 runner 落库（R2 输出即类型）；
译文缺失 / 非 ChatTranslateResult -> 显式 ValueError（ACT 重试后 FAILED，
宁失败不假成功，详设 §2.1/§5.1——不产假结果、无静默降级）。
"""

from engine.core.context import EngineContext
from models.workers import ChatTranslateInput, ChatTranslateResult


def run(inputs: ChatTranslateInput, ctx: EngineContext) -> ChatTranslateResult:
    """取 REASON 的翻译结果；不可用即抛错（不产假成功）。"""
    output = ctx.llm_output
    if not isinstance(output, ChatTranslateResult) or not output.translated:
        raise ValueError("REASON 未产出可用译文（宁失败不假成功）")
    return output
