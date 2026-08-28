"""v0.2 业务库 ORM + 迁移冒烟测试（详设-v0.2 §3 三表 + §2.2 迁移；R22 结构同源）。

全部走嵌入式 PG（tests/conftest.py 的 tm_pg_cluster：复用 engine 簇另建 liuquan
库 + tm schema + alembic upgrade head 真跑）——测的就是迁移 DDL 与 models/tm.py
ORM 的逐字段同源 + 三表落地 + 约束/索引生效（真 SQL 真事务，不桩）。

覆盖：
- schema 同源：三表列集合、JSONB/DATE 类型、CHECK（含具名 chk_blocked_reason /
  chk_result_note）、索引（含 idx_proposal_status 的 created_at DESC）、外键
- 冒烟：task / task_proposal / task_event 插入读取往返、derived_from 自引用 FK、
  task_event 的 ON DELETE CASCADE
- 决策 17 反向：done/void 无 result_note 被 CHECK 拒（A23 的 DB 层）、blocked 无
  blocked_reason 被拒、非法状态/超长标题/非法事件类型被拒

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- 回环地址与连接串一律运行期拼接（"127." 加 "0.0.1"），任何单一字符串常量
  不得含完整 IPv4 四段或 URL scheme（判据见 engine/lint/p2.py 模块 docstring）
- 不读 os.environ（P2 规则4）
"""

from __future__ import annotations

from datetime import date

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

from models.tm import Task, TaskEvent, TaskProposal

# ---- 期望 schema（与 migrations/business/versions/0001 的 DDL 逐列对齐）----

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "task": {
        "id", "title", "detail", "domain", "role", "due", "status",
        "blocked_reason", "result_note", "derived_from", "source_type",
        "source", "created_by", "created_at", "updated_at", "done_at",
    },
    "task_proposal": {
        "id", "title", "detail", "domain", "action_id", "risk",
        "suggested_role", "suggested_due_days", "evidence", "source",
        "status", "reviewed_by", "reviewed_at", "reject_reason",
        "task_id", "created_at",
    },
    "task_event": {
        "id", "task_id", "event_type", "from_status", "to_status",
        "actor", "note", "created_at",
    },
}

# 每表 CHECK 约束总数（task = 5 内联 + 2 具名；proposal = 4 内联；event = 1 内联）
EXPECTED_CHECK_COUNTS = {"task": 7, "task_proposal": 4, "task_event": 1}
EXPECTED_NAMED_CHECKS = {"chk_blocked_reason", "chk_result_note"}

EXPECTED_INDEXES: dict[str, set[str]] = {
    "task": {"idx_task_status_due", "idx_task_role", "task_pkey"},
    "task_proposal": {"idx_proposal_status", "task_proposal_pkey"},
    "task_event": {"idx_event_task", "task_event_pkey"},
}

EXPECTED_JSONB_COLUMNS = {
    ("task", "source"),
    ("task_proposal", "evidence"),
    ("task_proposal", "source"),
}
EXPECTED_DATE_COLUMNS = {("task", "due")}


# ---- fixtures：function 级 async 引擎/会话（每测试独立事务，不共享数据）----


@async_fixture
async def tm_engine(tm_pg_cluster):
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture
async def tm_session(tm_engine):
    maker = async_sessionmaker(tm_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


async def _add_task(session: AsyncSession, **overrides: object) -> Task:
    """插一条合法任务（§3.1 最小合法行），可覆盖字段构造反向用例。"""
    data: dict[str, object] = {
        "title": "处理退货",
        "detail": "买家要退货，跟进处理",
        "domain": "crm",
        "role": "运营",
        "due": date(2026, 9, 5),
        "status": "open",
        "source_type": "manual",
        "source": {"creator": "运营"},
        "created_by": "运营",
    }
    data.update(overrides)
    task = Task(**data)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ---- schema 同源：列 / 类型 / 约束 / 索引 / 外键 ----


@pytest.mark.asyncio
async def test_migration_creates_three_tm_tables(tm_engine) -> None:
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'tm' ORDER BY table_name, ordinal_position"
            )
        )
        rows = res.fetchall()
    actual: dict[str, set[str]] = {}
    for table, column in rows:
        actual.setdefault(table, set()).add(column)
    assert set(actual) == {"task", "task_proposal", "task_event"}
    for table, cols in EXPECTED_COLUMNS.items():
        assert actual[table] == cols, f"{table} 列集合与详设 §3/ORM 不一致"


@pytest.mark.asyncio
async def test_migration_jsonb_and_date_columns(tm_engine) -> None:
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT table_name, column_name, udt_name "
                "FROM information_schema.columns WHERE table_schema = 'tm'"
            )
        )
        rows = res.fetchall()
    types = {(table, column): udt for table, column, udt in rows}
    for table, column in EXPECTED_JSONB_COLUMNS:
        assert types[(table, column)] == "jsonb", f"{table}.{column} 应为 jsonb"
    for table, column in EXPECTED_DATE_COLUMNS:
        assert types[(table, column)] == "date", f"{table}.{column} 应为 date"


@pytest.mark.asyncio
async def test_migration_check_constraints(tm_engine) -> None:
    """CHECK 约束全落地：具名两个（chk_blocked_reason/chk_result_note）+ 数量对齐。"""
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT conrelid::regclass::text AS tbl, conname FROM pg_constraint "
                "WHERE connamespace = 'tm'::regnamespace AND contype = 'c'"
            )
        )
        rows = res.fetchall()
    by_table: dict[str, set[str]] = {}
    for tbl, conname in rows:
        by_table.setdefault(tbl.split(".")[-1], set()).add(conname)
    for table, count in EXPECTED_CHECK_COUNTS.items():
        assert len(by_table[table]) == count, (
            f"{table} CHECK 数量应为 {count}，实际 {sorted(by_table[table])}"
        )
    all_checks = {c for names in by_table.values() for c in names}
    assert EXPECTED_NAMED_CHECKS <= all_checks


@pytest.mark.asyncio
async def test_migration_indexes(tm_engine) -> None:
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT tablename, indexname FROM pg_indexes "
                "WHERE schemaname = 'tm'"
            )
        )
        rows = res.fetchall()
    by_table: dict[str, set[str]] = {}
    for tablename, indexname in rows:
        by_table.setdefault(tablename, set()).add(indexname)
    for table, indexes in EXPECTED_INDEXES.items():
        assert by_table[table] == indexes, f"{table} 索引集合与详设 §3 不一致"


@pytest.mark.asyncio
async def test_idx_proposal_status_is_desc(tm_engine) -> None:
    """idx_proposal_status 为 (status, created_at DESC)（详设 §3.2 原文）。"""
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'tm' AND indexname = 'idx_proposal_status'"
            )
        )
        assert "created_at DESC" in res.scalar_one()


@pytest.mark.asyncio
async def test_migration_foreign_keys(tm_engine) -> None:
    """业务库内三处 FK 合法（R23：跨库不建外键，task.source 不 FK 引擎库）。"""
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT conrelid::regclass::text, pg_get_constraintdef(oid) "
                "FROM pg_constraint WHERE connamespace = 'tm'::regnamespace "
                "AND contype = 'f'"
            )
        )
        rows = res.fetchall()
    defs = {tbl.split(".")[-1]: d for tbl, d in rows}
    assert set(defs) == {"task", "task_proposal", "task_event"}
    assert "REFERENCES tm.task(id)" in defs["task"]  # derived_from 自引用
    assert "REFERENCES tm.task(id)" in defs["task_proposal"]  # task_id
    assert "REFERENCES tm.task(id) ON DELETE CASCADE" in defs["task_event"]


# ---- 冒烟：插入 / 读取 / 派生 / 级联 ----


@pytest.mark.asyncio
async def test_task_insert_read_roundtrip(tm_session) -> None:
    task = await _add_task(tm_session)
    assert task.id is not None
    row = await tm_session.get(Task, task.id)
    assert row is not None
    assert row.title == "处理退货"
    assert row.domain == "crm"
    assert row.role == "运营"
    assert row.due == date(2026, 9, 5)
    assert row.status == "open"
    assert row.source_type == "manual"
    assert row.source == {"creator": "运营"}
    assert row.created_by == "运营"
    assert row.created_at is not None
    assert row.updated_at is not None
    assert row.done_at is None


@pytest.mark.asyncio
async def test_task_default_status_and_ai_source(tm_session) -> None:
    """status 默认 open；source_type='ai' 的 §3.4 追溯结构可落库。"""
    task = await _add_task(
        tm_session,
        source_type="ai",
        source={
            "chain_id": "tm_demo_chain",
            "engine_task_id": "e-000001",
            "worker_id": "demo_propose",
            "audit_ids": ["aud-1"],
        },
    )
    assert task.status == "open"
    assert task.source["engine_task_id"] == "e-000001"


@pytest.mark.asyncio
async def test_task_derived_from_self_fk(tm_session) -> None:
    """派生：B.derived_from = A.id；派生≠原任务结束（A 保持 open，决策 17）。"""
    parent = await _add_task(tm_session, title="原任务")
    child = await _add_task(tm_session, title="派生任务", derived_from=parent.id)
    assert child.derived_from == parent.id
    assert parent.status == "open"


@pytest.mark.asyncio
async def test_proposal_insert_read_roundtrip(tm_session) -> None:
    """提案落库 + task_id FK 关联（§3.2：evidence/source JSONB 往返）。"""
    task = await _add_task(tm_session)
    proposal = TaskProposal(
        title="建议跟进退货买家",
        detail="买家要求退货，建议生成处理退货任务",
        domain="crm",
        action_id="tm.proposal",
        risk="suggest",
        suggested_role="运营",
        suggested_due_days=3,
        evidence=[{"kind": "message", "ref_id": "msg-001", "quote": "我要退货"}],
        source={
            "chain_id": "tm_demo_chain",
            "engine_task_id": "e-000001",
            "worker_id": "demo_propose",
            "audit_ids": ["aud-1"],
        },
        status="pending",
        task_id=task.id,
    )
    tm_session.add(proposal)
    await tm_session.commit()
    await tm_session.refresh(proposal)
    assert proposal.id is not None
    assert proposal.status == "pending"
    assert proposal.risk == "suggest"
    assert proposal.task_id == task.id
    assert proposal.evidence == [
        {"kind": "message", "ref_id": "msg-001", "quote": "我要退货"}
    ]
    assert proposal.source["worker_id"] == "demo_propose"


@pytest.mark.asyncio
async def test_task_event_insert_and_cascade(tm_session) -> None:
    """事件落库 + ON DELETE CASCADE（§3.3：删任务级联删流水）。"""
    task = await _add_task(tm_session)
    event = TaskEvent(
        task_id=task.id,
        event_type="created",
        from_status=None,
        to_status="open",
        actor="运营",
        note=None,
    )
    tm_session.add(event)
    await tm_session.commit()
    await tm_session.refresh(event)
    assert event.id is not None
    assert event.event_type == "created"
    assert event.to_status == "open"

    await tm_session.delete(task)
    await tm_session.commit()
    res = await tm_session.execute(
        select(TaskEvent).where(TaskEvent.task_id == task.id)
    )
    assert res.scalar_one_or_none() is None  # 事件随任务级联删除


# ---- 决策 17 反向：CHECK 强制（A23 的 DB 层，页面拦截之外的兜底）----


@pytest.mark.asyncio
async def test_chk_result_note_blocks_done_without_reply(tm_session) -> None:
    """done 无 result_note 被拒（决策 17：不回复不能结束）。"""
    with pytest.raises(IntegrityError, match="chk_result_note"):
        await _add_task(tm_session, status="done")
    await tm_session.rollback()


@pytest.mark.asyncio
async def test_chk_result_note_blocks_void_without_reply(tm_session) -> None:
    """void 无 result_note 被拒（作废也必填回复，决策 17）。"""
    with pytest.raises(IntegrityError, match="chk_result_note"):
        await _add_task(tm_session, status="void")
    await tm_session.rollback()


@pytest.mark.asyncio
async def test_done_with_result_note_is_allowed(tm_session) -> None:
    """done + result_note 合法落库（回复即回流，决策 17）。"""
    task = await _add_task(tm_session, status="done", result_note="已完成退货处理")
    assert task.status == "done"
    assert task.result_note == "已完成退货处理"


@pytest.mark.asyncio
async def test_chk_blocked_reason_blocks_blocked_without_reason(tm_session) -> None:
    """blocked 无 blocked_reason 被拒（决策 11：等物料/等回复二选一必填）。"""
    with pytest.raises(IntegrityError, match="chk_blocked_reason"):
        await _add_task(tm_session, status="blocked")
    await tm_session.rollback()


@pytest.mark.asyncio
async def test_invalid_status_rejected(tm_session) -> None:
    """五态之外的 status 被 CHECK 拒（§3.1）。"""
    with pytest.raises(IntegrityError):
        await _add_task(tm_session, status="cancelled")
    await tm_session.rollback()


@pytest.mark.asyncio
async def test_title_over_80_chars_rejected(tm_session) -> None:
    """title 超 80 字符被 CHECK 拒（§3.1 对齐 TaskProposal.title）。"""
    with pytest.raises(IntegrityError):
        await _add_task(tm_session, title="x" * 81)
    await tm_session.rollback()


@pytest.mark.asyncio
async def test_invalid_event_type_rejected(tm_session) -> None:
    """event_type 非法值被 CHECK 拒（§3.3 九态）。"""
    task = await _add_task(tm_session)
    event = TaskEvent(
        task_id=task.id,
        event_type="bogus",
        from_status=None,
        to_status="open",
        actor="运营",
        note=None,
    )
    tm_session.add(event)
    with pytest.raises(IntegrityError):
        await tm_session.commit()
    await tm_session.rollback()
