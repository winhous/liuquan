"""创建 scrape.image_file 表（v0.5 批 4，详设-v0.5 §4.1）。

扒图产物表（batch_id × url 唯一，幂等防重）。
来源：xhs / xianyu / crm（子目录按来源分）。

复用 0008 模式（CREATE SCHEMA IF NOT EXISTS + CREATE TABLE + Index）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 创建 scrape schema
    op.execute("CREATE SCHEMA IF NOT EXISTS scrape")

    # 创建 scrape.image_file 表
    op.create_table(
        "image_file",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("local_path", sa.Text),
        sa.Column("day_dir", sa.Text),
        sa.Column("desc", sa.Text),
        sa.Column(
            "tags",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("author_id", sa.Text),
        sa.Column("width", sa.BigInteger),
        sa.Column("height", sa.BigInteger),
        sa.Column(
            "watermark",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "batch_id", "url", name="uq_image_file_batch_url"
        ),
        schema="scrape",
    )

    # 创建索引：source + created_at（列表查询常用）
    op.create_index(
        "idx_scrape_image_file_source_created",
        "image_file",
        ["source", "created_at"],
        schema="scrape",
    )


def downgrade() -> None:
    op.drop_index("idx_scrape_image_file_source_created", schema="scrape")
    op.drop_table("image_file", schema="scrape")
    op.execute("DROP SCHEMA IF EXISTS scrape")
