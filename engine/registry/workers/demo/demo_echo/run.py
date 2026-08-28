"""demo_echo 工序 ACT（P3-1 契约：``run(inputs, ctx) -> RegisteredModel``；任务 T10）。

确定性回显：ACT 把入参原样返回（demo-echo 桩驱动，结构化冒烟）；
REASON 的 LLM 结果在 ``ctx.llm_output``，本工序不消费（职责分离：
REASON 产出 -> ACT 执行副作用，§2.1）。
"""

from engine.core.context import EngineContext
from models.workers import DemoEchoInput, DemoEchoResult


def run(inputs: DemoEchoInput, ctx: EngineContext) -> DemoEchoResult:
    """回显入参文本（确定性；输出经 runner 过 output Model 校验后落库）。"""
    return DemoEchoResult(text=inputs.text)
