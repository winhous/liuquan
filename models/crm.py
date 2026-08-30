"""业务库 ORM（database: liuquan, schema: crm）—— customer/message/snapshot/todo_candidate 四表。

纯数据层（详设-v0.3 §3：无业务逻辑；web 与引擎消费者共用同一 ORM，
承开发规范 R22「唯一通用语言」）。字段/类型/默认值/约束/索引与
migrations/business/versions/0002 的 DDL 逐列同源（R22：结构漂移活不过启动；
tests/test_crm_models.py 的 schema 测试锁死）。

复用 models/tm.py 的 TmBase（业务库 declarative base，R22；v0.2 已发版结构不动）。
crm 表注册进 TmBase.metadata 后，migrations/business/env.py 的 target_metadata
自动涵盖两 schema（R22 同源，env.py 已 import 本模块）。

对齐详设 §3：
- customer：五态 follow_up_status + 三阶段 trade_stage（EOMS 语义平移）；
  逾期语义 = max(last_contacted_at, updated_at) 超 FOLLOW_UP_DAYS 无动静
  （config 外置，页面层判定，本模块只定义形状）
- message：append-only（只插入不更新；message_time 可空 = 粘贴不标，决策 23）
- snapshot：滚动快照版本化（每客户多行，取 created_at 最新一行为当前；
  need_history 只增不减；todos 为候选摘要列表，非任务实体，详设 §3.3）
- todo_candidate：evidence 非空强制（chk_candidate_evidence，决策 16 禁幻觉①）；
  status pending/confirmed/dismissed（confirmed=已转任务，决策 19）；
  confirmed_task_id 确认事务内回填 tm.task id（追溯闭环，详设 §8.1）

跨库不建外键（R23/详设 §2.1）：engine_task_id 不 FK 引擎库；confirmed_task_id
同库但不建 FK——按 R23 走声明，关联走 tm.task.source JSONB 的 customer_id
（决策 24：任务只按 domain 区分，客户 = 关联对象）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.tm import TmBase


class Customer(TmBase):
    """crm.customer：客户表（详设-v0.3 §3.1）。

    五态 follow_up_status（waiting_reply/replied/closed_deal/on_hold/archived，
    archived 不参与逾期判定、默认列表不展示）+ 三阶段 trade_stage
    （pre_sale/in_sale/after_sale）；latest_summary 为页面层维护的冗余
    （最新快照 summary）；last_contacted_at 粘贴归档/回复发送时刷新。
    """

    __tablename__ = "customer"
    __table_args__ = (
        CheckConstraint("length(nickname) BETWEEN 1 AND 255"),  # 重名忽略大小写精确匹配（应用层）
        CheckConstraint(
            "follow_up_status IN "
            "('waiting_reply','replied','closed_deal','on_hold','archived')"
        ),
        CheckConstraint("trade_stage IN ('pre_sale','in_sale','after_sale')"),
        Index("idx_crm_customer_status", "follow_up_status"),
        Index("idx_crm_customer_updated", desc("updated_at")),
        {"schema": "crm"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    nickname: Mapped[str] = mapped_column(Text, nullable=False)
    source_shop: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )  # 来源店铺
    remark: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )  # 备注
    follow_up_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'waiting_reply'")
    )
    trade_stage: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pre_sale'")
    )
    order_no: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )  # 订单号备注（可空语义 = 空串）
    latest_summary: Mapped[str | None] = mapped_column(
        Text
    )  # 冗余：最新快照 summary（页面层维护）
    last_contacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # 最新操作时间（粘贴归档/回复发送刷新）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Message(TmBase):
    """crm.message：对话消息表（详设-v0.3 §3.2，append-only）。

    append-only：只插入不更新（时间线提供人工删除口，误贴清理）；
    message_time 一律留空（决策 23：历史记录/正常粘贴均不标时间）。
    """

    __tablename__ = "message"
    __table_args__ = (
        CheckConstraint("direction IN ('buyer','seller')"),
        Index("idx_crm_message_customer", "customer_id", "created_at"),
        {"schema": "crm"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("crm.customer.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_text: Mapped[str] = mapped_column(Text, nullable=False)  # 原文
    translated_text: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )  # 中文译文
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )  # 原文语种
    message_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # 可空：粘贴场景不标（决策 23）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Snapshot(TmBase):
    """crm.snapshot：客户快照表（详设-v0.3 §3.3，滚动快照版本化）。

    每客户多行，取 created_at 最新一行为当前快照（EOMS 验证语义）；
    need_history 需求变更史只增不减；todos 为 AI 滚动总结携带的待办摘要
    （供 todo_generate 防重与上下文，不是任务实体——任务实体 =
    tm.task + crm.todo_candidate）。
    """

    __tablename__ = "snapshot"
    __table_args__ = (
        Index("idx_crm_snapshot_customer", "customer_id", desc("created_at")),
        {"schema": "crm"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("crm.customer.id", ondelete="CASCADE"),
        nullable=False,
    )
    current_need: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    need_history: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )  # list[str]，需求变更史只增不减
    sentiment: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    todos: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )  # list[str]，历史候选摘要（供防重/上下文）
    summary: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TodoCandidate(TmBase):
    """crm.todo_candidate：待办候选表（详设-v0.3 §3.4，决策 19/25）。

    evidence 非空强制（chk_candidate_evidence：jsonb_array_length(evidence) > 0，
    决策 16 禁幻觉①：无依据候选不可确认——DB CHECK 兜底，页面层 + 引擎消费者
    双层校验）；status pending/confirmed/dismissed（confirmed=已转任务）；
    confirmed_task_id 确认事务内回填 tm.task id（追溯闭环：候选 -> 任务）。
    防重：同一 customer_id 下 status=pending 的候选确认后置 confirmed，
    重复确认拒（应用层 + DB 检查，详设 §3.4）。
    """

    __tablename__ = "todo_candidate"
    __table_args__ = (
        CheckConstraint("length(content) BETWEEN 1 AND 200"),  # 中文动宾短语
        CheckConstraint("status IN ('pending','confirmed','dismissed')"),
        CheckConstraint(
            "jsonb_array_length(evidence) > 0", name="chk_candidate_evidence"
        ),
        Index("idx_crm_candidate_customer", "customer_id", "status"),
        Index("idx_crm_candidate_status", "status", desc("created_at")),
        {"schema": "crm"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("crm.customer.id", ondelete="CASCADE"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )  # 生成依据（一句话，给人看）
    suggested_tags: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )  # AI 建议标签 list[str]（决策 25，确认时写入 tm.task.tags）
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False)  # EvidenceRef[]（type/ref_id/quote，决策 16）
    suggested_next: Mapped[dict | None] = mapped_column(
        JSONB
    )  # AI 对任务生成后的下一步建议（决策 27 闭环：确认时写入 tm.task.ai_suggestion）
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    engine_task_id: Mapped[int | None] = mapped_column(
        BigInteger
    )  # 来源引擎任务（追溯，跨库不建 FK）
    confirmed_task_id: Mapped[int | None] = mapped_column(
        BigInteger
    )  # 确认后生成的 tm.task id（追溯，按 R23 走声明不建 FK）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
