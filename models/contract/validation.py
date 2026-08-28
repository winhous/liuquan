"""§6.4 业务校验函数：代码校验，非 AI 自律（详设-v0.1 §6.4 约束；规范 R3 第二层）。

模型层只定义形状；本模块执行「无依据不出建议」「audit_ids 可追溯」两条
业务规则。调用方（引擎 ACT 相位 / 验收层）按工序的 risk 决定何时调用。
"""

from __future__ import annotations

from collections.abc import Callable

from .task import TaskProposal


def assert_suggest_has_evidence(proposal: TaskProposal) -> None:
    """risk: suggest 的工序产出提案时 evidence 不得为空（无依据不出建议）。

    由 risk: suggest 工序的 ACT 相位在产出提案后调用；违反抛 ValueError。
    """
    if not proposal.evidence:
        raise ValueError(
            "risk: suggest 的工序产出提案必须携带 evidence"
            "（无依据不出建议，详设 §6.4）"
        )


def validate_audit_ids(
    proposal: TaskProposal, audit_lookup: Callable[[list[str]], bool]
) -> bool:
    """audit_ids 非空且可在审计表查到，返回 bool。

    audit_lookup 由调用方注入（真实场景 = 查 engine_audit 表的可调用；
    测试 = 桩函数，不打真库）。audit_ids 为空或 lookup 查不到都返回 False。
    """
    ids = proposal.source.audit_ids
    if not ids:
        return False
    return audit_lookup(ids)
