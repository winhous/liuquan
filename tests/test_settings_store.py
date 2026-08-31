"""v0.4 批 1b SettingsStore 测试（嵌入式 PG，零网络，R12 桩注入）。

覆盖（详设 §3/§4/§7.3）：
- get 回退默认 / 类型解析（int/bool/time/str）
- 单键 set 原子性（其他 key 的 updated_at 不动）
- shop CRUD：增/改/启停/删 + 重名拦截

基建：tm_pg_cluster（含 sys schema，conftest 已建）+ NullPool AsyncEngine。
"""

from __future__ import annotations

from datetime import time

import pytest
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from web.settings_store import SettingsError, SettingsStore


@async_fixture
async def settings_engine(tm_pg_cluster):
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def store(settings_engine) -> SettingsStore:
    return SettingsStore(settings_engine)


@async_fixture(autouse=True)
async def _clean(settings_engine):
    yield
    async with AsyncSession(settings_engine) as session, session.begin():
        await session.execute(text("DELETE FROM sys.settings"))
        await session.execute(text("DELETE FROM sys.shop"))


# ---- settings get / set ----


@pytest.mark.asyncio
async def test_get_returns_default_when_key_missing(store: SettingsStore):
    val = await store.get("crm.follow_up_days", 5)
    assert val == 5


@pytest.mark.asyncio
async def test_get_returns_default_when_empty(store: SettingsStore):
    await store.set("crm.follow_up_days", "")
    val = await store.get("crm.follow_up_days", 5)
    # 空字符串解析 int 失败 -> 回退 default
    assert val == 5


@pytest.mark.asyncio
async def test_get_int_type(store: SettingsStore):
    await store.set("crm.follow_up_days", "7")
    val = await store.get("crm.follow_up_days", 5)
    assert val == 7
    assert isinstance(val, int)


@pytest.mark.asyncio
async def test_get_bool_type_true(store: SettingsStore):
    await store.set("notify.feishu_enabled", "true")
    val = await store.get("notify.feishu_enabled", True)
    assert val is True


@pytest.mark.asyncio
async def test_get_bool_type_false(store: SettingsStore):
    await store.set("notify.feishu_enabled", "false")
    val = await store.get("notify.feishu_enabled", True)
    assert val is False


@pytest.mark.asyncio
async def test_get_time_type(store: SettingsStore):
    await store.set("schedule.default_time", "07:00")
    val = await store.get("schedule.default_time", time(7, 0))
    assert val == time(7, 0)


@pytest.mark.asyncio
async def test_get_str_type(store: SettingsStore):
    await store.set("some.key", "hello")
    val = await store.get("some.key", "default")
    assert val == "hello"


@pytest.mark.asyncio
async def test_set_atomicity_other_keys_unchanged(store: SettingsStore):
    """单键 set 不动其他 key 的 updated_at（原子性）。"""
    await store.set("key.a", "1")
    await store.set("key.b", "2")
    # 读 key.a 的 updated_at
    async with store._maker() as session:
        row = (await session.execute(
            text("SELECT updated_at FROM sys.settings WHERE key = 'key.a'")
        )).one()
        ts_a = row[0]
    # 更新 key.b
    await store.set("key.b", "20")
    # key.a 的 updated_at 不变
    async with store._maker() as session:
        row = (await session.execute(
            text("SELECT updated_at FROM sys.settings WHERE key = 'key.a'")
        )).one()
        assert row[0] == ts_a
    # key.b 的值已更新
    assert await store.get("key.b") == "20"


@pytest.mark.asyncio
async def test_get_raw(store: SettingsStore):
    await store.set("crm.follow_up_days", "3")
    raw = await store.get_raw("crm.follow_up_days")
    assert raw == "3"


@pytest.mark.asyncio
async def test_get_raw_missing(store: SettingsStore):
    raw = await store.get_raw("nonexistent")
    assert raw is None


# ---- shop CRUD ----


@pytest.mark.asyncio
async def test_create_shop(store: SettingsStore):
    sid = await store.create_shop("花朵工厂", remark="主店铺")
    assert sid > 0
    shops = await store.list_shops()
    assert len(shops) == 1
    assert shops[0]["name"] == "花朵工厂"
    assert shops[0]["remark"] == "主店铺"
    assert shops[0]["enabled"] is True


@pytest.mark.asyncio
async def test_create_shop_duplicate_rejected(store: SettingsStore):
    await store.create_shop("花朵工厂")
    with pytest.raises(SettingsError, match="已存在同名"):
        await store.create_shop("花朵工厂")


@pytest.mark.asyncio
async def test_create_shop_duplicate_case_insensitive(store: SettingsStore):
    await store.create_shop("Flowers")
    with pytest.raises(SettingsError, match="已存在同名"):
        await store.create_shop("flowers")


@pytest.mark.asyncio
async def test_update_shop(store: SettingsStore):
    sid = await store.create_shop("原始名")
    await store.update_shop(sid, "新名", remark="更新")
    shops = await store.list_shops()
    assert shops[0]["name"] == "新名"
    assert shops[0]["remark"] == "更新"


@pytest.mark.asyncio
async def test_update_shop_duplicate_name_rejected(store: SettingsStore):
    sid1 = await store.create_shop("花朵A")
    await store.create_shop("花朵B")
    with pytest.raises(SettingsError, match="已存在同名"):
        await store.update_shop(sid1, "花朵B")


@pytest.mark.asyncio
async def test_toggle_shop(store: SettingsStore):
    sid = await store.create_shop("花朵")
    enabled = await store.toggle_shop(sid)
    assert enabled is False
    shops = await store.list_shops()
    assert len(shops) == 0  # 默认只列启用
    all_shops = await store.list_shops(include_disabled=True)
    assert len(all_shops) == 1
    assert all_shops[0]["enabled"] is False


@pytest.mark.asyncio
async def test_delete_shop(store: SettingsStore):
    sid = await store.create_shop("花朵")
    await store.delete_shop(sid)
    shops = await store.list_shops()
    assert len(shops) == 0


@pytest.mark.asyncio
async def test_list_shops_default_only_enabled(store: SettingsStore):
    sid = await store.create_shop("启用店铺")
    await store.create_shop("停用店铺")
    await store.toggle_shop(sid + 1)
    shops = await store.list_shops()
    assert len(shops) == 1
    assert shops[0]["name"] == "启用店铺"
