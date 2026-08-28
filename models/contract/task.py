"""§6.4 Task（引擎 -> TM：「帮我派活给人」）= TaskProposal（详设-v0.1 §6.4）。

公共契约，v0.1 定稿即冻结。§6.4 的两条业务约束（evidence 空 + suggest
拒、audit_ids 可查）是代码校验，放 models/contract/validation.py，
不在本模块（模型层只定义形状，校验函数层执行规则）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SourceTrace(BaseModel):
    """来源追溯：任何任务能回溯到触发它的 AI 调用。"""

    chain_id: str
    engine_task_id: str  # 展示形 e-000123
    worker_id: str
    audit_ids: list[str] = []  # 支撑结论的 LLM 调用（可回放证据）


class EvidenceRef(BaseModel):
    """依据引用：提案必须可追溯业务事实。"""

    kind: Literal["message", "metric", "order_view", "listing", "image"]
    ref_id: str  # 业务侧对象 id
    quote: str | None = None  # 原文摘录（如那句要跟进的买家消息）


class TaskProposal(BaseModel):
    """任务提案：AI 建议 TM 派活给人，人工审核后执行（决策 11/12，无代码自动通过）。"""

    title: str = Field(min_length=1, max_length=80)
    detail: str  # 给执行人看的完整说明
    domain: str
    action_id: str  # 关联 Action 声明
    suggested_priority: Literal["P0", "P1", "P2", "P3"]  # 建议，人工可改（决策 11）
    suggested_role: Literal["运营", "采购", "管理员"]
    suggested_due_days: int | None  # AI 建议（P0=0/P1=3/P2=7/P3=None），人工可改
    # 条数上限 20：pydantic v2 的 list 限长用 max_length（详设 §6.4 的 max_items 是 v1 写法）
    evidence: list[EvidenceRef] = Field(max_length=20)
    source: SourceTrace
