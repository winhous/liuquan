"""scrape.link_record 增网盘三列（v0.6 批 6，详设-v0.6 §15.2 迁移 0012）。

用户复核反馈 F2/F3：夸克网盘同步上传（默认本地 + 可选同步上传）。链接文件夹
（§15.1 一链接一文件夹）是上传网盘的单位，上传状态/链接回填在 link_record：

- netdisk_status：网盘上传状态，默认 'none'，CHECK 四值
  （none 未上传 / pending 上传中 / uploaded 已上传 / failed 上传失败）
- netdisk_url：夸克永久分享链接（批 7 上传成功后回填）
- netdisk_uploaded_at：上传成功时间（TIMESTAMPTZ）

迁移号说明：0011 已占用（link_record 表 + image_file 扩展），本迁移为 0012
（down_revision="0011"，业务库迁移链 head = 0012）。
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- scrape.link_record 增网盘三列（夸克上传，批 7 消费者回填）----
    op.add_column(
        "link_record",
        sa.Column(
            "netdisk_status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'none'"),
        ),
        schema="scrape",
    )
    op.create_check_constraint(
        "chk_scrape_link_netdisk_status",
        "link_record",
        "netdisk_status IN ('none','pending','uploaded','failed')",
        schema="scrape",
    )
    op.add_column(
        "link_record",
        sa.Column("netdisk_url", sa.Text),
        schema="scrape",
    )
    op.add_column(
        "link_record",
        sa.Column("netdisk_uploaded_at", sa.DateTime(timezone=True)),
        schema="scrape",
    )


def downgrade() -> None:
    op.drop_column("link_record", "netdisk_uploaded_at", schema="scrape")
    op.drop_column("link_record", "netdisk_url", schema="scrape")
    op.drop_constraint(
        "chk_scrape_link_netdisk_status",
        "link_record",
        type_="check",
        schema="scrape",
    )
    op.drop_column("link_record", "netdisk_status", schema="scrape")
