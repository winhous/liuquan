"""创建 catalog schema + 7 表（v0.7 批 1 数据地基，详设-v0.7 §3）。

catalog schema 七表：
- item：货档案（physical/combo/custom 三型；核心表）
- item_bom：combo 配方行（parent=combo，child=physical，qty）
- item_image：档案图库（item × scrape.image_file 引用，不复制文件）
- image_shop_usage：图店使用足迹（image_file × sys.shop，平台 join）
- warehouse：仓库（种子：代发仓 / 自有仓）
- stock：库存 = 实物档案 × 仓库
- stock_ledger：库存流水（sale/purchase/loss/adjust/return）

sys.shop 增 platform 列（店铺平台：etsy/xianyu/xhs/other）。

两仓种子（幂等 INSERT）：代发仓、自有仓。

迁移号说明：0012 已占用（scrape netdisk 三列），本迁移为 0013
（down_revision="0012"，业务库迁移链 head = 0013）。
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- CREATE SCHEMA catalog（若不存在）----
    op.execute("CREATE SCHEMA IF NOT EXISTS catalog")

    # ---- catalog.item（货档案，核心表）----
    op.create_table(
        "item",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("product_name", sa.Text),
        sa.Column(
            "kind",
            sa.Text,
            nullable=False,
        ),
        sa.Column("cost", sa.Numeric(12, 2)),
        sa.Column("supplier", sa.Text),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "remark",
            sa.Text,
            nullable=False,
            server_default=sa.text("''"),
        ),
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
        sa.UniqueConstraint("code", name="uq_catalog_item_code"),
        sa.CheckConstraint(
            "code ~ '^[A-Za-z0-9_-]{1,40}$'",
            name="chk_catalog_item_code_format",
        ),
        sa.CheckConstraint(
            "kind IN ('physical','combo','custom')",
            name="chk_catalog_item_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active','delisted')",
            name="chk_catalog_item_status",
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 120",
            name="chk_catalog_item_name",
        ),
        sa.CheckConstraint(
            "product_name IS NULL OR length(product_name) BETWEEN 1 AND 120",
            name="chk_catalog_item_product_name",
        ),
        schema="catalog",
    )
    op.create_index(
        "idx_catalog_item_product_name",
        "item",
        ["product_name"],
        schema="catalog",
    )
    op.create_index(
        "idx_catalog_item_kind",
        "item",
        ["kind"],
        schema="catalog",
    )

    # ---- catalog.item_bom（combo 配方行）----
    op.create_table(
        "item_bom",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "parent_item_id",
            sa.BigInteger,
            sa.ForeignKey("catalog.item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "child_item_id",
            sa.BigInteger,
            sa.ForeignKey("catalog.item.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "qty",
            sa.Numeric(10, 3),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "parent_item_id",
            "child_item_id",
            name="uq_catalog_item_bom_parent_child",
        ),
        sa.CheckConstraint("qty > 0", name="chk_catalog_item_bom_qty"),
        schema="catalog",
    )
    op.create_index(
        "idx_catalog_item_bom_child",
        "item_bom",
        ["child_item_id"],
        schema="catalog",
    )

    # ---- catalog.item_image（档案图库）----
    op.create_table(
        "item_image",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "item_id",
            sa.BigInteger,
            sa.ForeignKey("catalog.item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "image_file_id",
            sa.BigInteger,
            sa.ForeignKey("scrape.image_file.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "is_main",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "sort",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "item_id",
            "image_file_id",
            name="uq_catalog_item_image_item_file",
        ),
        schema="catalog",
    )
    op.create_index(
        "idx_catalog_item_image_file",
        "item_image",
        ["image_file_id"],
        schema="catalog",
    )

    # ---- catalog.image_shop_usage（图店使用足迹）----
    op.create_table(
        "image_shop_usage",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "image_file_id",
            sa.BigInteger,
            sa.ForeignKey("scrape.image_file.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "shop_id",
            sa.BigInteger,
            sa.ForeignKey("sys.shop.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "note",
            sa.Text,
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "image_file_id",
            "shop_id",
            name="uq_catalog_image_shop_usage_file_shop",
        ),
        schema="catalog",
    )
    op.create_index(
        "idx_catalog_image_shop_usage_shop",
        "image_shop_usage",
        ["shop_id"],
        schema="catalog",
    )

    # ---- catalog.warehouse（仓库）----
    op.create_table(
        "warehouse",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column(
            "remark",
            sa.Text,
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
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
            "length(name) BETWEEN 1 AND 60",
            name="chk_catalog_warehouse_name",
        ),
        schema="catalog",
    )

    # ---- 两仓种子（幂等 INSERT）----
    op.execute(
        """
        INSERT INTO catalog.warehouse (name, remark, enabled)
        VALUES ('代发仓', '供应商直发，不经手实物', true)
        ON CONFLICT (name) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO catalog.warehouse (name, remark, enabled)
        VALUES ('自有仓', '自己囤货仓库', true)
        ON CONFLICT (name) DO NOTHING
        """
    )

    # ---- catalog.stock（库存 = 实物档案 × 仓库）----
    op.create_table(
        "stock",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "item_id",
            sa.BigInteger,
            sa.ForeignKey("catalog.item.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "warehouse_id",
            sa.BigInteger,
            sa.ForeignKey("catalog.warehouse.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "qty",
            sa.Numeric(14, 3),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "item_id",
            "warehouse_id",
            name="uq_catalog_stock_item_warehouse",
        ),
        sa.CheckConstraint("qty >= 0", name="chk_catalog_stock_qty"),
        schema="catalog",
    )

    # ---- catalog.stock_ledger（库存流水）----
    op.create_table(
        "stock_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "item_id",
            sa.BigInteger,
            sa.ForeignKey("catalog.item.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "warehouse_id",
            sa.BigInteger,
            sa.ForeignKey("catalog.warehouse.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "change_type",
            sa.Text,
            nullable=False,
        ),
        sa.Column(
            "qty_delta",
            sa.Numeric(14, 3),
            nullable=False,
        ),
        sa.Column("before_qty", sa.Numeric(14, 3)),
        sa.Column("after_qty", sa.Numeric(14, 3)),
        sa.Column("ref_type", sa.Text),
        sa.Column("ref_id", sa.BigInteger),
        sa.Column(
            "operator",
            sa.Text,
            nullable=False,
            server_default=sa.text("'web'"),
        ),
        sa.Column(
            "note",
            sa.Text,
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "change_type IN ('sale','purchase','loss','adjust','return')",
            name="chk_catalog_stock_ledger_change_type",
        ),
        sa.CheckConstraint(
            "qty_delta <> 0",
            name="chk_catalog_stock_ledger_qty_delta",
        ),
        schema="catalog",
    )
    op.create_index(
        "idx_catalog_stock_ledger_item_created",
        "stock_ledger",
        ["item_id", sa.text("created_at")],
        schema="catalog",
    )
    op.create_index(
        "idx_catalog_stock_ledger_type_created",
        "stock_ledger",
        ["change_type", sa.text("created_at")],
        schema="catalog",
    )

    # ---- sys.shop 增 platform 列（详设 §3.7）----
    op.add_column(
        "shop",
        sa.Column(
            "platform",
            sa.Text,
            nullable=False,
            server_default=sa.text("'other'"),
        ),
        schema="sys",
    )
    op.create_check_constraint(
        "chk_sys_shop_platform",
        "shop",
        "platform IN ('etsy','xianyu','xhs','other')",
        schema="sys",
    )


def downgrade() -> None:
    # ---- sys.shop 删 platform 列 ----
    op.drop_constraint(
        "chk_sys_shop_platform", "shop", type_="check", schema="sys"
    )
    op.drop_column("shop", "platform", schema="sys")

    # ---- catalog.stock_ledger ----
    op.drop_index(
        "idx_catalog_stock_ledger_type_created",
        table_name="stock_ledger",
        schema="catalog",
    )
    op.drop_index(
        "idx_catalog_stock_ledger_item_created",
        table_name="stock_ledger",
        schema="catalog",
    )
    op.drop_table("stock_ledger", schema="catalog")

    # ---- catalog.stock ----
    op.drop_table("stock", schema="catalog")

    # ---- catalog.warehouse ----
    op.drop_table("warehouse", schema="catalog")

    # ---- catalog.image_shop_usage ----
    op.drop_index(
        "idx_catalog_image_shop_usage_shop",
        table_name="image_shop_usage",
        schema="catalog",
    )
    op.drop_table("image_shop_usage", schema="catalog")

    # ---- catalog.item_image ----
    op.drop_index(
        "idx_catalog_item_image_file",
        table_name="item_image",
        schema="catalog",
    )
    op.drop_table("item_image", schema="catalog")

    # ---- catalog.item_bom ----
    op.drop_index(
        "idx_catalog_item_bom_child",
        table_name="item_bom",
        schema="catalog",
    )
    op.drop_table("item_bom", schema="catalog")

    # ---- catalog.item ----
    op.drop_index(
        "idx_catalog_item_kind", table_name="item", schema="catalog"
    )
    op.drop_index(
        "idx_catalog_item_product_name", table_name="item", schema="catalog"
    )
    op.drop_table("item", schema="catalog")

    # ---- DROP SCHEMA catalog ----
    op.execute("DROP SCHEMA IF EXISTS catalog")
