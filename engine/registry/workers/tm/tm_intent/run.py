"""tm_intent 工序 ACT（P3-1 契约：``run(inputs, ctx) -> RegisteredModel``）。

REASON 产出 IntentResult（结构化流转指令，已过 output Model 校验）-> ACT 校验：
action 必须命中 config/actions.yaml 声明的动作集合（R10：动作枚举住 config，
run.py 零字面量——P1 词表来源）；不满足 -> ValueError（宁失败不假成功，
模糊澄清不猜测执行——决策 28）。
"""

from engine.core.context import EngineContext
from models.workers import IntentInput, IntentResult


def run(inputs: IntentInput, ctx: EngineContext) -> IntentResult:
    output = ctx.llm_output
    if not isinstance(output, IntentResult):
        raise ValueError("REASON 未产出结构化指令（宁失败不假成功）")
    actions = set((ctx.config.get("actions") or {}).keys())
    if not actions:
        raise ValueError("config/actions.yaml 未声明动作清单（R10 规格外置缺失）")
    if output.action not in actions:
        raise ValueError(f"指令动作非法：{output.action!r}（宁失败不假成功）")
    return output
