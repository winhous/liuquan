"""业务库迁移 0002：crm schema 四表（详设-v0.3 §3 字段级 DDL 逐列对齐，含全部约束与索引）。

customer / message / snapshot / todo_candidate
- 列类型（BIGSERIAL/TEXT/JSONB/TIMESTAMPTZ）、NOT NULL、server_default 与 §3 一一对应
- CHECK：customer 五态 follow_up_status + 三阶段 trade_stage + nickname 长度；
  message direction buyer/seller；todo_candidate content 长度 + status 三态 +
  **chk_candidate_evidence（jsonb_array_length(evidence) > 0，决策 16 禁幻觉①）**
- FK：message/snapshot/todo_candidate 的 customer_id -> crm.customer(id) ON DELETE
  CASCADE（级联清理，决策 22）；todo_candidate.engine_task_id 跨库不建 FK、
  confirmed_task_id 按 R23 走声明不建 FK（详设 §2.1）
- 索引：idx_crm_customer_status / idx_crm_customer_updated(updated_at DESC) /
  idx_crm_message_customer / idx_crm_snapshot_customer(customer_id, created_at DESC) /
  idx_crm_candidate_customer / idx_crm_candidate_status(status, created_at DESC)

修订号 0002（v0.3 首版）。迁移手写 DDL 与 models/crm.py 的 ORM 模型同源对齐
（字段/类型/默认值/约束/索引逐列一致，tests/test_crm_models.py 的 schema 测试
锁死）。schema crm 由部署侧建（承 v0.2 模式：schema tm 由部署侧建、迁移内不建，
详设 §2.1；本地开发/tests/conftest.py 模拟部署侧建库建 schema）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- crm.customer：客户表（§3.1；五态 + 三阶段 + 逾期语义字段）----
    op.create_table(
        "customer",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("nickname", sa.Text(), nullable=False),
        sa.Column(
            "source_shop", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "remark", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "follow_up_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'waiting_reply'"),
        ),
        sa.Column(
            "trade_stage",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pre_sale'"),
        ),
        sa.Column(
            "order_no", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("latest_summary", sa.Text(), nullable=True),
        sa.Column("last_contacted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("length(nickname) BETWEEN 1 AND 255"),
        sa.CheckConstraint(
            "follow_up_status IN "
            "('waiting_reply','replied','closed_deal','on_hold','archived')"
        ),
        sa.CheckConstraint("trade_stage IN ('pre_sale','in_sale','after_sale')"),
        schema="crm",
    )
    op.create_index(
        "idx_crm_customer_status", "customer", ["follow_up_status"], schema="crm"
    )
    op.create_index(
        "idx_crm_customer_updated",
        "customer",
        [sa.text("updated_at DESC")],
        schema="crm",
    )

    # ---- crm.message：对话消息表（§3.2；append-only，message_time 可空=粘贴不标）----
    op.create_table(
        "message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "customer_id",
            sa.BigInteger(),
            sa.ForeignKey("crm.customer.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column(
            "translated_text",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column(
            "language", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("message_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("direction IN ('buyer','seller')"),
        schema="crm",
    )
    op.create_index(
        "idx_crm_message_customer",
        "message",
        ["customer_id", "created_at"],
        schema="crm",
    )

    # ---- crm.snapshot：客户快照表（§3.3；滚动快照版本化，need_history 只增不减）----
    op.create_table(
        "snapshot",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "customer_id",
            sa.BigInteger(),
            sa.ForeignKey("crm.customer.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "current_need", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "need_history",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "sentiment", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "todos",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "summary", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="crm",
    )
    op.create_index(
        "idx_crm_snapshot_customer",
        "snapshot",
        ["customer_id", sa.text("created_at DESC")],
        schema="crm",
    )

    # ---- crm.todo_candidate：待办候选表（§3.4；evidence 非空 CHECK + 三态 + 追溯字段）----
    op.create_table(
        "todo_candidate",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "customer_id",
            sa.BigInteger(),
            sa.ForeignKey("crm.customer.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "reason", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "suggested_tags",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("evidence", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("engine_task_id", sa.BigInteger(), nullable=True),
        sa.Column("confirmed_task_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("length(content) BETWEEN 1 AND 200"),
        sa.CheckConstraint("status IN ('pending','confirmed','dismissed')"),
        sa.CheckConstraint(
            "jsonb_array_length(evidence) > 0", name="chk_candidate_evidence"
        ),
        schema="crm",
    )
    op.create_index(
        "idx_crm_candidate_customer",
        "todo_candidate",
        ["customer_id", "status"],
        schema="crm",
    )
    op.create_index(
        "idx_crm_candidate_status",
        "todo_candidate",
        ["status", sa.text("created_at DESC")],
        schema="crm",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_crm_candidate_status", table_name="todo_candidate", schema="crm"
    )
    op.drop_index(
        "idx_crm_candidate_customer", table_name="todo_candidate", schema="crm"
    )
    op.drop_table("todo_candidate", schema="crm")
    op.drop_index(
        "idx_crm_snapshot_customer", table_name="snapshot", schema="crm"
    )
    op.drop_table("snapshot", schema="crm")
    op.drop_index("idx_crm_message_customer", table_name="message", schema="crm")
    op.drop_table("message", schema="crm")
    op.drop_index(
        "idx_crm_customer_updated", table_name="customer", schema="crm"
    )
    op.drop_index("idx_crm_customer_status", table_name="customer", schema="crm")
    op.drop_table("customer", schema="crm")
