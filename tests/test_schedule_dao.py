"""v0.4 引擎库 schedule 表 DAO 全函数测试（详设-v0.4 §5/§9.1；R22 结构同源）。

全部走嵌入式 PG（tests/conftest.py 的 engine_pg_cluster：独立临时簇 +
alembic upgrade head 真跑（自动含 0002））——测的就是真 SQL 真事务，不桩。

覆盖：
- schema 同源（schedule）：列集合 + CHECK（chk_schedule_name + chk_schedule_last_status）
  + 索引（idx_schedule_enabled）
- DAO 往返：list_schedules / get_schedule / create_schedule / update_schedule / mark_schedule_run
- ensure_seed_schedules：空表插种子（幂等：非空不插）
- next_run_time：纯代码计算（同天晚于 after / 跨天 / after 恰为整点取下一个）

注意（本文件自身在 P2 扫描对象内）：
- 回环地址与连接串一律运行期拼接（"127." 加 "0.0.1"），任何单一字符串常量
  不得含完整 IPv4 四段或 URL scheme（判据见 engine/lint/p2.py 模块 docstring）
- 不读 os.environ（P2 规则4）
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from engine.core.db import (
    Schedule,
    create_engine,
    create_schedule,
    dispose_engine,
    ensure_seed_schedules,
    get_schedule,
    list_schedules,
    mark_schedule_run,
    next_run_time,
    update_schedule,
)


# ---- fixture：AsyncEngine（function 级，事件循环内建）+ 每测试 TRUNCATE 隔离 ----


@async_fixture
async def db_engine(engine_pg_cluster):
    engine = create_engine(engine_pg_cluster.url)
    yield engine
    await dispose_engine(engine)


@async_fixture(autouse=True)
async def _clean_schedule(db_engine):
    """每测试后 TRUNCATE schedule 表，互不污染。"""
    yield
    async with AsyncSession(db_engine) as session, session.begin():
        await session.execute(text("TRUNCATE schedule RESTART IDENTITY CASCADE"))


# ---- schema 同源：列 / CHECK / 索引 ----


@pytest.mark.asyncio
async def test_migration_creates_schedule_table(db_engine) -> None:
    """schedule 表列集合与迁移 0002 + ORM 逐列对齐。"""
    EXPECTED_COLUMNS = {
        "id", "chain_id", "name", "cron", "enabled",
        "last_run_at", "next_run_at", "last_status",
        "created_at", "updated_at",
    }
    async with AsyncSession(db_engine) as session:
        res = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'schedule' "
                "ORDER BY ordinal_position"
            )
        )
        cols = {row[0] for row in res.fetchall()}
    assert cols == EXPECTED_COLUMNS, f"schedule 列集合不一致：{cols}"


@pytest.mark.asyncio
async def test_migration_schedule_check_constraints(db_engine) -> None:
    """CHECK 约束全落地：chk_schedule_name + chk_schedule_last_status。"""
    async with AsyncSession(db_engine) as session:
        res = await session.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'schedule'::regclass AND contype = 'c'"
            )
        )
        names = {row[0] for row in res.fetchall()}
    assert "chk_schedule_name" in names, f"缺 chk_schedule_name，实际：{names}"
    assert "chk_schedule_last_status" in names, f"缺 chk_schedule_last_status，实际：{names}"


@pytest.mark.asyncio
async def test_migration_schedule_index(db_engine) -> None:
    """idx_schedule_enabled 索引存在。"""
    async with AsyncSession(db_engine) as session:
        res = await session.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'schedule' AND indexname = 'idx_schedule_enabled'"
            )
        )
        assert res.fetchone() is not None, "idx_schedule_enabled 索引缺失"


# ---- DAO：list_schedules / get_schedule / create_schedule ----


@pytest.mark.asyncio
async def test_list_schedules_empty(db_engine) -> None:
    rows = await list_schedules(db_engine)
    assert rows == []


@pytest.mark.asyncio
async def test_create_schedule_roundtrip(db_engine) -> None:
    sid = await create_schedule(
        db_engine,
        chain_id="test_chain",
        name="测试链",
        cron="30 8 * * *",
        enabled=True,
    )
    assert isinstance(sid, int)
    row = await get_schedule(db_engine, sid)
    assert row is not None
    assert row.id == sid
    assert row.chain_id == "test_chain"
    assert row.name == "测试链"
    assert row.cron == "30 8 * * *"
    assert row.enabled is True
    assert row.last_run_at is None
    assert row.next_run_at is None
    assert row.last_status == ""  # DDL 默认空串
    assert row.created_at is not None
    assert row.updated_at is not None


@pytest.mark.asyncio
async def test_get_schedule_missing_returns_none(db_engine) -> None:
    assert await get_schedule(db_engine, 999999) is None


@pytest.mark.asyncio
async def test_list_schedules_insertion_order(db_engine) -> None:
    s1 = await create_schedule(db_engine, chain_id="a", name="链A")
    s2 = await create_schedule(db_engine, chain_id="b", name="链B")
    rows = await list_schedules(db_engine)
    assert [r.id for r in rows] == [s1, s2]


# ---- DAO：update_schedule（只更新传入字段）----


@pytest.mark.asyncio
async def test_update_schedule_name_only(db_engine) -> None:
    sid = await create_schedule(db_engine, chain_id="c", name="旧名", cron="0 7 * * *")
    await update_schedule(db_engine, sid, name="新名")
    row = await get_schedule(db_engine, sid)
    assert row is not None
    assert row.name == "新名"
    assert row.cron == "0 7 * * *"  # 未传 cron，不变


@pytest.mark.asyncio
async def test_update_schedule_cron_only(db_engine) -> None:
    sid = await create_schedule(db_engine, chain_id="d", name="链D")
    await update_schedule(db_engine, sid, cron="0 8 * * *")
    row = await get_schedule(db_engine, sid)
    assert row is not None
    assert row.cron == "0 8 * * *"
    assert row.name == "链D"  # 未传 name，不变


@pytest.mark.asyncio
async def test_update_schedule_enabled_only(db_engine) -> None:
    sid = await create_schedule(db_engine, chain_id="e", name="链E", enabled=True)
    await update_schedule(db_engine, sid, enabled=False)
    row = await get_schedule(db_engine, sid)
    assert row is not None
    assert row.enabled is False


@pytest.mark.asyncio
async def test_update_schedule_nothing_noop(db_engine) -> None:
    """不传任何字段 = 不更新。"""
    sid = await create_schedule(db_engine, chain_id="f", name="链F")
    row_before = await get_schedule(db_engine, sid)
    await update_schedule(db_engine, sid)  # 无参数
    row_after = await get_schedule(db_engine, sid)
    assert row_before is not None and row_after is not None
    assert row_before.updated_at == row_after.updated_at


# ---- DAO：mark_schedule_run（锚点推进 + last_status 清空）----


@pytest.mark.asyncio
async def test_mark_schedule_run(db_engine) -> None:
    sid = await create_schedule(db_engine, chain_id="g", name="链G")
    last = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)
    nxt = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)
    await mark_schedule_run(db_engine, sid, last_run_at=last, next_run_at=nxt)
    row = await get_schedule(db_engine, sid)
    assert row is not None
    assert row.last_run_at == last
    assert row.next_run_at == nxt
    assert row.last_status == ""  # 清空


@pytest.mark.asyncio
async def test_mark_schedule_run_clears_last_status(db_engine) -> None:
    """mark_schedule_run 会把 last_status 清空。"""
    sid = await create_schedule(db_engine, chain_id="h", name="链H")
    # 先手动写入一个非空 last_status
    await update_schedule(db_engine, sid, name="链H")  # 确保有数据
    async with AsyncSession(db_engine) as session, session.begin():
        await session.execute(
            text("UPDATE schedule SET last_status = 'done' WHERE id = :id"),
            {"id": sid},
        )
    last = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)
    nxt = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)
    await mark_schedule_run(db_engine, sid, last_run_at=last, next_run_at=nxt)
    row = await get_schedule(db_engine, sid)
    assert row is not None
    assert row.last_status == ""


# ---- 反向：CHECK 约束拦截 ----


@pytest.mark.asyncio
async def test_schedule_name_too_long_rejected(db_engine) -> None:
    """name 超过 100 字符被 chk_schedule_name 拒。"""
    with pytest.raises(IntegrityError):
        await create_schedule(db_engine, chain_id="x", name="n" * 101)


@pytest.mark.asyncio
async def test_schedule_name_boundary_100_ok(db_engine) -> None:
    """name 恰好 100 字符通过。"""
    sid = await create_schedule(db_engine, chain_id="y", name="a" * 100)
    row = await get_schedule(db_engine, sid)
    assert row is not None
    assert len(row.name) == 100


# ---- ensure_seed_schedules：幂等种子 ----


@pytest.mark.asyncio
async def test_ensure_seed_empty_table_inserts(db_engine) -> None:
    """空表 → 插入 crm_reminder_chain + seo_healthcheck_chain + scrape_download_chain 种子。"""
    await ensure_seed_schedules(db_engine)
    rows = await list_schedules(db_engine)
    assert len(rows) == 3
    chain_ids = {r.chain_id for r in rows}
    assert "crm_reminder_chain" in chain_ids
    assert "seo_healthcheck_chain" in chain_ids
    assert "scrape_download_chain" in chain_ids  # v0.6 批 4：定时扒图种子
    crm_seed = next(r for r in rows if r.chain_id == "crm_reminder_chain")
    assert crm_seed.name == "CRM 未跟进提醒"
    assert crm_seed.cron == "0 7 * * *"
    assert crm_seed.enabled is True
    hc_seed = next(r for r in rows if r.chain_id == "seo_healthcheck_chain")
    assert hc_seed.name == "listing 体检"
    assert hc_seed.cron == "0 8 * * *"
    assert hc_seed.enabled is True
    scrape_seed = next(r for r in rows if r.chain_id == "scrape_download_chain")
    assert scrape_seed.name == "定时扒图"
    assert scrape_seed.cron == "0 7 * * *"
    assert scrape_seed.enabled is True


@pytest.mark.asyncio
async def test_ensure_seed_cron_override(db_engine) -> None:
    """v0.6 §5.4：cron_overrides 覆盖 scrape_download_chain 种子 cron（引擎启动读设置）。"""
    await ensure_seed_schedules(
        db_engine, cron_overrides={"scrape_download_chain": "30 8 * * *"}
    )
    rows = await list_schedules(db_engine)
    scrape_seed = next(r for r in rows if r.chain_id == "scrape_download_chain")
    assert scrape_seed.cron == "30 8 * * *"
    crm_seed = next(r for r in rows if r.chain_id == "crm_reminder_chain")
    assert crm_seed.cron == "0 7 * * *"  # 既有种子不被覆盖


@pytest.mark.asyncio
async def test_ensure_seed_nonempty_adds_missing(db_engine) -> None:
    """非空表但缺新链 → per-chain 幂等补插。"""
    await create_schedule(
        db_engine, chain_id="other_chain", name="已有链"
    )
    await ensure_seed_schedules(db_engine)
    rows = await list_schedules(db_engine)
    chain_ids = {r.chain_id for r in rows}
    # other_chain 保留，crm_reminder_chain + seo_healthcheck_chain + scrape_download_chain 补插
    assert "other_chain" in chain_ids
    assert "crm_reminder_chain" in chain_ids
    assert "seo_healthcheck_chain" in chain_ids
    assert "scrape_download_chain" in chain_ids
    assert len(rows) == 4


@pytest.mark.asyncio
async def test_ensure_seed_idempotent(db_engine) -> None:
    """连续两次调用幂等（per-chain 幂等：chain_id 已存在不重复插）。"""
    await ensure_seed_schedules(db_engine)
    await ensure_seed_schedules(db_engine)
    rows = await list_schedules(db_engine)
    assert len(rows) == 3
    chain_ids = {r.chain_id for r in rows}
    assert "crm_reminder_chain" in chain_ids
    assert "seo_healthcheck_chain" in chain_ids
    assert "scrape_download_chain" in chain_ids


@pytest.mark.asyncio
async def test_ensure_seed_partial_existing(db_engine) -> None:
    """已有 crm_reminder_chain 但缺其余 → 只补缺的。"""
    await create_schedule(
        db_engine, chain_id="crm_reminder_chain", name="已有提醒链", cron="0 7 * * *"
    )
    await ensure_seed_schedules(db_engine)
    rows = await list_schedules(db_engine)
    assert len(rows) == 3
    chain_ids = {r.chain_id for r in rows}
    assert "crm_reminder_chain" in chain_ids
    assert "seo_healthcheck_chain" in chain_ids
    assert "scrape_download_chain" in chain_ids


# ---- next_run_time：纯代码计算 ----


def test_next_run_time_same_day_after() -> None:
    """after 在当天 HH:MM 之前 → 返回当天 HH:MM。"""
    after = datetime(2026, 9, 1, 6, 30, tzinfo=timezone.utc)
    result = next_run_time("0 7 * * *", after)
    assert result == datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)


def test_next_run_time_same_day_exact_boundary() -> None:
    """after 正好等于 HH:MM → 取下一个（次日）。"""
    after = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)
    result = next_run_time("0 7 * * *", after)
    assert result == datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)


def test_next_run_time_same_day_past() -> None:
    """after 在当天 HH:MM 之后 → 返回次日 HH:MM。"""
    after = datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc)
    result = next_run_time("0 7 * * *", after)
    assert result == datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)


def test_next_run_time_cross_month() -> None:
    """跨月：8 月 31 日 08:00 → 9 月 1 日 07:00。"""
    after = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)
    result = next_run_time("0 7 * * *", after)
    assert result == datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)


def test_next_run_time_custom_cron() -> None:
    """自定义 cron：每天 23:59。"""
    after = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    result = next_run_time("59 23 * * *", after)
    assert result == datetime(2026, 9, 1, 23, 59, tzinfo=timezone.utc)


def test_next_run_time_custom_cron_after_midnight() -> None:
    """after 在 23:59 之后 → 返回次日 23:59。"""
    after = datetime(2026, 9, 1, 23, 59, tzinfo=timezone.utc)
    result = next_run_time("59 23 * * *", after)
    assert result == datetime(2026, 9, 2, 23, 59, tzinfo=timezone.utc)


def test_next_run_time_just_before_boundary() -> None:
    """after 在 06:59:59 → 返回 07:00。"""
    after = datetime(2026, 9, 1, 6, 59, 59, tzinfo=timezone.utc)
    result = next_run_time("0 7 * * *", after)
    assert result == datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)


# ---- next_run_time 错误输入 ----


def test_next_run_time_invalid_segments() -> None:
    with pytest.raises(ValueError, match="5 段"):
        next_run_time("0 7 *", datetime(2026, 9, 1, tzinfo=timezone.utc))


def test_next_run_time_non_daily_rejected() -> None:
    """非 daily（第 2 段非 *）被拒。"""
    with pytest.raises(ValueError, match="daily"):
        next_run_time("0 7 1 * *", datetime(2026, 9, 1, tzinfo=timezone.utc))


def test_next_run_time_non_numeric_rejected() -> None:
    with pytest.raises(ValueError, match="数字"):
        next_run_time("x 7 * * *", datetime(2026, 9, 1, tzinfo=timezone.utc))


def test_next_run_time_hour_out_of_range() -> None:
    with pytest.raises(ValueError, match="越界"):
        next_run_time("0 25 * * *", datetime(2026, 9, 1, tzinfo=timezone.utc))
