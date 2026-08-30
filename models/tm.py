"""业务库 ORM（database: liuquan, schema: tm）—— task / task_proposal / task_event 三表。

纯数据层（详设-v0.2 §5：无业务逻辑；web 与引擎转交器共用同一 Model，
承开发规范 R22「唯一通用语言」）。字段/类型/默认值/约束/索引与
migrations/business/versions/ 的 DDL 逐列同源（R22：结构漂移活不过启动；
tests/test_tm_models.py 的 schema 测试锁死）。

v0.3 扩展（迁移 0003，详设-v0.3 §4）：task +tags/+ai_suggestion（决策 25/27）；
task_event +detail + event_type 扩 12 值（suggested/transferred/disagreed，
决策 27/28 流转留痕）。crm 四表 ORM 见 models/crm.py（复用本模块 TmBase）。

来源追溯结构（详设-v0.2 §3.4，JSONB）：
- source_type='ai'   : {"chain_id", "engine_task_id", "worker_id",
                        "audit_ids": [...], "proposal_id"}   # proposal_id 批准时回填
- source_type='manual': {"creator": "运营"}                    # 人工建任务，creator 即依据

跨库不建外键（R23/详设 §2.1）：task.source 存引擎任务 id 展示形，不 FK 引擎库；
derived_from / task_id 均为业务库内 FK（合法）。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class TmBase(DeclarativeBase):
    """业务库 ORM 基类（与 migrations/business/env.py 的 target_metadata 同源）。"""


class Task(TmBase):
    """tm.task：任务表（详设-v0.2 §3.1）。

    五态 open/in_progress/done/void/blocked；结束（done/void）必填 result_note
    （chk_result_note 强制，决策 17「不回复不能结束」）；blocked 必填
    blocked_reason（chk_blocked_reason 强制，决策 11）；derived_from 派生来源
    （派生≠原任务结束，决策 17）。
    """

    __tablename__ = "task"
    __table_args__ = (
        CheckConstraint("length(title) BETWEEN 1 AND 80"),  # 对齐 TaskProposal.title
        CheckConstraint("role IN ('运营','采购','管理员')"),  # 负责人按角色指派（决策 11）
        CheckConstraint("status IN ('open','in_progress','done','void','blocked')"),  # 五态
        CheckConstraint("source_type IN ('ai','manual')"),  # ai=提案批准 / manual=人工创建
        CheckConstraint("created_by IN ('运营','采购','管理员')"),  # 创建人角色
        CheckConstraint(
            "(status = 'blocked' AND blocked_reason IS NOT NULL)"
            " OR (status <> 'blocked' AND blocked_reason IS NULL)",
            name="chk_blocked_reason",
        ),
        CheckConstraint(
            "(status IN ('done','void') AND result_note IS NOT NULL)"
            " OR (status NOT IN ('done','void') AND result_note IS NULL)",
            name="chk_result_note",
        ),
        Index("idx_task_status_due", "status", "due"),
        Index("idx_task_role", "role"),
        {"schema": "tm"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)  # 给执行人的完整说明（人工任务可空）
    domain: Mapped[str] = mapped_column(Text, nullable=False)  # 来源业务域 crm/erp/seo/tm/...（决策 16）
    role: Mapped[str] = mapped_column(Text, nullable=False)  # 负责人按角色指派（决策 11）
    due: Mapped[date] = mapped_column(Date, nullable=False)  # 截止：手填 + AI 建议可改（决策 11）
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'open'")
    )
    blocked_reason: Mapped[str | None] = mapped_column(Text)  # status=blocked 时必填
    result_note: Mapped[str | None] = mapped_column(Text)  # 人工回复：结束（done/void）前必填（决策 17）
    derived_from: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tm.task.id")
    )  # 派生来源：本任务由哪个任务派生（决策 17；派生≠原任务结束）
    source_type: Mapped[str] = mapped_column(Text, nullable=False)  # ai=提案批准 / manual=人工创建
    source: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 来源追溯（§3.4）
    created_by: Mapped[str] = mapped_column(Text, nullable=False)  # 创建人角色
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    done_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # 勾选完成时写入（统计/回流用）
    tags: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )  # 开放标签 list[str]（决策 25；1-30 字符/去重/最多 20 个，应用层校验）
    ai_suggestion: Mapped[dict | None] = mapped_column(
        JSONB
    )  # AI 下一步建议（决策 27，详设 §4.1 结构；来源 = 候选 suggested_next）


class TaskProposal(TmBase):
    """tm.task_proposal：AI 提案表（详设-v0.2 §3.2，全部人工审，决策 12）。

    risk 由代码规则标注（域+动作类型查表），非 AI 自评（R9）；evidence 非空
    + audit_ids 可查 + ref_id 数据引用封闭性 = 禁幻觉三件套（决策 16，机器
    校验在引擎转交器执行，本模块只定义形状）。
    """

    __tablename__ = "task_proposal"
    __table_args__ = (
        CheckConstraint("length(title) BETWEEN 1 AND 80"),
        CheckConstraint("risk IN ('read','suggest','write')"),  # 风险级：代码规则标注，非 AI 自评
        CheckConstraint("suggested_role IN ('运营','采购','管理员')"),
        CheckConstraint("status IN ('pending','approved','rejected')"),  # 审核态三态
        Index("idx_proposal_status", "status", desc("created_at")),
        {"schema": "tm"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)  # 给执行人的完整说明（TaskProposal.detail）
    domain: Mapped[str] = mapped_column(Text, nullable=False)  # 工序域（TaskProposal.domain）
    action_id: Mapped[str] = mapped_column(Text, nullable=False)  # 关联 Action 声明
    risk: Mapped[str] = mapped_column(Text, nullable=False)  # 风险级：read/suggest/write
    suggested_role: Mapped[str] = mapped_column(Text, nullable=False)  # AI 建议角色
    suggested_due_days: Mapped[int | None] = mapped_column(Integer)  # AI 建议截止天数（独立建议）
    evidence: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )  # EvidenceRef 数组：kind/ref_id/quote（对齐契约 §6.4）
    source: Mapped[dict] = mapped_column(JSONB, nullable=False)  # SourceTrace：chain_id/engine_task_id/worker_id/audit_ids
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    reviewed_by: Mapped[str | None] = mapped_column(Text)  # 审核人角色
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)  # 驳回理由（不强制）
    task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tm.task.id")
    )  # 批准后关联生成的任务（业务库内 FK，合法）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaskEvent(TmBase):
    """tm.task_event：状态流水表（详设-v0.2 §3.3，事件写入点代码写死，不依赖 AI）。

    event_type 十二态：created/approved/started/completed/voided/blocked/unblocked/
    derived/updated（v0.2 九态）+ suggested/transferred/disagreed（v0.3 流转留痕，
    决策 27/28，详设-v0.3 §4.2）：
    - suggested：AI 建议写入 ai_suggestion 时（note=建议摘要，detail=ai_suggestion 快照）
    - transferred：人执行流转时（from/to_status 沿用；detail={action, target, note}）
    - disagreed：人选择与 AI 建议不同时（detail={ai_suggestion, human_chose}）
    """

    __tablename__ = "task_event"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created','approved','started','completed','voided',"
            "'blocked','unblocked','derived','updated','suggested','transferred',"
            "'disagreed')",
            name="chk_event_type",
        ),
        Index("idx_event_task", "task_id", "created_at"),
        {"schema": "tm"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tm.task.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_status: Mapped[str | None] = mapped_column(Text)  # 迁移前状态（创建事件为空）
    to_status: Mapped[str | None] = mapped_column(Text)  # 迁移后状态
    actor: Mapped[str | None] = mapped_column(Text)  # 操作者角色
    note: Mapped[str | None] = mapped_column(Text)  # 备注（如 blocked_reason / result_note）
    detail: Mapped[dict | None] = mapped_column(
        JSONB
    )  # 结构化详情（from/to/note/指令解析，决策 27/28，详设 §4.2）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
