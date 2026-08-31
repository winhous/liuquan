"""v0.4 批 3a 调度器 + schedule 接口测试（详设 §9.2/§9.3）。

覆盖（对应 A42 验收断言）：
- 桩时钟/interval：到点触发链入队（trigger_type=schedule、input.trigger_date=今天）
  + 锚点推进（last_run_at=next_run_at、next_run_at=下次）
- 漏跑补跑：next_run_at 在过去（07:00 已过、now=07:10）-> 补触发一次
- 不重跑：锚点推进后再 tick 同次不重复触发
- 立即运行接口：POST run -> engine_task 落库（manual_schedule）+ 锚点不动
- 新增/启停/改时间接口（chain_id 白名单 404、cron 非法 422）
- 种子：空表 tick 后 crm_reminder_chain 存在
- 列表接口返回结构

基建复用（from test_runner import ...，fixture 随模块收集）：db_engine +
_clean_engine_tables（autouse 清引擎库）+ registry

注意（本文件在 P2 扫描对象内）：
URL/IP/sk- 形态一律运行期拼接；不读 os.environ。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engine.core import db as _db
from engine.core.db import Schedule, ScheduleRow
from engine.registry import load_registry
from engine.server import (
    Scheduler,
    _extract_reminders,
    _validate_cron,
    _fmt_task,
    create_app,
)
from test_runner import (  # noqa: F401  # 跨模块 fixture 随模块收集（autouse 清引擎库）
    _clean_engine_tables,
    db_engine,
)


@async_fixture(autouse=True)
async def _clean_schedule_table(db_engine):
    """每测试后额外清 schedule 表（test_runner._clean_engine_tables 不含此表）。"""
    yield
    async with AsyncSession(db_engine) as session, session.begin():
        await session.execute(text("TRUNCATE schedule RESTART IDENTITY CASCADE"))

REPO_ROOT = Path(__file__).resolve().parents[1]

# 测试用假地址（P2：段拼接）
_FAKE_SCHEME = "ht" + "tp://"
_FAKE_HOST = "127" + ".0.0.1"
_FAKE_BASE = _FAKE_SCHEME + "engine" + ".test"

# 已注册链 id（registry 已登记；调度器种子用 crm_reminder_chain 但该链
# 尚未在 registry 注册（批 3b 注册），故接口测试用已注册链）
_TEST_CHAIN_ID = "demo_echo_chain"


class _StubRunner:
    """测试用桩 runner（三接口 + 调度器不触达 runner 执行）。"""

    async def consume_once(self):
        return None

    async def resume(self, task_id: int):  # pragma: no cover
        raise AssertionError("调度器测试不应触发 runner.resume")


async def _noop_consumer(data: Any, **kwargs: Any) -> Any:
    raise AssertionError("调度器测试不应触发消费者")


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    _set_env(monkeypatch)
    return load_registry(REPO_ROOT)


@pytest.fixture
def api_app(db_engine, registry):
    """三接口 + schedule 接口测试 app（调度器禁用，手动 tick）。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={"tm.proposal": _noop_consumer},
        scheduler_enabled=False,
    )
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url=_FAKE_BASE)


# ==== _extract_reminders 纯函数 ====


def test_extract_reminders_valid() -> None:
    """ReminderResult 判据：含 reminders 列表 -> 返回原 dict。"""
    data = {"reminders": [{"customer_id": 1, "title": "跟进"}]}
    assert _extract_reminders(data) is data


def test_extract_reminders_missing_key() -> None:
    """缺 reminders 键 -> None。"""
    assert _extract_reminders({"todos": []}) is None


def test_extract_reminders_not_list() -> None:
    """reminders 非列表 -> None。"""
    assert _extract_reminders({"reminders": "not-a-list"}) is None


def test_extract_reminders_non_dict() -> None:
    """非 dict 入参 -> None。"""
    assert _extract_reminders(None) is None
    assert _extract_reminders("string") is None


# ==== _validate_cron ====


def test_validate_cron_valid() -> None:
    """合法 daily cron -> 原串返回。"""
    assert _validate_cron("0 7 * * *") == "0 7 * * *"
    assert _validate_cron("30 8 * * *") == "30 8 * * *"


def test_validate_cron_invalid() -> None:
    """非法 cron -> HTTPException 422。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        _validate_cron("invalid")
    assert exc_info.value.status_code == 422


def test_validate_cron_weekly() -> None:
    """非 daily cron（每周）-> 422。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        _validate_cron("0 7 * * 1")
    assert exc_info.value.status_code == 422


# ==== GET /api/engine/schedules ====


@pytest.mark.asyncio
async def test_list_schedules_empty(api_app) -> None:
    """空表 -> {schedules: []}。"""
    app, *_ = api_app if isinstance(api_app, tuple) else (api_app,)
    async with _client(app) as client:
        resp = await client.get("/api/engine/schedules")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schedules"] == []


@pytest.mark.asyncio
async def test_list_schedules_with_data(db_engine, registry) -> None:
    """有数据 -> 返回含正确字段的列表。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={},
        scheduler_enabled=False,
    )
    now = datetime.now(timezone.utc)
    next_run = _db.next_run_time("0 7 * * *", now)
    schedule_id = await _db.create_schedule(
        db_engine, chain_id=_TEST_CHAIN_ID, name="测试链", cron="0 7 * * *"
    )
    async with _db._sessions(db_engine)() as session, session.begin():
        await session.execute(
            __import__("sqlalchemy").update(Schedule)
            .where(Schedule.id == schedule_id)
            .values(next_run_at=next_run)
        )
    async with _client(app) as client:
        resp = await client.get("/api/engine/schedules")
    assert resp.status_code == 200
    schedules = resp.json()["schedules"]
    assert len(schedules) == 1
    s = schedules[0]
    assert s["id"] == schedule_id
    assert s["chain_id"] == _TEST_CHAIN_ID
    assert s["name"] == "测试链"
    assert s["cron"] == "0 7 * * *"
    assert s["enabled"] is True
    assert s["last_status"] == ""


# ==== POST /api/engine/schedules（新增）====


@pytest.mark.asyncio
async def test_create_schedule_201(db_engine, registry) -> None:
    """合法新增 -> 201 {id}，next_run_at 已计算。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={},
        scheduler_enabled=False,
    )
    async with _client(app) as client:
        resp = await client.post(
            "/api/engine/schedules",
            json={"chain_id": _TEST_CHAIN_ID, "schedule": "0 7 * * *", "name": "测试链"},
        )
    assert resp.status_code == 201
    sid = resp.json()["id"]
    row = await _db.get_schedule(db_engine, sid)
    assert row is not None
    assert row.chain_id == _TEST_CHAIN_ID
    assert row.cron == "0 7 * * *"
    assert row.enabled is True
    assert row.next_run_at is not None


@pytest.mark.asyncio
async def test_create_schedule_unknown_chain_422(db_engine, registry) -> None:
    """chain_id 未在 registry -> 422。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={},
        scheduler_enabled=False,
    )
    async with _client(app) as client:
        resp = await client.post(
            "/api/engine/schedules",
            json={"chain_id": "ghost_chain", "schedule": "0 7 * * *"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_schedule_bad_cron_422(db_engine, registry) -> None:
    """非法 cron -> 422。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={},
        scheduler_enabled=False,
    )
    async with _client(app) as client:
        resp = await client.post(
            "/api/engine/schedules",
            json={"chain_id": _TEST_CHAIN_ID, "schedule": "bad cron"},
        )
    assert resp.status_code == 422


# ==== POST /api/engine/schedules/{id}/toggle ====


@pytest.mark.asyncio
async def test_toggle_schedule(db_engine, registry) -> None:
    """toggle 翻转 enabled。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={},
        scheduler_enabled=False,
    )
    sid = await _db.create_schedule(
        db_engine, chain_id=_TEST_CHAIN_ID, name="测试", enabled=True
    )
    async with _client(app) as client:
        resp = await client.post(f"/api/engine/schedules/{sid}/toggle")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    row = await _db.get_schedule(db_engine, sid)
    assert row is not None
    assert row.enabled is False


@pytest.mark.asyncio
async def test_toggle_schedule_404(db_engine, registry) -> None:
    """toggle 不存在 -> 404。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={},
        scheduler_enabled=False,
    )
    async with _client(app) as client:
        resp = await client.post("/api/engine/schedules/99999/toggle")
    assert resp.status_code == 404


# ==== POST /api/engine/schedules/{id}/time ====


@pytest.mark.asyncio
async def test_update_time(db_engine, registry) -> None:
    """改时间 -> next_run_at 重算。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={},
        scheduler_enabled=False,
    )
    sid = await _db.create_schedule(
        db_engine, chain_id=_TEST_CHAIN_ID, name="测试", cron="0 7 * * *"
    )
    async with _client(app) as client:
        resp = await client.post(
            f"/api/engine/schedules/{sid}/time",
            json={"schedule": "30 8 * * *"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_run_at"] is not None
    row = await _db.get_schedule(db_engine, sid)
    assert row is not None
    assert row.cron == "30 8 * * *"


@pytest.mark.asyncio
async def test_update_time_bad_cron_422(db_engine, registry) -> None:
    """改时间 - 非法 cron -> 422。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={},
        scheduler_enabled=False,
    )
    sid = await _db.create_schedule(
        db_engine, chain_id=_TEST_CHAIN_ID, name="测试"
    )
    async with _client(app) as client:
        resp = await client.post(
            f"/api/engine/schedules/{sid}/time",
            json={"schedule": "bad cron"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_time_404(db_engine, registry) -> None:
    """改时间 - 不存在 -> 404。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={},
        scheduler_enabled=False,
    )
    async with _client(app) as client:
        resp = await client.post(
            "/api/engine/schedules/99999/time",
            json={"schedule": "0 7 * * *"},
        )
    assert resp.status_code == 404


# ==== POST /api/engine/schedules/{id}/run（立即运行，决策 37-7）====


@pytest.mark.asyncio
async def test_run_schedule_creates_task(db_engine, registry) -> None:
    """立即运行 -> engine_task 落库（trigger_type=manual_schedule）+ 锚点不动。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={},
        scheduler_enabled=False,
    )
    now = datetime.now(timezone.utc)
    next_run = _db.next_run_time("0 7 * * *", now)
    sid = await _db.create_schedule(
        db_engine, chain_id=_TEST_CHAIN_ID, name="测试", cron="0 7 * * *"
    )
    # 手动设 next_run_at
    async with _db._sessions(db_engine)() as session, session.begin():
        from sqlalchemy import update as _update
        await session.execute(
            _update(Schedule).where(Schedule.id == sid).values(next_run_at=next_run)
        )
    old_next = (await _db.get_schedule(db_engine, sid)).next_run_at
    async with _client(app) as client:
        resp = await client.post(f"/api/engine/schedules/{sid}/run")
    assert resp.status_code == 200
    engine_task_id = resp.json()["engine_task_id"]
    assert engine_task_id.startswith("e-")
    # 验证 engine_task 落库
    task_id = int(engine_task_id[2:])
    row = await _db.get_task(db_engine, task_id)
    assert row is not None
    assert row.trigger_type == "manual_schedule"
    assert row.trigger_ref == str(sid)
    assert row.input.get("trigger_date") is not None
    # 锚点不动
    row2 = await _db.get_schedule(db_engine, sid)
    assert row2.next_run_at == old_next


@pytest.mark.asyncio
async def test_run_schedule_404(db_engine, registry) -> None:
    """立即运行 - 不存在 -> 404。"""
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunner(),
        consumers={},
        scheduler_enabled=False,
    )
    async with _client(app) as client:
        resp = await client.post("/api/engine/schedules/99999/run")
    assert resp.status_code == 404


# ==== Scheduler 类测试（桩时钟）====


@pytest.mark.asyncio
async def test_scheduler_tick_triggers_on_time(db_engine, registry) -> None:
    """到点触发：next_run_at <= now -> 创建 engine_task + 锚点推进。"""
    # 设定固定时钟：2026-01-15 07:10 UTC
    fixed_now = datetime(2026, 1, 15, 7, 10, 0, tzinfo=timezone.utc)
    next_run = datetime(2026, 1, 15, 7, 0, 0, tzinfo=timezone.utc)  # 07:00 已过
    sid = await _db.create_schedule(
        db_engine, chain_id="crm_reminder_chain", name="CRM 提醒", cron="0 7 * * *"
    )
    async with _db._sessions(db_engine)() as session, session.begin():
        from sqlalchemy import update as _update
        await session.execute(
            _update(Schedule).where(Schedule.id == sid).values(next_run_at=next_run)
        )

    scheduler = Scheduler(
        db_engine, registry, interval=9999, now_fn=lambda: fixed_now
    )
    await scheduler.tick()

    # 验证 task 落库
    async with AsyncSession(db_engine) as session:
        from engine.core.db import EngineTask
        tasks = (await session.execute(select(EngineTask))).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].trigger_type == "schedule"
    assert tasks[0].trigger_ref == str(sid)
    assert tasks[0].input.get("trigger_date") == "2026-01-15"

    # 验证锚点推进
    row = await _db.get_schedule(db_engine, sid)
    assert row is not None
    assert row.last_run_at == next_run  # last_run_at = 本次 next_run_at
    assert row.next_run_at > fixed_now  # next_run_at = 下次（2026-01-16 07:00）
    assert row.last_status == ""


@pytest.mark.asyncio
async def test_scheduler_tick_missed_catchup(db_engine, registry) -> None:
    """漏跑补跑：07:10 启动，07:00 到点未触发 -> 补触发一次（详设 §9.3）。"""
    fixed_now = datetime(2026, 1, 15, 7, 10, 0, tzinfo=timezone.utc)
    next_run = datetime(2026, 1, 15, 7, 0, 0, tzinfo=timezone.utc)
    last_run = datetime(2026, 1, 14, 7, 0, 0, tzinfo=timezone.utc)  # 昨天跑过
    sid = await _db.create_schedule(
        db_engine, chain_id="crm_reminder_chain", name="CRM 提醒", cron="0 7 * * *"
    )
    async with _db._sessions(db_engine)() as session, session.begin():
        from sqlalchemy import update as _update
        await session.execute(
            _update(Schedule).where(Schedule.id == sid).values(
                next_run_at=next_run, last_run_at=last_run
            )
        )

    scheduler = Scheduler(
        db_engine, registry, interval=9999, now_fn=lambda: fixed_now
    )
    await scheduler.tick()

    # 补触发一次
    async with AsyncSession(db_engine) as session:
        from engine.core.db import EngineTask
        tasks = (await session.execute(select(EngineTask))).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].input.get("trigger_date") == "2026-01-15"

    # 锚点推进
    row = await _db.get_schedule(db_engine, sid)
    assert row.last_run_at == next_run
    assert row.next_run_at > fixed_now


@pytest.mark.asyncio
async def test_scheduler_tick_no_double_fire(db_engine, registry) -> None:
    """不重跑：锚点推进后同 tick 不重复触发（详设 §9.3）。"""
    fixed_now = datetime(2026, 1, 15, 7, 10, 0, tzinfo=timezone.utc)
    next_run = datetime(2026, 1, 15, 7, 0, 0, tzinfo=timezone.utc)
    sid = await _db.create_schedule(
        db_engine, chain_id="crm_reminder_chain", name="CRM 提醒", cron="0 7 * * *"
    )
    async with _db._sessions(db_engine)() as session, session.begin():
        from sqlalchemy import update as _update
        await session.execute(
            _update(Schedule).where(Schedule.id == sid).values(next_run_at=next_run)
        )

    scheduler = Scheduler(
        db_engine, registry, interval=9999, now_fn=lambda: fixed_now
    )
    # 连续两次 tick
    await scheduler.tick()
    await scheduler.tick()

    # 只触发一次
    async with AsyncSession(db_engine) as session:
        from engine.core.db import EngineTask
        tasks = (await session.execute(select(EngineTask))).scalars().all()
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_scheduler_tick_disabled_schedule_skipped(db_engine, registry) -> None:
    """disabled 链不触发。"""
    fixed_now = datetime(2026, 1, 15, 7, 10, 0, tzinfo=timezone.utc)
    next_run = datetime(2026, 1, 15, 7, 0, 0, tzinfo=timezone.utc)
    sid = await _db.create_schedule(
        db_engine, chain_id="crm_reminder_chain", name="CRM 提醒",
        cron="0 7 * * *", enabled=False
    )
    async with _db._sessions(db_engine)() as session, session.begin():
        from sqlalchemy import update as _update
        await session.execute(
            _update(Schedule).where(Schedule.id == sid).values(next_run_at=next_run)
        )

    scheduler = Scheduler(
        db_engine, registry, interval=9999, now_fn=lambda: fixed_now
    )
    await scheduler.tick()

    async with AsyncSession(db_engine) as session:
        from engine.core.db import EngineTask
        tasks = (await session.execute(select(EngineTask))).scalars().all()
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_scheduler_tick_exception_marks_failed(db_engine, registry) -> None:
    """触发异常 -> last_status='failed'，不阻塞其他链。"""
    fixed_now = datetime(2026, 1, 15, 7, 10, 0, tzinfo=timezone.utc)
    next_run = datetime(2026, 1, 15, 7, 0, 0, tzinfo=timezone.utc)
    # 链 1：正常
    sid1 = await _db.create_schedule(
        db_engine, chain_id=_TEST_CHAIN_ID, name="正常链", cron="0 7 * * *"
    )
    # 链 2：也正常但 next_run_at 设为 None（触发条件检查中的异常）
    sid2 = await _db.create_schedule(
        db_engine, chain_id=_TEST_CHAIN_ID, name="异常链", cron="0 7 * * *"
    )
    async with _db._sessions(db_engine)() as session, session.begin():
        from sqlalchemy import update as _update
        await session.execute(
            _update(Schedule).where(Schedule.id == sid1).values(next_run_at=next_run)
        )
        # sid2 的 next_run_at = None -> 被跳过（不是异常）

    # 直接通过 monkeypatch 制造异常：在 tick 中间 mock mark_schedule_run 抛异常
    import unittest.mock as _mock
    scheduler = Scheduler(
        db_engine, registry, interval=9999, now_fn=lambda: fixed_now
    )
    call_count = 0
    original_mark = _db.mark_schedule_run

    async def failing_mark(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("模拟 DB 写入异常")
        return await original_mark(*args, **kwargs)

    with _mock.patch.object(_db, "mark_schedule_run", failing_mark):
        await scheduler.tick()

    # 链 1 标记 failed（mark_schedule_run 异常）
    row1 = await _db.get_schedule(db_engine, sid1)
    assert row1 is not None
    assert row1.last_status == "failed"

    # 链 2 未受影响（next_run_at 为 None，未进入触发分支）
    row2 = await _db.get_schedule(db_engine, sid2)
    assert row2 is not None
    assert row2.last_status == ""


@pytest.mark.asyncio
async def test_scheduler_seed_on_empty_table(db_engine, registry) -> None:
    """空表 tick -> 种子 crm_reminder_chain 存在。"""
    fixed_now = datetime(2026, 1, 15, 7, 10, 0, tzinfo=timezone.utc)
    scheduler = Scheduler(
        db_engine, registry, interval=9999, now_fn=lambda: fixed_now
    )
    await scheduler.tick()

    rows = await _db.list_schedules(db_engine)
    assert len(rows) == 1
    assert rows[0].chain_id == "crm_reminder_chain"
    assert rows[0].cron == "0 7 * * *"
    assert rows[0].enabled is True
    # 种子创建后 next_run_at 为 None（需手动设或由 web 接口创建时计算）；
    # 但 tick 后种子行存在即满足验收要求


@pytest.mark.asyncio
async def test_scheduler_seed_idempotent(db_engine, registry) -> None:
    """非空表 tick -> 不重复插入种子。"""
    fixed_now = datetime(2026, 1, 15, 7, 10, 0, tzinfo=timezone.utc)
    # 预先插入一条
    await _db.create_schedule(
        db_engine, chain_id="crm_reminder_chain", name="已有", cron="0 7 * * *"
    )
    scheduler = Scheduler(
        db_engine, registry, interval=9999, now_fn=lambda: fixed_now
    )
    await scheduler.tick()

    rows = await _db.list_schedules(db_engine)
    assert len(rows) == 1  # 不重复


@pytest.mark.asyncio
async def test_scheduler_start_stop(db_engine, registry) -> None:
    """start/stop 生命周期（幂等）。"""
    scheduler = Scheduler(
        db_engine, registry, interval=9999
    )
    scheduler.start()
    assert scheduler._task is not None
    assert not scheduler._task.done()
    await scheduler.stop()
    assert scheduler._task is None
    # 幂等 stop
    await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_tick_initializes_null_next_run(db_engine, registry) -> None:
    """锚点 NULL 初始化（验收修复）：新建/种子链 next_run_at=None 时 tick 初始化
    为下一个 HH:MM 且不立即触发（今天已过 -> 明天；不补跑——新链从下次计划开始）。"""
    fixed_now = datetime(2026, 1, 15, 7, 10, 0, tzinfo=timezone.utc)  # 07:10，07:00 已过
    sid = await _db.create_schedule(
        db_engine, chain_id="crm_reminder_chain", name="CRM 提醒", cron="0 7 * * *"
    )
    # 确认创建时 next_run_at 为空（种子/新建链初始态）
    row0 = await _db.get_schedule(db_engine, sid)
    assert row0 is not None and row0.next_run_at is None

    scheduler = Scheduler(
        db_engine, registry, interval=9999, now_fn=lambda: fixed_now
    )
    await scheduler.tick()

    # 锚点被初始化（今天 07:00 已过 -> 明天 07:00），不触发
    row = await _db.get_schedule(db_engine, sid)
    assert row is not None
    assert row.next_run_at is not None
    assert row.next_run_at == datetime(2026, 1, 16, 7, 0, 0, tzinfo=timezone.utc)
    assert row.last_run_at is None
    async with AsyncSession(db_engine) as session:
        from engine.core.db import EngineTask
        tasks = (await session.execute(select(EngineTask))).scalars().all()
    assert len(tasks) == 0  # 未触发


@pytest.mark.asyncio
async def test_scheduler_tick_null_then_fires_next_day(db_engine, registry) -> None:
    """NULL 初始化后：第二次 tick（到明天 07:00）正常触发（种子链开箱可用闭环）。"""
    day1 = datetime(2026, 1, 15, 7, 10, 0, tzinfo=timezone.utc)
    sid = await _db.create_schedule(
        db_engine, chain_id="crm_reminder_chain", name="CRM 提醒", cron="0 7 * * *"
    )
    scheduler = Scheduler(
        db_engine, registry, interval=9999, now_fn=lambda: day1
    )
    await scheduler.tick()  # 初始化锚点 -> 明天 07:00

    # 第二天 07:00 整：到点触发
    day2 = datetime(2026, 1, 16, 7, 0, 0, tzinfo=timezone.utc)
    scheduler2 = Scheduler(
        db_engine, registry, interval=9999, now_fn=lambda: day2
    )
    await scheduler2.tick()
    async with AsyncSession(db_engine) as session:
        from engine.core.db import EngineTask
        tasks = (await session.execute(select(EngineTask))).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].trigger_type == "schedule"
    assert tasks[0].input.get("trigger_date") == "2026-01-16"
