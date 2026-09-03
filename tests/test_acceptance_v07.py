"""v0.7 验收断言（详设-v0.7 §10；@version_acceptance）：A76-A83/A89。

覆盖（对应详设 §10 验收断言表）：
- A76：表结构——迁移 0013 建 schema catalog 7 表 + 约束（code UNIQUE/格式 CHECK、
  kind CHECK、stock UNIQUE、ledger 五类 CHECK、item_image/usage UNIQUE）+
  sys.shop.platform 列（CHECK + default other）+ 两仓种子
- A77：手工建档 physical（商品名=打火机 + 红色 LTR-RED cost 12.5）→ item 落库 active +
  字段正确；再加蓝档成功
- A78：编号校验——非法编号（逗号/斜杠/空格/中文/超长）→ 4xx；重复编号 → 409 不落库；
  合法通过；code 必填
- A79：同商品同名档冲突：product_name=打火机 + name=红色 建两次 → 409
- A80：combo 建档 + 配方——先建红机/精装盒 physical → 建 combo 精装红（BOM 红机×1+
  盒×1）→ 配方可见；child 非 physical → 4xx；BOM 挂 physical 档 → 4xx；
  combo 写库存 → 409
- A81：custom 建档 → 无库存 + 成本可空；custom 记库存 409；custom 挂 BOM 409
- A82：库存记账+流水（核心）：purchase +100→stock=100+ledger（before 0/after 100）；
  loss -3→97+ledger；超扣→4xx且无半条流水；combo/custom 记数 409；sale→501
- A83：库存不影响建档：无库存行也建档通过（接单采购模式）
- A89：无图建档成功 + code 用 ERP 风格编号（如 GLCA00001 格式通过）

基建：tm_pg_cluster（conftest 嵌入式 PG，业务库迁移 upgrade head 自动含 0013）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- URL/IP 一律运行期拼接，单一字符串常量不得含完整 scheme 或 IPv4 四段
- 不读 os.environ / os.getenv（P2 规则4）
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
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


# ==== A77：手工建档 physical（商品名=打火机 + 红色 LTR-RED cost 12.5）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a77_manual_create_items(biz_engine) -> None:
    """A77：手工建档 physical（详设 §4.1）。

    POST items（商品名=打火机 + 档 红色 LTR-RED cost 12.5）→ item 落库 active +
    code/name/product_name/kind/cost 正确；同商品再加蓝档成功。
    """
    from web.catalog_service import create_items, CatalogServiceError

    async with AsyncSession(biz_engine) as session, session.begin():
        # 建红色 physical 档
        ids = await create_items(
            session,
            product_name="A77打火机",
            rows=[{
                "name": "A77红色",
                "kind": "physical",
                "code": "A77-LTR-RED",
                "cost": 12.5,
            }],
        )
        assert len(ids) == 1
        red_id = ids[0]

    # 验证落库
    async with AsyncSession(biz_engine) as session:
        item = await session.get(
            __import__("models.catalog", fromlist=["Item"]).Item, red_id
        )
        assert item is not None
        assert item.code == "A77-LTR-RED"
        assert item.name == "A77红色"
        assert item.product_name == "A77打火机"
        assert item.kind == "physical"
        assert float(item.cost) == 12.5
        assert item.status == "active"

    # 再加蓝档（同商品名）
    async with AsyncSession(biz_engine) as session, session.begin():
        ids2 = await create_items(
            session,
            product_name="A77打火机",
            rows=[{
                "name": "A77蓝色",
                "kind": "physical",
                "code": "A77-LTR-BLUE",
                "cost": 13.0,
            }],
        )
        assert len(ids2) == 1

    # 验证蓝档
    async with AsyncSession(biz_engine) as session:
        blue = await session.get(
            __import__("models.catalog", fromlist=["Item"]).Item, ids2[0]
        )
        assert blue is not None
        assert blue.code == "A77-LTR-BLUE"
        assert blue.name == "A77蓝色"
        assert blue.product_name == "A77打火机"
        assert blue.kind == "physical"
        assert float(blue.cost) == 13.0


# ==== A78：编号校验——非法编号/重复编号/合法通过/code 必填 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a78_code_format_and_uniqueness(biz_engine) -> None:
    """A78：编号校验（详设 §8.4-1）。

    非法编号（逗号/斜杠/空格/中文/超长）→ 4xx；重复编号 → 409 不落库；
    合法通过；code 必填。
    """
    from web.catalog_service import create_items, CatalogServiceError

    # --- 非法编号测试 ---
    bad_codes = [
        ("LTR,RED", "逗号"),
        ("LTR/RED", "斜杠"),
        ("LTR RED", "空格"),
        ("红机RED", "中文"),
        ("A" * 41, "超长（41字符）"),
    ]
    for bad_code, reason in bad_codes:
        async with AsyncSession(biz_engine) as session, session.begin():
            with pytest.raises(CatalogServiceError, match="编号"):
                await create_items(
                    session,
                    product_name="测试",
                    rows=[{"name": "测试", "kind": "physical", "code": bad_code}],
                )

    # --- code 必填 ---
    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(CatalogServiceError, match="必填"):
            await create_items(
                session,
                product_name="测试",
                rows=[{"name": "测试", "kind": "physical", "code": ""}],
            )

    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(CatalogServiceError, match="必填"):
            await create_items(
                session,
                product_name="测试",
                rows=[{"name": "测试", "kind": "physical", "code": None}],
            )

    # --- 重复编号 409 ---
    async with AsyncSession(biz_engine) as session, session.begin():
        await create_items(
            session,
            product_name="测试",
            rows=[{"name": "测试A", "kind": "physical", "code": "DUP-CODE-001"}],
        )
    # 相同 code 再建 → 409
    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(CatalogServiceError, match="已被占用"):
            await create_items(
                session,
                product_name="测试",
                rows=[{"name": "测试B", "kind": "physical", "code": "DUP-CODE-001"}],
            )
    # 验证不落库
    async with AsyncSession(biz_engine) as session:
        from models.catalog import Item
        dup_check = await session.execute(
            select(func.count()).select_from(Item).where(Item.code == "DUP-CODE-001")
        )
        assert dup_check.scalar_one() == 1  # 只有第一条

    # --- 合法编号通过 ---
    good_codes = ["A78-LTR", "A78_combo", "A78", "a78-b-c_123", "A78GLCA1"]
    for i, good_code in enumerate(good_codes):
        async with AsyncSession(biz_engine) as session, session.begin():
            ids = await create_items(
                session,
                product_name="合法测试A78",
                rows=[{
                    "name": f"合法A78-{i}",
                    "kind": "physical",
                    "code": good_code,
                }],
            )
            assert len(ids) == 1


# ==== A79：同商品同名档冲突 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a79_dup_name_in_product(biz_engine) -> None:
    """A79：同商品名 + 同名档 → 409（详设 §8.4-2 + §15②）。

    product_name=打火机 + name=红色 建两次 → 409。
    """
    from web.catalog_service import create_items, CatalogServiceError

    # 建第一次（用独立 product_name 避免与 A77 冲突）
    async with AsyncSession(biz_engine) as session, session.begin():
        ids = await create_items(
            session,
            product_name="DupTest品",
            rows=[{"name": "红色", "kind": "physical", "code": "DUP-NAME-001"}],
        )
        assert len(ids) == 1

    # 建第二次（同 product_name + 同 name）→ 409
    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(CatalogServiceError, match="同名档案"):
            await create_items(
                session,
                product_name="DupTest品",
                rows=[{"name": "红色", "kind": "physical", "code": "DUP-NAME-002"}],
            )

    # 无商品名下同名也冲突
    async with AsyncSession(biz_engine) as session, session.begin():
        await create_items(
            session,
            product_name=None,
            rows=[{"name": "DupTest通用件", "kind": "physical", "code": "DUP-NAME-003"}],
        )
    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(CatalogServiceError, match="同名档案"):
            await create_items(
                session,
                product_name=None,
                rows=[{"name": "DupTest通用件", "kind": "physical", "code": "DUP-NAME-004"}],
            )


# ==== A80：combo 建档 + 配方 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a80_combo_bom_and_no_stock(biz_engine) -> None:
    """A80：combo 建档 + 配方（详设 §8.4-3/§2.1）。

    先建红机/精装盒 physical → 建 combo 精装红（BOM 红机×1+盒×1）→
    配方可见；child 非 physical → 4xx；BOM 挂 physical 档 → 4xx；
    combo 写库存 → 409。
    """
    from web.catalog_service import create_items, CatalogServiceError
    from models.catalog import ItemBom, Item, Stock
    from sqlalchemy import select as sel

    # 建两个 physical 子件
    async with AsyncSession(biz_engine) as session, session.begin():
        ids = await create_items(
            session,
            product_name="A80打火机",
            rows=[
                {"name": "A80红机", "kind": "physical", "code": "A80-RED-MACH"},
                {"name": "A80精装盒", "kind": "physical", "code": "A80-BOX"},
            ],
        )
        red_mach_id, box_id = ids

    # 建 combo 精装红（带 BOM）
    async with AsyncSession(biz_engine) as session, session.begin():
        combo_ids = await create_items(
            session,
            product_name="A80打火机",
            rows=[{
                "name": "A80精装红",
                "kind": "combo",
                "code": "A80-COMBO-REDBOX",
                "bom": [
                    {"child_item_id": red_mach_id, "qty": 1},
                    {"child_item_id": box_id, "qty": 1},
                ],
            }],
        )
        combo_id = combo_ids[0]

    # 验证 combo 档案 + 配方可见
    async with AsyncSession(biz_engine) as session:
        combo = await session.get(Item, combo_id)
        assert combo is not None
        assert combo.kind == "combo"
        assert combo.status == "active"

        bom_rows = (
            await session.execute(
                sel(ItemBom).where(ItemBom.parent_item_id == combo_id)
            )
        ).scalars().all()
        assert len(bom_rows) == 2
        child_ids_in_bom = {b.child_item_id for b in bom_rows}
        assert red_mach_id in child_ids_in_bom
        assert box_id in child_ids_in_bom

    # --- child 非 physical → 4xx ---
    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(CatalogServiceError, match="必须是 physical"):
            await create_items(
                session,
                product_name="测试",
                rows=[{
                    "name": "错误combo",
                    "kind": "combo",
                    "code": "ERR-COMBO-001",
                    "bom": [
                        {"child_item_id": combo_id, "qty": 1},  # combo 引 combo
                    ],
                }],
            )

    # --- BOM 挂 physical 档 → 4xx ---
    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(CatalogServiceError, match="只有 combo"):
            await create_items(
                session,
                product_name="测试",
                rows=[{
                    "name": "错误物理",
                    "kind": "physical",
                    "code": "ERR-PHYS-001",
                    "bom": [{"child_item_id": red_mach_id, "qty": 1}],
                }],
            )

    # --- combo 写库存 → 409（service 层校验：combo 无库存行）---
    # 在 stock 表直接尝试插入 combo 行 → DB 层不会拦（接口层由 inventory service 校验）
    # 这里用 service 层的逻辑验证：combo 建档后 stock 表中无该 item 的行
    async with AsyncSession(biz_engine) as session:
        stock_rows = (
            await session.execute(
                sel(func.count()).select_from(Stock).where(Stock.item_id == combo_id)
            )
        ).scalar_one()
        assert stock_rows == 0, "combo 建档后不应有 stock 行"

    # 补充说明：库存操作的 combo 409 拦截由批 3 inventory service 实现；
    # 本批 service 层可验证的点：建档不建 stock 行 + combo 无库存入口


# ==== A81：custom 档案 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a81_custom_item_no_stock(biz_engine) -> None:
    """A81：custom 档案（详设 §2.1 + §8.4-4）。

    kind=custom 建档 → 无库存 + 成本可空；custom 挂 BOM → 409；
    custom 记库存 → 409（service 层校验）。
    """
    from web.catalog_service import create_items, CatalogServiceError
    from models.catalog import Item, Stock
    from sqlalchemy import select as sel

    # 建 custom 档案（成本可空）
    async with AsyncSession(biz_engine) as session, session.begin():
        ids = await create_items(
            session,
            product_name="A81定制花束",
            rows=[{
                "name": "A81定制花束A",
                "kind": "custom",
                "code": "A81-CUSTOM-BQ-001",
                "cost": None,
            }],
        )
        custom_id = ids[0]

    # 验证 custom 档案
    async with AsyncSession(biz_engine) as session:
        item = await session.get(Item, custom_id)
        assert item is not None
        assert item.kind == "custom"
        assert item.cost is None
        assert item.status == "active"

    # 验证无 stock 行
    async with AsyncSession(biz_engine) as session:
        stock_count = (
            await session.execute(
                sel(func.count()).select_from(Stock).where(Stock.item_id == custom_id)
            )
        ).scalar_one()
        assert stock_count == 0

    # --- custom 挂 BOM → 409 ---
    async with AsyncSession(biz_engine) as session, session.begin():
        # 先建一个 physical 用来引用
        phys_ids = await create_items(
            session,
            product_name="A81测试",
            rows=[{"name": "A81测试件", "kind": "physical", "code": "A81-PHYS-001"}],
        )
        phys_id = phys_ids[0]

    # custom 带 BOM 建档 → 拒绝
    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(CatalogServiceError, match="custom 档案不允许有配方行"):
            await create_items(
                session,
                product_name="测试",
                rows=[{
                    "name": "错误custom",
                    "kind": "custom",
                    "code": "CUSTOM-ERR-001",
                    "bom": [{"child_item_id": phys_id, "qty": 1}],
                }],
            )

    # --- custom 记库存 → 409（service 层：combo/custom 无库存行）---
    # 同 A80 说明：库存操作的 409 由批 3 inventory service 实现；
    # 本批验证：custom 建档后 stock 表无该 item 的行
    async with AsyncSession(biz_engine) as session:
        stock_count = (
            await session.execute(
                sel(func.count()).select_from(Stock).where(Stock.item_id == custom_id)
            )
        ).scalar_one()
        assert stock_count == 0, "custom 建档后不应有 stock 行"


# ==== A89：存量接缝——无图建档 + ERP 风格编号 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a89_legacy_friendly_create_no_image(biz_engine) -> None:
    """A89：存量接缝（详设 §1.4 + §4.1）。

    无图建档成功 + code 直接用 ERP 将来编号（格式通过即可）→ 导入通道可行。
    """
    from web.catalog_service import create_items
    from models.catalog import Item, ItemImage
    from sqlalchemy import select as sel

    # ERP 风格编号：字母前缀 + 数字补零
    erp_codes = [
        ("GLCA00001", "physical"),  # 火机类
        ("GLCB00023", "physical"),  # 盒类
        ("CUSTOM-0001", "custom"),  # 定制占位
    ]

    for i, (code, kind) in enumerate(erp_codes):
        async with AsyncSession(biz_engine) as session, session.begin():
            ids = await create_items(
                session,
                product_name=f"ERP测试{i}",
                rows=[{
                    "name": f"ERP档{i}",
                    "kind": kind,
                    "code": code,
                }],
            )
            assert len(ids) == 1

    # 验证全部建档成功且无图关联
    async with AsyncSession(biz_engine) as session:
        for code, _kind in erp_codes:
            item = (
                await session.execute(
                    sel(Item).where(Item.code == code)
                )
            ).scalar_one_or_none()
            assert item is not None, f"ERP 编号 {code} 应成功建档"
            assert item.status == "active"

            # 无图
            img_count = (
                await session.execute(
                    sel(func.count()).select_from(ItemImage).where(
                        ItemImage.item_id == item.id
                    )
                )
            ).scalar_one()
            assert img_count == 0, f"ERP 档案 {code} 应无图"


# ==== A82：库存记账 + 流水（核心）（详设 §5.1/§5.2 + §8.4 第 5 条）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a82_stock_ledger_transactional(biz_engine) -> None:
    """A82：库存记账 + 流水（核心，详设 §5.1/§5.2 + §8.4 第 5 条）。

    - 入库 purchase +100 → stock=100 + ledger（before 0/after 100）
    - 报损 loss -3 → stock=97 + ledger
    - 超扣（loss -200）→ 4xx 且无半条流水（事务原子性）
    - combo 记库存 → 409
    - custom 记库存 → 409
    - sale → 501
    """
    from web.catalog_service import create_items, CatalogServiceError
    from web.inventory_service import write_ledger, InventoryServiceError
    from models.catalog import Stock, StockLedger, Warehouse

    # ---- 准备：建 physical 档案 + 获取仓库 ----
    async with AsyncSession(biz_engine) as session, session.begin():
        phys_ids = await create_items(
            session,
            product_name="A82库存测试",
            rows=[{"name": "A82打火机", "kind": "physical", "code": "A82-PHYS-001"}],
        )
        phys_id = phys_ids[0]

    # 获取种子仓库（代发仓）
    async with AsyncSession(biz_engine) as session:
        wh = (
            await session.execute(
                select(Warehouse).where(Warehouse.enabled == True)  # noqa: E712
            )
        ).scalars().first()
        assert wh is not None, "种子仓库不存在"
        warehouse_id = wh.id

    # ---- 入库 purchase +100 → stock=100 ----
    async with AsyncSession(biz_engine) as session, session.begin():
        result = await write_ledger(
            session,
            item_id=phys_id,
            warehouse_id=warehouse_id,
            change_type="purchase",
            qty=100,
            note="A82 入库测试",
        )
        assert result["before_qty"] == 0.0
        assert result["after_qty"] == 100.0

    # 验证 stock 行
    async with AsyncSession(biz_engine) as session:
        stock = (
            await session.execute(
                select(Stock).where(
                    Stock.item_id == phys_id,
                    Stock.warehouse_id == warehouse_id,
                )
            )
        ).scalar_one_or_none()
        assert stock is not None
        assert float(stock.qty) == 100.0

    # 验证 ledger 行
    async with AsyncSession(biz_engine) as session:
        ledger_rows = (
            await session.execute(
                select(StockLedger).where(
                    StockLedger.item_id == phys_id,
                    StockLedger.warehouse_id == warehouse_id,
                    StockLedger.change_type == "purchase",
                )
            )
        ).scalars().all()
        assert len(ledger_rows) == 1
        assert float(ledger_rows[0].qty_delta) == 100.0
        assert float(ledger_rows[0].before_qty) == 0.0
        assert float(ledger_rows[0].after_qty) == 100.0
        assert ledger_rows[0].note == "A82 入库测试"

    # ---- 报损 loss -3 → stock=97 ----
    async with AsyncSession(biz_engine) as session, session.begin():
        result = await write_ledger(
            session,
            item_id=phys_id,
            warehouse_id=warehouse_id,
            change_type="loss",
            qty=3,
            note="A82 报损测试",
        )
        assert result["before_qty"] == 100.0
        assert result["after_qty"] == 97.0

    # 验证 stock = 97
    async with AsyncSession(biz_engine) as session:
        stock = (
            await session.execute(
                select(Stock).where(
                    Stock.item_id == phys_id,
                    Stock.warehouse_id == warehouse_id,
                )
            )
        ).scalar_one()
        assert float(stock.qty) == 97.0

    # 验证 loss ledger 行
    async with AsyncSession(biz_engine) as session:
        loss_ledger = (
            await session.execute(
                select(StockLedger).where(
                    StockLedger.item_id == phys_id,
                    StockLedger.change_type == "loss",
                )
            )
        ).scalars().all()
        assert len(loss_ledger) == 1
        assert float(loss_ledger[0].qty_delta) == -3.0
        assert float(loss_ledger[0].before_qty) == 100.0
        assert float(loss_ledger[0].after_qty) == 97.0

    # ---- 超扣（loss -200）→ 4xx 且无半条流水 ----
    # 记录当前 ledger 数量
    async with AsyncSession(biz_engine) as session:
        ledger_count_before = (
            await session.execute(
                select(func.count()).select_from(StockLedger).where(
                    StockLedger.item_id == phys_id
                )
            )
        ).scalar_one()

    # 超扣应抛异常
    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(InventoryServiceError, match="库存不足"):
            await write_ledger(
                session,
                item_id=phys_id,
                warehouse_id=warehouse_id,
                change_type="loss",
                qty=200,
            )

    # 验证：stock 未变（仍为 97），ledger 未增（事务回滚）
    async with AsyncSession(biz_engine) as session:
        stock = (
            await session.execute(
                select(Stock).where(
                    Stock.item_id == phys_id,
                    Stock.warehouse_id == warehouse_id,
                )
            )
        ).scalar_one()
        assert float(stock.qty) == 97.0, "超扣后 stock 应不变"

        ledger_count_after = (
            await session.execute(
                select(func.count()).select_from(StockLedger).where(
                    StockLedger.item_id == phys_id
                )
            )
        ).scalar_one()
        assert ledger_count_after == ledger_count_before, "超扣不应写入任何流水"

    # ---- combo 记库存 → 409 ----
    async with AsyncSession(biz_engine) as session, session.begin():
        combo_ids = await create_items(
            session,
            product_name="A82combo测试",
            rows=[{
                "name": "A82combo",
                "kind": "combo",
                "code": "A82-COMBO-001",
                "bom": [{"child_item_id": phys_id, "qty": 1}],
            }],
        )
        combo_id = combo_ids[0]

    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(InventoryServiceError, match="只有 physical"):
            await write_ledger(
                session,
                item_id=combo_id,
                warehouse_id=warehouse_id,
                change_type="purchase",
                qty=10,
            )

    # ---- custom 记库存 → 409 ----
    async with AsyncSession(biz_engine) as session, session.begin():
        custom_ids = await create_items(
            session,
            product_name="A82custom测试",
            rows=[{"name": "A82custom", "kind": "custom", "code": "A82-CUSTOM-001"}],
        )
        custom_id = custom_ids[0]

    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(InventoryServiceError, match="只有 physical"):
            await write_ledger(
                session,
                item_id=custom_id,
                warehouse_id=warehouse_id,
                change_type="purchase",
                qty=10,
            )

    # ---- sale → 501 ----
    async with AsyncSession(biz_engine) as session, session.begin():
        with pytest.raises(InventoryServiceError, match="501"):
            await write_ledger(
                session,
                item_id=phys_id,
                warehouse_id=warehouse_id,
                change_type="sale",
                qty=1,
            )


# ==== A83：库存不影响建档（详设 §5.1 接单采购模式）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a83_stock_not_gate_create(biz_engine) -> None:
    """A83：库存不影响建档（详设 §5.1 接单采购模式）。

    无库存行也建档通过——建档不读库存。
    """
    from web.catalog_service import create_items
    from models.catalog import Item, Stock
    from sqlalchemy import select as sel

    # 建档 physical，不写库存
    async with AsyncSession(biz_engine) as session, session.begin():
        ids = await create_items(
            session,
            product_name="A83无库存建档",
            rows=[{
                "name": "A83新品",
                "kind": "physical",
                "code": "A83-PHYS-001",
                "cost": 5.0,
            }],
        )
        item_id = ids[0]

    # 验证建档成功
    async with AsyncSession(biz_engine) as session:
        item = await session.get(Item, item_id)
        assert item is not None
        assert item.kind == "physical"
        assert item.status == "active"

        # 验证无 stock 行（建档不创建库存行）
        stock_count = (
            await session.execute(
                sel(func.count()).select_from(Stock).where(Stock.item_id == item_id)
            )
        ).scalar_one()
        assert stock_count == 0, "建档不应自动创建 stock 行"

    # 再建一个 custom（更极端：永远不会有库存）
    async with AsyncSession(biz_engine) as session, session.begin():
        ids = await create_items(
            session,
            product_name="A83无库存建档",
            rows=[{
                "name": "A83定制",
                "kind": "custom",
                "code": "A83-CUSTOM-001",
            }],
        )
        custom_id = ids[0]

    async with AsyncSession(biz_engine) as session:
        item = await session.get(Item, custom_id)
        assert item is not None
        assert item.kind == "custom"
        assert item.status == "active"
