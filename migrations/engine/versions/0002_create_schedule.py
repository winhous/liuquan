"""引擎库迁移：schedule 表（v0.4 详设 §5）。

定时触发底座：chain_id / cron / enabled / last_run_at / next_run_at /
last_status（三值 CHECK + 空串默认）+ idx_schedule_enabled。

ORM 同源：engine/core/db.py Schedule 类逐列对齐。
revision=0002，down_revision=0001（引擎库初始迁移）。

Revision ID: 0002
Create Date: 2026-09-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedule",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chain_id", sa.Text(), nullable=False),
        sa.Column(
            "name",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "cron",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'0 7 * * *'"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
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
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 100",
            name="chk_schedule_name",
        ),
        sa.CheckConstraint(
            "last_status IN ('','done','failed','skipped')",
            name="chk_schedule_last_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_schedule_enabled", "schedule", ["enabled"])


def downgrade() -> None:
    op.drop_index("idx_schedule_enabled", table_name="schedule")
    op.drop_table("schedule")
