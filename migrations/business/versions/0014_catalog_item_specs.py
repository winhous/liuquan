"""catalog.item 增 product_code 和 specs 列（v0.7 复核修订，详设-v0.7 §16.3）。

product_code TEXT：商品英文代号（同 product_name 组共享）。
specs JSONB：规格属性（{属性名: 属性值}），默认 '{}'。

迁移号说明：0013 已占用（catalog 7 表），本迁移为 0014
（down_revision="0013"，业务库迁移链 head = 0014）。
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- catalog.item 增 product_code 列 ----
    op.add_column(
        "item",
        sa.Column("product_code", sa.Text),
        schema="catalog",
    )

    # ---- catalog.item 增 specs 列（JSONB，非空，默认空对象）----
    op.add_column(
        "item",
        sa.Column(
            "specs",
            sa.JSON,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema="catalog",
    )


def downgrade() -> None:
    # ---- catalog.item 删 specs 列 ----
    op.drop_column("item", "specs", schema="catalog")

    # ---- catalog.item 删 product_code 列 ----
    op.drop_column("item", "product_code", schema="catalog")