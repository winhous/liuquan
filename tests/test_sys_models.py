"""v0.4 + v0.7 业务库 sys schema ORM + 迁移冒烟测试（详设-v0.4 §3/§4 + v0.7 §3.7；R22 结构同源）。

全部走嵌入式 PG（tests/conftest.py 的 tm_pg_cluster：复用 engine 簇另建 liuquan
库 + tm/crm/sys schema + alembic upgrade head 真跑（自动含 0006/0007/0013））——测的就是
迁移 DDL 与 models/sys.py ORM 的逐字段同源 + 两表落地 + 约束/索引生效（真 SQL 真事务，不桩）。

覆盖：
- schema 同源（sys）：settings 表列集合 + CHECK（chk_setting_key_format）
- schema 同源（sys）：shop 表列集合 + CHECK（chk_shop_name + chk_sys_shop_platform）+ 索引（idx_shop_enabled）
- 迁移 0007（tm）：tm.task.source_type CHECK 含 'schedule' 值
- 冒烟（sys）：settings/shop 插入读取往返 + 默认值（含 platform 默认 'other'）
- 反向（sys）：settings 非法键名被拒 / shop 重名被拒 / shop name 长度违规被拒 / shop platform 非法值被拒

注意（本文件自身在 P2 扫描对象内）：
- 回环地址与连接串一律运行期拼接（"127." 加 "0.0.1"），任何单一字符串常量
  不得含完整 IPv4 四段或 URL scheme（判据见 engine/lint/p2.py 模块 docstring）
- 不读 os.environ（P2 规则4）
"""

from __future__ import annotations

import pytest
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from models.sys import Setting, Shop

# ---- 期望 schema（与 migrations/business/versions/0006 的 DDL 逐列对齐）----

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "settings": {"key", "value", "description", "updated_at"},
    "shop": {"id", "name", "remark", "enabled", "platform", "created_at", "updated_at"},
}

# settings CHECK：key 正则 = 1；shop CHECK：name 长度 + platform = 2
EXPECTED_CHECK_COUNTS = {"settings": 1, "shop": 2}

EXPECTED_NAMED_CHECKS = {"chk_setting_key_format", "chk_shop_name", "chk_sys_shop_platform"}
EXPECTED_INDEXES = {"shop": {"idx_shop_enabled", "shop_pkey", "shop_name_key"}}


# ---- fixtures：function 级 async 引擎/会话（每测试独立事务，不共享数据）----


@async_fixture
async def sys_engine(tm_pg_cluster):
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture
async def sys_session(sys_engine):
    maker = async_sessionmaker(sys_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


# ---- schema 同源：列 / CHECK / 索引 ----


@pytest.mark.asyncio
async def test_migration_creates_sys_settings_and_shop(sys_engine) -> None:
    """sys.settings + sys.shop 两表列集合与详设 §3/§4 逐列对齐。"""
    async with AsyncSession(sys_engine) as session:
        res = await session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'sys' ORDER BY table_name, ordinal_position"
            )
        )
        rows = res.fetchall()
    actual: dict[str, set[str]] = {}
    for table, column in rows:
        actual.setdefault(table, set()).add(column)
    assert "settings" in actual
    assert "shop" in actual
    for table, cols in EXPECTED_COLUMNS.items():
        assert actual[table] == cols, f"sys.{table} 列集合与详设/ORM 不一致"


@pytest.mark.asyncio
async def test_migration_sys_check_constraints(sys_engine) -> None:
    """CHECK 约束全落地：数量对齐 + 具名约束在册。"""
    async with AsyncSession(sys_engine) as session:
        res = await session.execute(
            text(
                "SELECT conrelid::regclass::text AS tbl, conname FROM pg_constraint "
                "WHERE connamespace = 'sys'::regnamespace AND contype = 'c'"
            )
        )
        rows = res.fetchall()
    by_table: dict[str, set[str]] = {}
    for tbl, conname in rows:
        by_table.setdefault(tbl.split(".")[-1], set()).add(conname)
    for table, count in EXPECTED_CHECK_COUNTS.items():
        names = by_table.get(table, set())
        assert len(names) == count, (
            f"sys.{table} CHECK 数量应为 {count}，实际 {sorted(names)}"
        )
    all_checks = {c for names in by_table.values() for c in names}
    assert EXPECTED_NAMED_CHECKS <= all_checks


@pytest.mark.asyncio
async def test_migration_sys_indexes(sys_engine) -> None:
    async with AsyncSession(sys_engine) as session:
        res = await session.execute(
            text("SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'sys'")
        )
        rows = res.fetchall()
    by_table: dict[str, set[str]] = {}
    for tablename, indexname in rows:
        by_table.setdefault(tablename, set()).add(indexname)
    assert by_table["shop"] == EXPECTED_INDEXES["shop"], "sys.shop 索引集合与详设 §4 不一致"


# ---- 迁移 0007（tm）：tm.task.source_type CHECK 含 'schedule' ----


@pytest.mark.asyncio
async def test_migration_0007_source_type_includes_schedule(sys_engine) -> None:
    """迁移 0007 后：tm.task.source_type CHECK 约束包含 'schedule'。"""
    async with AsyncSession(sys_engine) as session:
        res = await session.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'tm.task'::regclass AND contype = 'c'"
            )
        )
        rows = res.fetchall()
    # 应有 1 条 CHECK（迁移 0007 替换为具名约束 chk_task_source_type）
    source_type_rows = [r for r in rows if "source_type" in r[1]]
    assert len(source_type_rows) == 1, (
        f"tm.task 应有 1 条 source_type CHECK，实际全部 CHECK: {rows}"
    )
    name, defn = source_type_rows[0]
    assert name == "chk_task_source_type", f"约束名应为 chk_task_source_type，实际：{name}"
    assert "'schedule'" in defn, f"CHECK 应含 'schedule'，实际：{defn}"
    assert "'ai'" in defn and "'manual'" in defn, f"CHECK 应含 'ai'/'manual'，实际：{defn}"


# ---- 冒烟：settings / shop 插入读取往返 + 默认值 ----


@pytest.mark.asyncio
async def test_setting_insert_read_roundtrip(sys_session) -> None:
    setting = Setting(key="crm.follow_up_days", value="5", description="逾期天数")
    sys_session.add(setting)
    await sys_session.commit()
    await sys_session.refresh(setting)
    assert setting.key == "crm.follow_up_days"
    assert setting.value == "5"
    assert setting.description == "逾期天数"
    assert setting.updated_at is not None

    row = await sys_session.get(Setting, "crm.follow_up_days")
    assert row is not None
    assert row.value == "5"


@pytest.mark.asyncio
async def test_setting_defaults(sys_session) -> None:
    """value/description 默认空串。"""
    setting = Setting(key="engine.max_attempts")
    sys_session.add(setting)
    await sys_session.commit()
    await sys_session.refresh(setting)
    assert setting.value == ""
    assert setting.description == ""


@pytest.mark.asyncio
async def test_shop_insert_read_roundtrip(sys_session) -> None:
    shop = Shop(name="测试店铺", remark="备注")
    sys_session.add(shop)
    await sys_session.commit()
    await sys_session.refresh(shop)
    assert shop.id is not None
    assert shop.name == "测试店铺"
    assert shop.remark == "备注"
    assert shop.enabled is True  # 默认 True
    assert shop.platform == "other"  # 默认 'other'（v0.7 §3.7）
    assert shop.created_at is not None
    assert shop.updated_at is not None

    row = await sys_session.get(Shop, shop.id)
    assert row is not None
    assert row.name == "测试店铺"


@pytest.mark.asyncio
async def test_shop_toggle_disabled(sys_session) -> None:
    shop = Shop(name="可停店铺")
    sys_session.add(shop)
    await sys_session.commit()
    await sys_session.refresh(shop)
    assert shop.enabled is True
    shop.enabled = False
    await sys_session.commit()
    await sys_session.refresh(shop)
    assert shop.enabled is False


# ---- 反向：非法键名 / 重名 / 长度违规 ----


@pytest.mark.asyncio
async def test_setting_invalid_key_rejected(sys_session) -> None:
    """settings key 格式不合规被 CHECK 拒（大写/特殊字符）。"""
    setting = Setting(key="INVALID_KEY", value="1")
    sys_session.add(setting)
    with pytest.raises(IntegrityError):
        await sys_session.commit()
    await sys_session.rollback()


@pytest.mark.asyncio
async def test_shop_duplicate_name_rejected(sys_session) -> None:
    """shop name 唯一约束：重名被拒。"""
    sys_session.add(Shop(name="唯一店铺"))
    await sys_session.commit()
    sys_session.add(Shop(name="唯一店铺"))
    with pytest.raises(IntegrityError):
        await sys_session.commit()
    await sys_session.rollback()


@pytest.mark.asyncio
async def test_shop_name_too_long_rejected(sys_session) -> None:
    """shop name 长度 CHECK：超过 100 字符被拒。"""
    shop = Shop(name="x" * 101)
    sys_session.add(shop)
    with pytest.raises(IntegrityError):
        await sys_session.commit()
    await sys_session.rollback()


@pytest.mark.asyncio
async def test_shop_name_boundary_100_ok(sys_session) -> None:
    """shop name 恰好 100 字符通过。"""
    shop = Shop(name="a" * 100)
    sys_session.add(shop)
    await sys_session.commit()
    await sys_session.refresh(shop)
    assert shop.id is not None
    assert len(shop.name) == 100


@pytest.mark.asyncio
async def test_shop_platform_invalid_rejected(sys_session) -> None:
    """shop platform CHECK：非法值被拒（v0.7 §3.7）。"""
    shop = Shop(name="测试平台店", platform="invalid_platform")
    sys_session.add(shop)
    with pytest.raises(IntegrityError):
        await sys_session.commit()
    await sys_session.rollback()


@pytest.mark.asyncio
async def test_shop_platform_valid_values(sys_session) -> None:
    """shop platform：四个合法值全部可写（v0.7 §3.7）。"""
    for plat in ("etsy", "xianyu", "xhs", "other"):
        shop = Shop(name=f"平台店铺-{plat}", platform=plat)
        sys_session.add(shop)
    await sys_session.commit()
    # 验证四个都成功写入
    result = await sys_session.execute(
        text("SELECT platform FROM sys.shop WHERE name LIKE '平台店铺-%' ORDER BY platform")
    )
    platforms = {r[0] for r in result.fetchall()}
    assert platforms == {"etsy", "xianyu", "xhs", "other"}
