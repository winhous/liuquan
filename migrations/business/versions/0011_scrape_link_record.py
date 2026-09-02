"""创建 scrape.link_record 表 + 扩展 scrape.image_file（v0.6 批 1，详设-v0.6 §4.1/§4.2）。

新表 scrape.link_record（一个链接一条记录，素材库图集层雏形）：
- normalized_url 唯一（去 xsec_token 等易变 query 后的查重键，同作品不同 token 幂等）
- source ∈ {xhs, xianyu, http}；status ∈ {pending, downloading, done, failed}
- image_count 成功落库图片数；desc/tags/author_id 元数据；batch_id 批次
- storage_dir 落盘相对路径；error_note 失败原因；degraded_note 元数据降级原因

scrape.image_file 扩展（图片挂链接 + 来源标记 + SKU×店铺留位）：
- link_record_id FK → scrape.link_record(id) ON DELETE CASCADE（图片挂链接）
- source_mark 默认 'scraped'，CHECK 四值（scraped/selfshot/ai_generated/authorized）
- sku_id/shop_id 裸列 BIGINT（v0.7 SKU 建档启用，本版不加 FK）
- uq_scrape_image_link_url(link_record_id, url) 图片幂等键（同链接同 URL 只写一次）
- 保留原 uq_image_file_batch_url 不动（兼容存量）

迁移号说明：详设 §4 原写 0010，但 0010 已被 v0.5 的 0010_crm_message_image.py 占用
（业务库迁移链实际 head = 0010），本迁移为 0011（down_revision="0010"）——详设已
同步修正（变更日志 2026-09-03「详设迁移号修正 0010 → 0011」）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 新表 scrape.link_record（schema 已由 0009 建）----
    op.create_table(
        "link_record",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("url", sa.Text, nullable=False),          # 原始分享链接（含易变 query）
        sa.Column("normalized_url", sa.Text, nullable=False),  # 规范化链接（查重键）
        sa.Column("source", sa.Text, nullable=False),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "image_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "desc",
            sa.Text,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "tags",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("author_id", sa.Text),
        sa.Column("batch_id", sa.Text, nullable=False),
        sa.Column("storage_dir", sa.Text),
        sa.Column("error_note", sa.Text),
        sa.Column("degraded_note", sa.Text),
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
        sa.CheckConstraint(
            "source IN ('xhs','xianyu','http')",
            name="chk_scrape_link_source",
        ),
        sa.CheckConstraint(
            "status IN ('pending','downloading','done','failed')",
            name="chk_scrape_link_status",
        ),
        sa.UniqueConstraint(
            "normalized_url", name="uq_scrape_link_record_normalized_url"
        ),
        schema="scrape",
    )

    # 索引：状态+时间（列表常用）+ 批次
    op.create_index(
        "idx_scrape_link_status_created",
        "link_record",
        ["status", sa.text("created_at DESC")],
        schema="scrape",
    )
    op.create_index(
        "idx_scrape_link_batch",
        "link_record",
        ["batch_id"],
        schema="scrape",
    )

    # ---- 扩展 scrape.image_file（挂链接 + 来源标记 + SKU×店铺留位）----
    op.add_column(
        "image_file",
        sa.Column(
            "link_record_id",
            sa.BigInteger,
            sa.ForeignKey("scrape.link_record.id", ondelete="CASCADE"),
        ),
        schema="scrape",
    )
    op.add_column(
        "image_file",
        sa.Column(
            "source_mark",
            sa.Text,
            nullable=False,
            server_default=sa.text("'scraped'"),
        ),
        schema="scrape",
    )
    op.add_column("image_file", sa.Column("sku_id", sa.BigInteger), schema="scrape")
    op.add_column("image_file", sa.Column("shop_id", sa.BigInteger), schema="scrape")
    op.create_check_constraint(
        "chk_scrape_image_source_mark",
        "image_file",
        "source_mark IN ('scraped','selfshot','ai_generated','authorized')",
        schema="scrape",
    )
    # 图片幂等键（同链接同 URL 只写一次）；原 uq_image_file_batch_url 保留不动
    op.create_unique_constraint(
        "uq_scrape_image_link_url",
        "image_file",
        ["link_record_id", "url"],
        schema="scrape",
    )
    op.create_index(
        "idx_scrape_image_link",
        "image_file",
        ["link_record_id"],
        schema="scrape",
    )


def downgrade() -> None:
    op.drop_index("idx_scrape_image_link", table_name="image_file", schema="scrape")
    op.drop_constraint(
        "uq_scrape_image_link_url", "image_file", type_="unique", schema="scrape"
    )
    op.drop_constraint(
        "chk_scrape_image_source_mark", "image_file", type_="check", schema="scrape"
    )
    op.drop_column("image_file", "shop_id", schema="scrape")
    op.drop_column("image_file", "sku_id", schema="scrape")
    op.drop_column("image_file", "source_mark", schema="scrape")
    op.drop_column("image_file", "link_record_id", schema="scrape")
    op.drop_index("idx_scrape_link_batch", table_name="link_record", schema="scrape")
    op.drop_index(
        "idx_scrape_link_status_created", table_name="link_record", schema="scrape"
    )
    op.drop_table("link_record", schema="scrape")
