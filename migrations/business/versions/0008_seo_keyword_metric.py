"""创建 seo.keyword_metric 表（v0.5 批 2，详设-v0.5 §4.1）。

SEO 关键词历史指标表（keyword × metric_date 唯一，幂等 409 防重）。
体检历史 + 变化检测数据源（listing_healthcheck 比较用）。

复用 0006 模式（CREATE SCHEMA IF NOT EXISTS + CREATE TABLE + Index）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 创建 seo schema
    op.execute("CREATE SCHEMA IF NOT EXISTS seo")

    # 创建 seo.keyword_metric 表
    op.create_table(
        "keyword_metric",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("keyword", sa.Text, nullable=False),
        sa.Column("metric_date", sa.Date, nullable=False),
        sa.Column("product_num", sa.BigInteger),
        sa.Column("avg_price_top", sa.Numeric(10, 2)),
        sa.Column(
            "top_competitors",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("metrics", JSONB),
        sa.Column("quota", JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "keyword", "metric_date", name="uq_keyword_metric_date"
        ),
        schema="seo",
    )

    # 创建索引
    op.create_index(
        "idx_seo_keyword_metric_kw",
        "keyword_metric",
        ["keyword", "metric_date"],
        schema="seo",
    )


def downgrade() -> None:
    op.drop_index("idx_seo_keyword_metric_kw", schema="seo")
    op.drop_table("keyword_metric", schema="seo")
    op.execute("DROP SCHEMA IF EXISTS seo")
