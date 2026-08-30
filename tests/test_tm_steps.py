"""v0.3 复核反馈 #6 步骤测试：任务分解清单 + 完成依赖 + 派生弱化（link_parent）。

- add_step / list_steps / toggle_step（可反勾）/ delete_step
- **完成依赖**：有未完成步骤 -> transition completed 被拒（TMWebError）；
  全部步骤完成 -> 可 done（result_note 照常必填）
- **派生弱化**：link_parent=False（默认）不挂 derived_from、无 derived 事件、
  source 记 from_task_id；True 挂关联 + derived 事件
"""

from __future__ import annotations

from datetime import date

import pytest
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from models.tm import Task, TaskEvent, TaskStep
from web.tm_store import ROLE_VALUES, TMStore, TMWebError

_ROLE = ROLE_VALUES[0]


@async_fixture
async def tm_engine(tm_pg_cluster):
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean(tm_engine):
    async with AsyncSession(tm_engine) as session, session.begin():
        await session.execute(
            text("TRUNCATE tm.task_step, tm.task_event, tm.task_proposal, tm.task RESTART IDENTITY CASCADE")
        )
    yield


@pytest.fixture
def store(tm_engine) -> TMStore:
    return TMStore(tm_engine)


@async_fixture
async def _task(tm_engine) -> int:
    async with AsyncSession(tm_engine) as session, session.begin():
        t = Task(
            title="处理退货", detail="", domain="crm", role=_ROLE,
            due=date(2026, 9, 30), source_type="manual",
            source={"creator": _ROLE}, created_by=_ROLE,
        )
        session.add(t)
        await session.flush()
        return t.id


# ---- 步骤 CRUD ----


@pytest.mark.asyncio
async def test_add_and_list_steps(store, _task) -> None:
    s1 = await store.add_step(_task, "确认退货原因")
    s2 = await store.add_step(_task, "联系物流取件")
    assert s1["status"] == "open" and s2["status"] == "open"
    steps = await store.list_steps(_task)
    assert [s["content"] for s in steps] == ["确认退货原因", "联系物流取件"]
    total, done = await store.step_counts(_task)
    assert (total, done) == (2, 0)


@pytest.mark.asyncio
async def test_toggle_step_and_back(store, _task) -> None:
    s = await store.add_step(_task, "办理退款")
    toggled = await store.toggle_step(_task, s["id"])
    assert toggled["status"] == "done"
    untoggled = await store.toggle_step(_task, s["id"])
    assert untoggled["status"] == "open"  # 可反勾


@pytest.mark.asyncio
async def test_delete_step(store, _task) -> None:
    s = await store.add_step(_task, "临时步骤")
    await store.delete_step(_task, s["id"])
    assert await store.list_steps(_task) == []


@pytest.mark.asyncio
async def test_add_step_requires_content(store, _task) -> None:
    with pytest.raises(TMWebError):
        await store.add_step(_task, "   ")


# ---- 完成依赖（步骤完成之后才能结束任务）----


@pytest.mark.asyncio
async def test_completed_blocked_by_open_steps(store, _task) -> None:
    await store.add_step(_task, "确认退货原因")
    await store.add_step(_task, "办理退款")
    await store.transition(_task, "started", actor=_ROLE)
    with pytest.raises(TMWebError) as exc:
        await store.transition(_task, "completed", actor=_ROLE, result_note="处理完")
    assert "步骤未完成" in str(exc.value)


@pytest.mark.asyncio
async def test_completed_ok_when_all_steps_done(store, _task) -> None:
    s1 = await store.add_step(_task, "确认退货原因")
    s2 = await store.add_step(_task, "办理退款")
    await store.transition(_task, "started", actor=_ROLE)
    await store.toggle_step(_task, s1["id"])
    await store.toggle_step(_task, s2["id"])
    task = await store.transition(_task, "completed", actor=_ROLE, result_note="全部完成")
    assert task.status == "done"


@pytest.mark.asyncio
async def test_voided_not_blocked_by_steps(store, _task) -> None:
    """作废不受步骤约束（作废 = 放弃，不要求步骤完成）。"""
    await store.add_step(_task, "未完成步骤")
    await store.transition(_task, "started", actor=_ROLE)
    task = await store.transition(_task, "voided", actor=_ROLE, result_note="放弃处理")
    assert task.status == "void"


# ---- 派生弱化（link_parent）----


@pytest.mark.asyncio
async def test_derive_default_no_link(store, _task, tm_engine) -> None:
    """link_parent 默认 False：不挂 derived_from、无 derived 事件、source 记 from_task_id。"""
    child = await store.derive_task(
        _task, title="快速新建", detail=None, domain="crm", role=_ROLE,
        due=date(2026, 9, 30), actor=_ROLE,
    )
    assert child.derived_from is None
    assert (child.source or {}).get("from_task_id") == _task
    async with AsyncSession(tm_engine) as session:
        evs = (await session.execute(text("SELECT event_type FROM tm.task_event ORDER BY id"))).scalars().all()
        assert list(evs) == ["created"]  # 无 derived 事件（不关联）


@pytest.mark.asyncio
async def test_derive_with_link(store, _task, tm_engine) -> None:
    child = await store.derive_task(
        _task, title="关联派生", detail=None, domain="crm", role=_ROLE,
        due=date(2026, 9, 30), actor=_ROLE, link_parent=True,
    )
    assert child.derived_from == _task
    async with AsyncSession(tm_engine) as session:
        evs = (await session.execute(text("SELECT event_type FROM tm.task_event ORDER BY id"))).scalars().all()
        assert list(evs) == ["created", "derived"]
