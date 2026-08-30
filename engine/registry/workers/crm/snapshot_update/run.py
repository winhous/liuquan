"""snapshot_update 工序 ACT（P3-1 契约：``run(inputs, ctx) -> RegisteredModel``）。

REASON 产出 CustomerSnapshotResult（滚动快照）-> ACT 消费 ctx.llm_output 校验
summary/current_need 至少其一非空后原样返回；空快照 -> ValueError（宁失败不假成功）。
"""

from engine.core.context import EngineContext
from models.workers import CustomerSnapshotResult, SnapshotUpdateInput


def run(inputs: SnapshotUpdateInput, ctx: EngineContext) -> CustomerSnapshotResult:
    output = ctx.llm_output
    if not isinstance(output, CustomerSnapshotResult):
        raise ValueError("REASON 未产出可用快照（宁失败不假成功）")
    if not output.summary.strip() and not output.current_need.strip():
        raise ValueError("快照 summary/current_need 均为空（宁失败不假成功）")
    return output
