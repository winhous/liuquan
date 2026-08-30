"""customer_reply_draft 工序 ACT（P3-1 契约：``run(inputs, ctx) -> RegisteredModel``）。

回复台三模式（对齐广成 customer-reply-draft）：
- REASON 产出 ReplyDraftResult（reply_en 英文 + reply_zh 中文回述）。
- **literal 模式 reply_zh 原样回传卖家全文（代码路径，不经模型）**——模型只产
  reply_en 严格英译，reply_zh 用 input.full_text 覆盖（防模型润色）。
- 校验：reply_en 非空；mode=points 无 points / mode=literal 无 full_text /
  auto 无上下文（messages+snapshot 空）-> ValueError（宁失败不假成功）。
"""

from engine.core.context import EngineContext
from models.workers import ReplyDraftInput, ReplyDraftResult


def run(inputs: ReplyDraftInput, ctx: EngineContext) -> ReplyDraftResult:
    mode = inputs.mode
    if mode == "points" and not inputs.points.strip():
        raise ValueError("mode=points 但回复要点为空（宁失败不假成功）")
    if mode == "literal" and not inputs.full_text.strip():
        raise ValueError("mode=literal 但中文回复全文为空（宁失败不假成功）")
    output = ctx.llm_output
    if not isinstance(output, ReplyDraftResult) or not output.reply_en.strip():
        raise ValueError("REASON 未产出可用英文回复（宁失败不假成功）")
    if mode == "literal":
        output = output.model_copy(update={"reply_zh": inputs.full_text})
    return output
