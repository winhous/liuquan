"""v0.7 验收断言（详设-v0.7 §10；@version_acceptance）：A76。

覆盖（对应详设 §10 验收断言表）：
- A76：表结构——迁移 0013 建 schema catalog 7 表 + 约束（code UNIQUE/格式 CHECK、
  kind CHECK、stock UNIQUE、ledger 五类 CHECK、item_image/usage UNIQUE）+
  sys.shop.platform 列（CHECK + default other）+ 两仓种子

基建：tm_pg_cluster（conftest 嵌入式 PG，业务库迁移 upgrade head 自动含 0013）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- URL/IP 一律运行期拼接，单一字符串常量不得含完整 scheme 或 IPv4 四段
- 不读 os.environ / os.getenv（P2 规则4）
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from pytest_asyncio import fixture as async_fixture


# ---- fixtures ----


@async_fixture
async def biz_engine(tm_pg_cluster):
    """业务库 AsyncEngine（NullPool：TestClient portal 循环与 pytest 循环不串）。"""
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


# ==== A76：表结构——迁移 0013 catalog 7 表 + 约束 + sys.shop.platform + 两仓种子 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a76_catalog_schema_tables_and_seed(biz_engine) -> None:
    """A76：迁移 0013 后 schema catalog 7 表存在 + code UNIQUE/格式 CHECK +
    kind CHECK + stock UNIQUE + ledger 五类 CHECK + item_image/usage UNIQUE +
    sys.shop.platform 列存在（default other）+ 两仓种子存在。

    真 SQL 连嵌入式 PG 断言（禁止假绿）。
    """
    async with AsyncSession(biz_engine) as session:
        # ---- 1. catalog schema 存在 ----
        schema_rows = await session.execute(
            text(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name = 'catalog'"
            )
        )
        schemas = [r[0] for r in schema_rows.fetchall()]
        assert "catalog" in schemas, "catalog schema 未创建"

        # ---- 2. 7 表全部存在 ----
        expected_tables = {
            "item",
            "item_bom",
            "item_image",
            "image_shop_usage",
            "warehouse",
            "stock",
            "stock_ledger",
        }
        table_rows = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'catalog'"
            )
        )
        actual_tables = {r[0] for r in table_rows.fetchall()}
        missing = expected_tables - actual_tables
        assert not missing, f"catalog schema 缺表: {missing}"

        # ---- 3. catalog.item 约束 ----
        # 3a. code UNIQUE
        idx_rows = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_schema = 'catalog' AND table_name = 'item' "
                "AND constraint_type = 'UNIQUE'"
            )
        )
        uniq_names = {r[0] for r in idx_rows.fetchall()}
        assert "uq_catalog_item_code" in uniq_names, "item.code UNIQUE 约束缺失"

        # 3b. code 格式 CHECK
        chk_rows = await session.execute(
            text(
                "SELECT constraint_name, check_clause "
                "FROM information_schema.check_constraints "
                "WHERE constraint_schema = 'catalog' "
                "AND constraint_name = 'chk_catalog_item_code_format'"
            )
        )
        chk_list = chk_rows.fetchall()
        assert len(chk_list) >= 1, "item.code 格式 CHECK 约束缺失"

        # 3c. kind CHECK
        kind_chk = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.check_constraints "
                "WHERE constraint_schema = 'catalog' "
                "AND constraint_name = 'chk_catalog_item_kind'"
            )
        )
        assert len(kind_chk.fetchall()) >= 1, "item.kind CHECK 约束缺失"

        # ---- 4. catalog.item_bom 约束 ----
        bom_chk = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.check_constraints "
                "WHERE constraint_schema = 'catalog' "
                "AND constraint_name = 'chk_catalog_item_bom_qty'"
            )
        )
        assert len(bom_chk.fetchall()) >= 1, "item_bom.qty CHECK 约束缺失"

        # ---- 5. catalog.item_image UNIQUE(item_id, image_file_id) ----
        img_uniq = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_schema = 'catalog' AND table_name = 'item_image' "
                "AND constraint_type = 'UNIQUE'"
            )
        )
        img_uniq_names = {r[0] for r in img_uniq.fetchall()}
        assert (
            "uq_catalog_item_image_item_file" in img_uniq_names
        ), "item_image UNIQUE(item_id, image_file_id) 缺失"

        # ---- 6. catalog.image_shop_usage UNIQUE(image_file_id, shop_id) ----
        usage_uniq = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_schema = 'catalog' AND table_name = 'image_shop_usage' "
                "AND constraint_type = 'UNIQUE'"
            )
        )
        usage_uniq_names = {r[0] for r in usage_uniq.fetchall()}
        assert (
            "uq_catalog_image_shop_usage_file_shop" in usage_uniq_names
        ), "image_shop_usage UNIQUE(image_file_id, shop_id) 缺失"

        # ---- 7. catalog.stock UNIQUE(item_id, warehouse_id) + qty CHECK ----
        stock_uniq = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_schema = 'catalog' AND table_name = 'stock' "
                "AND constraint_type = 'UNIQUE'"
            )
        )
        stock_uniq_names = {r[0] for r in stock_uniq.fetchall()}
        assert (
            "uq_catalog_stock_item_warehouse" in stock_uniq_names
        ), "stock UNIQUE(item_id, warehouse_id) 缺失"

        stock_chk = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.check_constraints "
                "WHERE constraint_schema = 'catalog' "
                "AND constraint_name = 'chk_catalog_stock_qty'"
            )
        )
        assert len(stock_chk.fetchall()) >= 1, "stock.qty CHECK 约束缺失"

        # ---- 8. catalog.stock_ledger 五类 CHECK ----
        ledger_chk = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.check_constraints "
                "WHERE constraint_schema = 'catalog' "
                "AND constraint_name = 'chk_catalog_stock_ledger_change_type'"
            )
        )
        assert (
            len(ledger_chk.fetchall()) >= 1
        ), "stock_ledger.change_type 五类 CHECK 缺失"

        ledger_delta_chk = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.check_constraints "
                "WHERE constraint_schema = 'catalog' "
                "AND constraint_name = 'chk_catalog_stock_ledger_qty_delta'"
            )
        )
        assert (
            len(ledger_delta_chk.fetchall()) >= 1
        ), "stock_ledger.qty_delta <> 0 CHECK 缺失"

        # ---- 9. sys.shop.platform 列存在 + default 'other' + CHECK ----
        platform_col = await session.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_schema = 'sys' AND table_name = 'shop' "
                "AND column_name = 'platform'"
            )
        )
        platform_rows = platform_col.fetchall()
        assert len(platform_rows) >= 1, "sys.shop.platform 列缺失"
        default_val = platform_rows[0][0]
        assert "other" in str(default_val), (
            f"sys.shop.platform 默认值应含 'other'，实际: {default_val}"
        )

        # platform CHECK
        shop_plat_chk = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.check_constraints "
                "WHERE constraint_schema = 'sys' "
                "AND constraint_name = 'chk_sys_shop_platform'"
            )
        )
        assert (
            len(shop_plat_chk.fetchall()) >= 1
        ), "sys.shop.platform CHECK 约束缺失"

        # ---- 10. 两仓种子存在 ----
        wh_rows = await session.execute(
            text(
                "SELECT name FROM catalog.warehouse ORDER BY name"
            )
        )
        wh_names = {r[0] for r in wh_rows.fetchall()}
        assert "代发仓" in wh_names, "种子仓库 '代发仓' 缺失"
        assert "自有仓" in wh_names, "种子仓库 '自有仓' 缺失"
