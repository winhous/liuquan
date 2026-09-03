"""业务库 ORM（database: liuquan, schema: catalog）—— 7 表。

货档案体系（详设-v0.7 §3：SKU/货档案建档物料层地基）：
- item：货档案（physical/combo/custom 三型；核心表）
- item_bom：combo 配方行（parent=combo，child=physical，qty）
- item_image：档案图库（item × scrape.image_file 引用，不复制文件）
- image_shop_usage：图店使用足迹（image_file × sys.shop，平台 join）
- warehouse：仓库（种子：代发仓 / 自有仓）
- stock：库存 = 实物档案 × 仓库
- stock_ledger：库存流水（sale/purchase/loss/adjust/return）

复用 models/tm.py 的 TmBase（业务库 declarative base，R22）。
migrations/business/versions/0013 创建 catalog schema + 七表（详设 §3）。

⚠ 跨 schema FK 注册要求（详设 §3.8）：
- item_image.image_file_id → scrape.image_file.id
- image_shop_usage.shop_id → sys.shop.id
是本项目首批跨 schema FK。SQLAlchemy 解析要求目标表已注册进 TmBase.metadata。
因此顶部 import sys/scrape 副作用注册 FK 目标表。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from models.tm import TmBase

# ---- 跨 schema FK 目标表注册（import 副作用，详设 §3.8 ⚠）----
from models import sys as _sys_reg  # noqa: E402,F401  （注册 sys.shop 进 TmBase.metadata）
from models import scrape as _scrape_reg  # noqa: E402,F401  （注册 scrape.image_file 进 TmBase.metadata）


class Item(TmBase):
    """catalog.item：货档案（详设-v0.7 §3.2，核心表）。

    physical 实物（有库存）/ combo 组合（配方，无库存）/ custom 定制（占位，无库存）。
    code 全库唯一 + 格式校验；kind CHECK 三值。
    """

    __tablename__ = "item"
    __table_args__ = (
        UniqueConstraint("code", name="uq_catalog_item_code"),
        CheckConstraint(
            "code ~ '^[A-Za-z0-9_-]{1,40}$'",
            name="chk_catalog_item_code_format",
        ),
        CheckConstraint(
            "kind IN ('physical','combo','custom')",
            name="chk_catalog_item_kind",
        ),
        CheckConstraint(
            "status IN ('active','delisted')",
            name="chk_catalog_item_status",
        ),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 120",
            name="chk_catalog_item_name",
        ),
        CheckConstraint(
            "product_name IS NULL OR length(product_name) BETWEEN 1 AND 120",
            name="chk_catalog_item_product_name",
        ),
        Index("idx_catalog_item_product_name", "product_name"),
        Index("idx_catalog_item_kind", "kind"),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(Text, nullable=False)  # 编号（Etsy SKU 框值）
    name: Mapped[str] = mapped_column(Text, nullable=False)  # 档名
    product_name: Mapped[str | None] = mapped_column(Text)  # 商品名归类文本（可空）
    product_code: Mapped[str | None] = mapped_column(Text)  # 商品英文代号（§16.2）
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # physical/combo/custom
    cost: Mapped[float | None] = mapped_column(Numeric(12, 2))  # 成本（可空）
    supplier: Mapped[str | None] = mapped_column(Text)  # 采购来源
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )  # active/delisted
    remark: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    specs: Mapped[dict] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::jsonb")
    )  # 规格属性（§16.3）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ItemBom(TmBase):
    """catalog.item_bom：combo 配方行（详设-v0.7 §3.3）。

    parent=combo 档，child=physical 档，qty>0。
    UNIQUE(parent_item_id, child_item_id)；反查索引 child_item_id。
    """

    __tablename__ = "item_bom"
    __table_args__ = (
        UniqueConstraint(
            "parent_item_id",
            "child_item_id",
            name="uq_catalog_item_bom_parent_child",
        ),
        CheckConstraint("qty > 0", name="chk_catalog_item_bom_qty"),
        Index("idx_catalog_item_bom_child", "child_item_id"),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    parent_item_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("catalog.item.id", ondelete="CASCADE"),
        nullable=False,
    )
    child_item_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("catalog.item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    qty: Mapped[float] = mapped_column(
        Numeric(10, 3), nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ItemImage(TmBase):
    """catalog.item_image：档案图库（详设-v0.7 §3.4）。

    item × scrape.image_file 引用（不复制文件）；一张图可被多档案引用。
    UNIQUE(item_id, image_file_id)；is_main 主图标记。
    """

    __tablename__ = "item_image"
    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "image_file_id",
            name="uq_catalog_item_image_item_file",
        ),
        Index("idx_catalog_item_image_file", "image_file_id"),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("catalog.item.id", ondelete="CASCADE"),
        nullable=False,
    )
    image_file_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("scrape.image_file.id", ondelete="RESTRICT"),
        nullable=False,
    )
    is_main: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    sort: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ImageShopUsage(TmBase):
    """catalog.image_shop_usage：图店使用足迹（详设-v0.7 §3.5）。

    image_file × sys.shop；同图同店只记一次（UNIQUE）。
    登记 = 人工动作（v0.7 手工上架后登记）。
    """

    __tablename__ = "image_shop_usage"
    __table_args__ = (
        UniqueConstraint(
            "image_file_id",
            "shop_id",
            name="uq_catalog_image_shop_usage_file_shop",
        ),
        Index("idx_catalog_image_shop_usage_shop", "shop_id"),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    image_file_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("scrape.image_file.id", ondelete="RESTRICT"),
        nullable=False,
    )
    shop_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys.shop.id", ondelete="RESTRICT"),
        nullable=False,
    )
    note: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Warehouse(TmBase):
    """catalog.warehouse：仓库（详设-v0.7 §3.6）。

    种子数据：代发仓 / 自有仓（迁移 0013 幂等 INSERT）。
    """

    __tablename__ = "warehouse"
    __table_args__ = (
        UniqueConstraint("name", name="uq_catalog_warehouse_name"),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 60",
            name="chk_catalog_warehouse_name",
        ),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    remark: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Stock(TmBase):
    """catalog.stock：库存 = 实物档案 × 仓库（详设-v0.7 §3.6）。

    只 physical 有行（接口校验）；combo/custom 无行。
    UNIQUE(item_id, warehouse_id)；qty >= 0。
    """

    __tablename__ = "stock"
    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "warehouse_id",
            name="uq_catalog_stock_item_warehouse",
        ),
        CheckConstraint("qty >= 0", name="chk_catalog_stock_qty"),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("catalog.item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    warehouse_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("catalog.warehouse.id", ondelete="RESTRICT"),
        nullable=False,
    )
    qty: Mapped[float] = mapped_column(
        Numeric(14, 3), nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StockLedger(TmBase):
    """catalog.stock_ledger：库存流水（详设-v0.7 §3.6）。

    五类必记：sale/purchase/loss/adjust/return。
    正入负出；before_qty/after_qty 快照追溯。
    """

    __tablename__ = "stock_ledger"
    __table_args__ = (
        CheckConstraint(
            "change_type IN ('sale','purchase','loss','adjust','return')",
            name="chk_catalog_stock_ledger_change_type",
        ),
        CheckConstraint(
            "qty_delta <> 0",
            name="chk_catalog_stock_ledger_qty_delta",
        ),
        Index(
            "idx_catalog_stock_ledger_item_created",
            "item_id",
            "created_at",
        ),
        Index(
            "idx_catalog_stock_ledger_type_created",
            "change_type",
            "created_at",
        ),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("catalog.item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    warehouse_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("catalog.warehouse.id", ondelete="RESTRICT"),
        nullable=False,
    )
    change_type: Mapped[str] = mapped_column(Text, nullable=False)
    qty_delta: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    before_qty: Mapped[float | None] = mapped_column(Numeric(14, 3))
    after_qty: Mapped[float | None] = mapped_column(Numeric(14, 3))
    ref_type: Mapped[str | None] = mapped_column(Text)
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    operator: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'web'")
    )
    note: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "Item",
    "ItemBom",
    "ItemImage",
    "ImageShopUsage",
    "Warehouse",
    "Stock",
    "StockLedger",
]
