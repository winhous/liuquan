"""创建 crm.message_image 表（v0.5 批 5，详设-v0.5 §4.1）。

CRM 对话图片表（message_id FK + url + status + ocr_text）。
状态：pending → downloaded/failed/skipped

复用 0009 模式（CREATE TABLE + Index + CHECK）。
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 创建 crm.message_image 表
    op.create_table(
        "message_image",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "message_id",
            sa.BigInteger,
            sa.ForeignKey("crm.message.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("local_path", sa.Text),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("width", sa.BigInteger),
        sa.Column("height", sa.BigInteger),
        sa.Column("ocr_text", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending','downloaded','failed','skipped')",
            name="chk_message_image_status",
        ),
        schema="crm",
    )

    # 创建索引：message_id + created_at（列表查询常用）
    op.create_index(
        "idx_crm_message_image_message",
        "message_image",
        ["message_id", "created_at"],
        schema="crm",
    )


def downgrade() -> None:
    op.drop_index("idx_crm_message_image_message", schema="crm")
    op.drop_table("message_image", schema="crm")