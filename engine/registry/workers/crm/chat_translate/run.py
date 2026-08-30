"""chat_translate 工序 ACT（P3-1 契约：``run(inputs, ctx) -> RegisteredModel``）。

REASON 产出 ChatTranscriptResult（逐条译文，已过 output Model 校验）-> ACT
消费 ctx.llm_output 校验后原样返回（runner 统一落库，R2 输出即类型）；
译文缺失 / 含空原文 -> 显式 ValueError（ACT 重试后 FAILED，宁失败不假成功）。
"""

from engine.core.context import EngineContext
from models.workers import ChatTranscriptInput, ChatTranscriptResult


def run(inputs: ChatTranscriptInput, ctx: EngineContext) -> ChatTranscriptResult:
    output = ctx.llm_output
    if not isinstance(output, ChatTranscriptResult) or not output.translations:
        raise ValueError("REASON 未产出可用译文（宁失败不假成功）")
    if any(not item.source_text.strip() for item in output.translations):
        raise ValueError("译文含空原文（宁失败不假成功）")
    return output
