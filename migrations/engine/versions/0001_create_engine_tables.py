"""引擎库初始迁移：4 张表（详设-v0.1 §3 DDL 逐列对齐，含全部索引与部分索引）。

engine_task / engine_step / engine_checkpoint / engine_audit
- BIGSERIAL PK、TEXT、INT、BIGINT、JSONB、TIMESTAMPTZ 与 §3 一一对应
- idx_task_status 为部分索引（WHERE status IN (...)，§3 原文）
- idx_ckpt_task 为 (task_id, id DESC)（恢复定位取最后一条）

修订号 0001（v0.1 首版）。迁移手写 DDL 与 engine/core/db.py 的 ORM 模型同源对齐
（字段/类型/默认值/索引逐列一致，tests/test_db.py 的 schema 测试锁死）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- engine_task：一次触发 = 一条，一条 = 一条链的完整执行 ----
    op.create_table(
        "engine_task",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chain_id", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("trigger_type", sa.Text(), nullable=False),
        sa.Column("trigger_ref", sa.Text(), nullable=True),
        sa.Column("input", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "current_step",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("'0'"),
        ),
        sa.Column("current_step_row", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_task_status",
        "engine_task",
        ["status"],
        postgresql_where=sa.text("status IN ('queued','running','paused')"),
    )

    # ---- engine_step：工序实例，每次执行一条（重试 = 新行，attempt+1）----
    op.create_table(
        "engine_step",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("engine_task.id"),
            nullable=False,
        ),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column(
            "phase",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'INIT'"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column("input", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("output", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column(
            "attempt",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("'0'"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_step_task", "engine_step", ["task_id", "step_index"]
    )

    # ---- engine_checkpoint：相位转换前落盘，崩溃/暂停恢复的依据 ----
    op.create_table(
        "engine_checkpoint",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("engine_task.id"),
            nullable=False,
        ),
        sa.Column(
            "step_id",
            sa.BigInteger(),
            sa.ForeignKey("engine_step.id"),
            nullable=False,
        ),
        sa.Column("from_phase", sa.Text(), nullable=False),
        sa.Column("to_phase", sa.Text(), nullable=False),
        sa.Column("state", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_ckpt_task",
        "engine_checkpoint",
        ["task_id", sa.text("id DESC")],
    )

    # ---- engine_audit：每次 LLM 调用一行（ok/reask/failed 都记）----
    op.create_table(
        "engine_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("engine_task.id"),
            nullable=False,
        ),
        sa.Column(
            "step_id",
            sa.BigInteger(),
            sa.ForeignKey("engine_step.id"),
            nullable=False,
        ),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "attempt",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("'0'"),
        ),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("input_full", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("output_full", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_audit_task", "engine_audit", ["task_id"])
    op.create_index(
        "idx_audit_worker_day", "engine_audit", ["worker_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_audit_worker_day", table_name="engine_audit")
    op.drop_index("idx_audit_task", table_name="engine_audit")
    op.drop_table("engine_audit")
    op.drop_index("idx_ckpt_task", table_name="engine_checkpoint")
    op.drop_table("engine_checkpoint")
    op.drop_index("idx_step_task", table_name="engine_step")
    op.drop_table("engine_step")
    op.drop_index("idx_task_status", table_name="engine_task")
    op.drop_table("engine_task")
