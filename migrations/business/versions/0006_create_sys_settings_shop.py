"""create sys schema + settings + shop tables（v0.4 批 1a/1b，详设 §3/§4）。

Revision ID: 0006
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS sys")

    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "key ~ '^[a-z][a-z0-9_.\\-]{0,63}$'",
            name="chk_setting_key_format",
        ),
        schema="sys",
    )

    op.create_table(
        "shop",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("remark", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 100", name="chk_shop_name"),
        schema="sys",
    )
    op.create_index("idx_shop_enabled", "shop", ["enabled"], schema="sys")


def downgrade() -> None:
    op.drop_index("idx_shop_enabled", schema="sys")
    op.drop_table("shop", schema="sys")
    op.drop_table("settings", schema="sys")
    op.execute("DROP SCHEMA IF EXISTS sys")
