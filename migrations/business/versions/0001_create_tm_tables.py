"""业务库初始迁移：tm schema 三表（详设-v0.2 §3.1/§3.2/§3.3 DDL 逐列对齐，含全部约束与索引）。

task / task_proposal / task_event
- BIGSERIAL PK、TEXT、INT、DATE、BIGINT、JSONB、TIMESTAMPTZ 与 §3 一一对应
- 两个具名 CHECK（chk_blocked_reason / chk_result_note）与内联 CHECK（title
  长度/角色/五态/source_type/risk/审核态/event_type）全部落地
- derived_from / task_id 均为业务库内 FK（R23：跨库不建外键，合法）
- 索引 idx_task_status_due / idx_task_role / idx_proposal_status / idx_event_task
  （idx_proposal_status 为 (status, created_at DESC)，§3.2 原文）

修订号 0001（v0.2 首版）。迁移手写 DDL 与 models/tm.py 的 ORM 模型同源对齐
（字段/类型/默认值/约束/索引逐列一致，tests/test_tm_models.py 的 schema 测试
锁死）。schema tm 由部署侧建（详设 §2.1：pgdev.sh 建库建 schema），迁移内不建。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- tm.task：任务表（§3.1；五态 + 必填回复 + 派生 + 来源追溯）----
    op.create_table(
        "task",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("due", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("result_note", sa.Text(), nullable=True),
        sa.Column(
            "derived_from",
            sa.BigInteger(),
            sa.ForeignKey("tm.task.id"),
            nullable=True,
        ),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 80"),
        sa.CheckConstraint("role IN ('运营','采购','管理员')"),
        sa.CheckConstraint("status IN ('open','in_progress','done','void','blocked')"),
        sa.CheckConstraint("source_type IN ('ai','manual')"),
        sa.CheckConstraint("created_by IN ('运营','采购','管理员')"),
        sa.CheckConstraint(
            "(status = 'blocked' AND blocked_reason IS NOT NULL)"
            " OR (status <> 'blocked' AND blocked_reason IS NULL)",
            name="chk_blocked_reason",
        ),
        sa.CheckConstraint(
            "(status IN ('done','void') AND result_note IS NOT NULL)"
            " OR (status NOT IN ('done','void') AND result_note IS NULL)",
            name="chk_result_note",
        ),
        schema="tm",
    )
    op.create_index("idx_task_status_due", "task", ["status", "due"], schema="tm")
    op.create_index("idx_task_role", "task", ["role"], schema="tm")

    # ---- tm.task_proposal：AI 提案表（§3.2；全部人工审，决策 12）----
    op.create_table(
        "task_proposal",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("action_id", sa.Text(), nullable=False),
        sa.Column("risk", sa.Text(), nullable=False),
        sa.Column("suggested_role", sa.Text(), nullable=False),
        sa.Column("suggested_due_days", sa.Integer(), nullable=True),
        sa.Column(
            "evidence",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("source", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("tm.task.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 80"),
        sa.CheckConstraint("risk IN ('read','suggest','write')"),
        sa.CheckConstraint("suggested_role IN ('运营','采购','管理员')"),
        sa.CheckConstraint("status IN ('pending','approved','rejected')"),
        schema="tm",
    )
    op.create_index(
        "idx_proposal_status",
        "task_proposal",
        ["status", sa.text("created_at DESC")],
        schema="tm",
    )

    # ---- tm.task_event：状态流水表（§3.3；事件写入点代码写死，不依赖 AI）----
    op.create_table(
        "task_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("tm.task.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "event_type IN ('created','approved','started','completed','voided',"
            "'blocked','unblocked','derived','updated')"
        ),
        schema="tm",
    )
    op.create_index("idx_event_task", "task_event", ["task_id", "created_at"], schema="tm")


def downgrade() -> None:
    op.drop_index("idx_event_task", table_name="task_event", schema="tm")
    op.drop_table("task_event", schema="tm")
    op.drop_index("idx_proposal_status", table_name="task_proposal", schema="tm")
    op.drop_table("task_proposal", schema="tm")
    op.drop_index("idx_task_role", table_name="task", schema="tm")
    op.drop_index("idx_task_status_due", table_name="task", schema="tm")
    op.drop_table("task", schema="tm")
