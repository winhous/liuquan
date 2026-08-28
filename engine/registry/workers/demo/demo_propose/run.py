"""demo_propose 工序 ACT（P3-1 契约：``run(inputs, ctx) -> RegisteredModel``；详设-v0.2 §7）。

纯代码工序（worker.yaml 的 reason: none，无 LLM 调用，零 token 成本）：把
demo_echo 的 echo 结果组织成 TaskProposal 契约输出——域 demo / action
tm.proposal / 默认角色与文案按工序 config/ 声明（R10 规格外置，本文件零
config 值字面量）；evidence 引用链喂给本工序的输入对象 id（决策 16 数据引用
封闭性）；无依据输入（空 ref_id / 空文本）拒产提案（详设 v0.1 §6.4：无依据
不出建议，宁失败不假成功）。
"""

from engine.core.context import EngineContext
from models.contract.task import EvidenceRef, SourceTrace, TaskProposal
from models.workers import DemoProposeInput


def run(inputs: DemoProposeInput, ctx: EngineContext) -> TaskProposal:
    """把 echo 文本组织成任务提案（确定性；无依据输入 -> ValueError）。"""
    text = inputs.text.strip()
    ref_id = inputs.ref_id.strip()
    if not text or not ref_id:
        raise ValueError(
            "demo_propose 无依据输入：evidence 必须引用链输入的 ref_id 与文本"
            "（无依据不出建议，详设 v0.1 §6.4）"
        )
    title_template = str(ctx.config["title_template"])
    detail_template = str(ctx.config["detail_template"])
    title = title_template.format(text=text)[:80]  # 契约 title 上限 80，截断保合规
    detail = detail_template.format(text=text, ref_id=ref_id)
    return TaskProposal(
        title=title,
        detail=detail,
        domain=ctx.domain,
        action_id="tm.proposal",
        suggested_role=ctx.config["default_role"],
        suggested_due_days=ctx.config.get("default_due_days"),
        evidence=[EvidenceRef(kind=inputs.kind, ref_id=ref_id, quote=text)],
        source=SourceTrace(
            chain_id=ctx.chain_id or "",
            engine_task_id=(
                f"e-{ctx.task_id:06d}" if ctx.task_id is not None else ""
            ),
            worker_id=ctx.worker_id,
            audit_ids=[],  # 纯代码工序无 LLM 调用，无审计条目（SourceTrace 缺省）
        ),
    )
