"""v0.3 业务库 ORM + 迁移冒烟测试（详设-v0.3 §3 crm 四表 + §4 tm 扩展；R22 结构同源）。

全部走嵌入式 PG（tests/conftest.py 的 tm_pg_cluster：复用 engine 簇另建 liuquan
库 + tm/crm schema + alembic upgrade head 真跑（自动含 0002/0003））——测的就是
迁移 DDL 与 models/crm.py / models/tm.py ORM 的逐字段同源 + 四表落地 + 约束/
索引/级联生效（真 SQL 真事务，不桩）。

覆盖：
- schema 同源（crm）：四表列集合、JSONB 类型、CHECK 数量（含具名
  chk_candidate_evidence）、索引（含三处 created_at/updated_at DESC）、外键
  （message/snapshot/todo_candidate -> crm.customer ON DELETE CASCADE；
  confirmed_task_id 按 R23 不建 FK）
- 冒烟（crm）：四表插入读取往返 + 默认值；客户删除级联清 message/snapshot/
  candidate（决策 22）；todo_candidate evidence 非空通过 + confirmed_task_id
  回填（§8.1 确认事务语义）
- 反向（crm）：evidence 空数组/缺省被拒（chk_candidate_evidence，决策 16
  禁幻觉①）、status 非法被拒
- tm 扩展（迁移 0003，§4）：tm.task tags/ai_suggestion 列存在 + 往返；
  tm.task_event detail 列存在 + 新事件类型 suggested/transferred/disagreed
  通过 CHECK（决策 27/28）

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- 回环地址与连接串一律运行期拼接（"127." 加 "0.0.1"），任何单一字符串常量
  不得含完整 IPv4 四段或 URL scheme（判据见 engine/lint/p2.py 模块 docstring）
- 不读 os.environ（P2 规则4）
"""

from __future__ import annotations

from datetime import date, datetime, timezone

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

from models.crm import Customer, Message, Snapshot, TodoCandidate
from models.tm import Task, TaskEvent

# ---- 期望 schema（与 migrations/business/versions/0002 + 0003 的 DDL 逐列对齐）----

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "customer": {
        "id", "nickname", "source_shop", "remark", "follow_up_status",
        "trade_stage", "order_no", "latest_summary", "last_contacted_at",
        "created_at", "updated_at",
    },
    "message": {
        "id", "customer_id", "source_text", "translated_text", "direction",
        "language", "message_time", "created_at",
    },
    "snapshot": {
        "id", "customer_id", "current_need", "need_history", "sentiment",
        "todos", "summary", "created_at",
    },
    "todo_candidate": {
        "id", "customer_id", "content", "reason", "suggested_tags",
        "evidence", "status", "engine_task_id", "confirmed_task_id",
        "created_at", "confirmed_at", "dismissed_at",
    },
}

# 每表 CHECK 约束总数（customer = 3；message = 1；snapshot = 0；
# todo_candidate = content 长度 + status + chk_candidate_evidence 具名 = 3）
EXPECTED_CHECK_COUNTS = {"customer": 3, "message": 1, "snapshot": 0, "todo_candidate": 3}
EXPECTED_NAMED_CHECKS = {"chk_candidate_evidence"}

EXPECTED_INDEXES: dict[str, set[str]] = {
    "customer": {"idx_crm_customer_status", "idx_crm_customer_updated", "customer_pkey"},
    "message": {"idx_crm_message_customer", "message_pkey"},
    "snapshot": {"idx_crm_snapshot_customer", "snapshot_pkey"},
    "todo_candidate": {
        "idx_crm_candidate_customer", "idx_crm_candidate_status", "todo_candidate_pkey",
    },
}

EXPECTED_JSONB_COLUMNS = {
    ("snapshot", "need_history"),
    ("snapshot", "todos"),
    ("todo_candidate", "suggested_tags"),
    ("todo_candidate", "evidence"),
}


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


async def _add_customer(session: AsyncSession, **overrides: object) -> Customer:
    """插一个合法客户（§3.1 最小合法行 = 仅 nickname），可覆盖字段构造反向用例。"""
    data: dict[str, object] = {"nickname": "Alice"}
    data.update(overrides)
    customer = Customer(**data)
    session.add(customer)
    await session.commit()
    await session.refresh(customer)
    return customer


async def _add_task(session: AsyncSession, **overrides: object) -> Task:
    """插一个合法任务（§3.1 最小合法行，crm 域任务卡数据源），可覆盖字段。"""
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


# ---- schema 同源（crm）：列 / 类型 / 约束 / 索引 / 外键 ----


@pytest.mark.asyncio
async def test_migration_creates_four_crm_tables(tm_engine) -> None:
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'crm' ORDER BY table_name, ordinal_position"
            )
        )
        rows = res.fetchall()
    actual: dict[str, set[str]] = {}
    for table, column in rows:
        actual.setdefault(table, set()).add(column)
    assert set(actual) == {"customer", "message", "snapshot", "todo_candidate"}
    for table, cols in EXPECTED_COLUMNS.items():
        assert actual[table] == cols, f"{table} 列集合与详设 §3/ORM 不一致"


@pytest.mark.asyncio
async def test_migration_jsonb_columns(tm_engine) -> None:
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT table_name, column_name, udt_name "
                "FROM information_schema.columns WHERE table_schema = 'crm'"
            )
        )
        rows = res.fetchall()
    types = {(table, column): udt for table, column, udt in rows}
    for table, column in EXPECTED_JSONB_COLUMNS:
        assert types[(table, column)] == "jsonb", f"{table}.{column} 应为 jsonb"


@pytest.mark.asyncio
async def test_migration_check_constraints(tm_engine) -> None:
    """CHECK 约束全落地：数量对齐 + 具名 chk_candidate_evidence 在册。"""
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT conrelid::regclass::text AS tbl, conname FROM pg_constraint "
                "WHERE connamespace = 'crm'::regnamespace AND contype = 'c'"
            )
        )
        rows = res.fetchall()
    by_table: dict[str, set[str]] = {}
    for tbl, conname in rows:
        by_table.setdefault(tbl.split(".")[-1], set()).add(conname)
    for table, count in EXPECTED_CHECK_COUNTS.items():
        names = by_table.get(table, set())  # snapshot 无 CHECK，允许空集
        assert len(names) == count, (
            f"{table} CHECK 数量应为 {count}，实际 {sorted(names)}"
        )
    all_checks = {c for names in by_table.values() for c in names}
    assert EXPECTED_NAMED_CHECKS <= all_checks


@pytest.mark.asyncio
async def test_migration_indexes(tm_engine) -> None:
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text("SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'crm'")
        )
        rows = res.fetchall()
    by_table: dict[str, set[str]] = {}
    for tablename, indexname in rows:
        by_table.setdefault(tablename, set()).add(indexname)
    for table, indexes in EXPECTED_INDEXES.items():
        assert by_table[table] == indexes, f"{table} 索引集合与详设 §3 不一致"


@pytest.mark.asyncio
async def test_crm_desc_indexes(tm_engine) -> None:
    """三处 DESC 索引（§3.1 updated_at DESC / §3.3 created_at DESC / §3.4 created_at DESC）。"""
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'crm' AND indexname IN "
                "('idx_crm_customer_updated','idx_crm_snapshot_customer',"
                "'idx_crm_candidate_status')"
            )
        )
        rows = res.fetchall()
    defs = dict(rows)
    assert "DESC" in defs["idx_crm_customer_updated"]
    assert "DESC" in defs["idx_crm_snapshot_customer"]
    assert "DESC" in defs["idx_crm_candidate_status"]


@pytest.mark.asyncio
async def test_migration_foreign_keys_cascade(tm_engine) -> None:
    """三处 FK -> crm.customer(id) ON DELETE CASCADE（决策 22 级联）；
    confirmed_task_id 按 R23 不建 FK（详设 §2.1，关联走 tm.task.source）。"""
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT conrelid::regclass::text, pg_get_constraintdef(oid) "
                "FROM pg_constraint WHERE connamespace = 'crm'::regnamespace "
                "AND contype = 'f'"
            )
        )
        rows = res.fetchall()
    defs = {tbl.split(".")[-1]: d for tbl, d in rows}
    assert set(defs) == {"message", "snapshot", "todo_candidate"}
    for table in ("message", "snapshot", "todo_candidate"):
        assert f"REFERENCES crm.customer(id) ON DELETE CASCADE" in defs[table], (
            f"{table}.customer_id 应为 ON DELETE CASCADE 外键"
        )


# ---- 冒烟（crm）：插入 / 读取 / 默认值 / 级联 / 回填 ----


@pytest.mark.asyncio
async def test_customer_insert_read_roundtrip(tm_session) -> None:
    customer = await _add_customer(tm_session)
    assert customer.id is not None
    row = await tm_session.get(Customer, customer.id)
    assert row is not None
    assert row.nickname == "Alice"
    assert row.follow_up_status == "waiting_reply"  # 默认五态
    assert row.trade_stage == "pre_sale"  # 默认三阶段
    assert row.source_shop == ""
    assert row.remark == ""
    assert row.order_no == ""
    assert row.latest_summary is None
    assert row.last_contacted_at is None
    assert row.created_at is not None
    assert row.updated_at is not None


@pytest.mark.asyncio
async def test_message_insert_read_roundtrip(tm_session) -> None:
    customer = await _add_customer(tm_session)
    msg = Message(
        customer_id=customer.id,
        source_text="I want a refund please",
        translated_text="我想要退款",
        direction="buyer",
        language="en",
        message_time=None,  # 决策 23：粘贴不标时间
    )
    tm_session.add(msg)
    await tm_session.commit()
    await tm_session.refresh(msg)
    assert msg.id is not None
    assert msg.customer_id == customer.id
    assert msg.source_text == "I want a refund please"
    assert msg.translated_text == "我想要退款"
    assert msg.direction == "buyer"
    assert msg.language == "en"
    assert msg.message_time is None
    assert msg.created_at is not None


@pytest.mark.asyncio
async def test_snapshot_insert_read_roundtrip(tm_session) -> None:
    customer = await _add_customer(tm_session)
    snap = Snapshot(
        customer_id=customer.id,
        current_need="等待退货标签",
        need_history=["初询退货", "确认退货"],  # 只增不减（语义在应用层，DB 存数组）
        sentiment="焦急",
        todos=["提供退货标签"],
        summary="买家要求退货，需提供标签并跟进退款",
    )
    tm_session.add(snap)
    await tm_session.commit()
    await tm_session.refresh(snap)
    assert snap.id is not None
    assert snap.need_history == ["初询退货", "确认退货"]
    assert snap.todos == ["提供退货标签"]
    assert snap.sentiment == "焦急"
    assert snap.summary.startswith("买家要求退货")


@pytest.mark.asyncio
async def test_candidate_insert_and_confirmed_task_backfill(tm_session) -> None:
    """候选落库 + 确认事务语义（§8.1）：status=confirmed + confirmed_task_id 回填。"""
    customer = await _add_customer(tm_session)
    candidate = TodoCandidate(
        customer_id=customer.id,
        content="提供退货标签并跟进退款",
        reason="买家明确要求退货（对话 3 条提及）",
        suggested_tags=["售后"],
        evidence=[{"type": "message", "ref_id": "msg-001", "quote": "I want a refund"}],
        status="pending",
        engine_task_id=1001,  # 跨库追溯，不 FK
    )
    tm_session.add(candidate)
    await tm_session.commit()
    await tm_session.refresh(candidate)
    assert candidate.id is not None
    assert candidate.status == "pending"
    assert candidate.evidence == [
        {"type": "message", "ref_id": "msg-001", "quote": "I want a refund"}
    ]

    # 确认事务（§8.1）：建 tm.task + 候选 confirmed + confirmed_at + confirmed_task_id 回填
    task = await _add_task(tm_session, title=candidate.content)
    candidate.status = "confirmed"
    candidate.confirmed_task_id = task.id
    candidate.confirmed_at = datetime.now(timezone.utc)
    await tm_session.commit()
    await tm_session.refresh(candidate)
    assert candidate.status == "confirmed"
    assert candidate.confirmed_at is not None
    assert candidate.confirmed_task_id == task.id


@pytest.mark.asyncio
async def test_customer_delete_cascades_children(tm_session) -> None:
    """决策 22：删客户级联清 message/snapshot/todo_candidate（FK CASCADE 兜底）。"""
    customer = await _add_customer(tm_session)
    tm_session.add(
        Message(customer_id=customer.id, source_text="hi", direction="buyer")
    )
    tm_session.add(Snapshot(customer_id=customer.id, current_need="x"))
    tm_session.add(
        TodoCandidate(
            customer_id=customer.id,
            content="跟进处理",
            evidence=[{"type": "message", "ref_id": "m1", "quote": "hi"}],
        )
    )
    await tm_session.commit()

    await tm_session.delete(customer)
    await tm_session.commit()

    assert (await tm_session.get(Customer, customer.id)) is None
    for model in (Message, Snapshot, TodoCandidate):
        res = await tm_session.execute(
            select(model).where(model.customer_id == customer.id)
        )
        assert res.scalar_one_or_none() is None, f"{model.__tablename__} 应随客户级联删除"


# ---- 反向（crm）：evidence 禁幻觉① / status 非法 ----


@pytest.mark.asyncio
async def test_candidate_evidence_empty_rejected(tm_session) -> None:
    """evidence 空数组被 chk_candidate_evidence 拒（决策 16 禁幻觉①）。"""
    customer = await _add_customer(tm_session)
    candidate = TodoCandidate(
        customer_id=customer.id,
        content="无依据候选",
        evidence=[],  # jsonb_array_length([]) = 0，不满足 > 0
    )
    tm_session.add(candidate)
    with pytest.raises(IntegrityError, match="chk_candidate_evidence"):
        await tm_session.commit()
    await tm_session.rollback()


@pytest.mark.asyncio
async def test_candidate_evidence_missing_rejected(tm_session) -> None:
    """evidence 缺省（NULL）被 NOT NULL 拒（禁幻觉① DB 兜底）。"""
    customer = await _add_customer(tm_session)
    candidate = TodoCandidate(customer_id=customer.id, content="无依据候选")
    tm_session.add(candidate)
    with pytest.raises(IntegrityError):
        await tm_session.commit()
    await tm_session.rollback()


@pytest.mark.asyncio
async def test_candidate_invalid_status_rejected(tm_session) -> None:
    """status 非法值被 CHECK 拒（§3.4 三态 pending/confirmed/dismissed）。"""
    customer = await _add_customer(tm_session)
    candidate = TodoCandidate(
        customer_id=customer.id,
        content="非法状态候选",
        evidence=[{"type": "message", "ref_id": "m1", "quote": "x"}],
        status="doing",  # 非三态
    )
    tm_session.add(candidate)
    with pytest.raises(IntegrityError):
        await tm_session.commit()
    await tm_session.rollback()


# ---- tm 扩展（迁移 0003，§4）：tags / ai_suggestion / detail + 12 值事件类型 ----


@pytest.mark.asyncio
async def test_tm_task_has_tags_and_ai_suggestion(tm_engine) -> None:
    """0003：tm.task +tags/+ai_suggestion（§4.1，决策 25/27），列存在且为 jsonb。"""
    async with AsyncSession(tm_engine) as session:
        res = await session.execute(
            text(
                "SELECT column_name, udt_name FROM information_schema.columns "
                "WHERE table_schema = 'tm' AND table_name = 'task' "
                "AND column_name IN ('tags','ai_suggestion')"
            )
        )
        rows = res.fetchall()
    types = dict(rows)
    assert types == {"tags": "jsonb", "ai_suggestion": "jsonb"}


@pytest.mark.asyncio
async def test_tm_task_tags_ai_suggestion_roundtrip(tm_session) -> None:
    """tags 默认 [] + 写入往返；ai_suggestion 结构（§4.1）可落库。"""
    task = await _add_task(tm_session)
    assert task.tags == []  # 默认空数组（决策 25 开放标签）
    assert task.ai_suggestion is None

    task.tags = ["售后", "退货"]
    task.ai_suggestion = {
        "action": "assign",
        "target_role": "运营",
        "tags": ["售后"],
        "note": "建议继续跟进退款",
        "suggested_at": "2026-08-29T10:00:00+08:00",
        "engine_task_id": 1001,
    }
    await tm_session.commit()
    await tm_session.refresh(task)
    assert task.tags == ["售后", "退货"]
    assert task.ai_suggestion["action"] == "assign"
    assert task.ai_suggestion["target_role"] == "运营"
    assert task.ai_suggestion["engine_task_id"] == 1001


@pytest.mark.asyncio
async def test_tm_task_event_detail_and_new_types(tm_session) -> None:
    """0003：task_event +detail 列；suggested/transferred/disagreed 三新事件类型过 CHECK（§4.2）。"""
    task = await _add_task(tm_session)

    # suggested：AI 建议写入 ai_suggestion 时（detail = ai_suggestion 快照）
    tm_session.add(
        TaskEvent(
            task_id=task.id,
            event_type="suggested",
            actor="AI",
            note="建议流转到 ERP 生成采购申请",
            detail={
                "action": "transfer",
                "target_domain": "erp",
                "note": "建议流转到 ERP 生成采购申请",
            },
        )
    )
    # transferred：人执行流转时（detail = {action, target, note}）
    tm_session.add(
        TaskEvent(
            task_id=task.id,
            event_type="transferred",
            from_status="open",
            to_status="in_progress",
            actor="运营",
            note="改派给采购跟进",
            detail={"action": "assign", "target": "采购", "note": "改派给采购跟进"},
        )
    )
    # disagreed：人选择与 AI 建议不同时（detail 记两者，决策 27 分歧留痕）
    tm_session.add(
        TaskEvent(
            task_id=task.id,
            event_type="disagreed",
            actor="运营",
            note="不同意 AI 建议",
            detail={
                "ai_suggestion": {"action": "transfer", "target_domain": "erp"},
                "human_chose": {"action": "tag", "tags": ["跟进"]},
            },
        )
    )
    await tm_session.commit()

    res = await tm_session.execute(
        select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
    )
    events = res.scalars().all()
    types = [e.event_type for e in events]
    assert types == ["suggested", "transferred", "disagreed"]
    by_type = {e.event_type: e for e in events}
    assert by_type["suggested"].detail["action"] == "transfer"
    assert by_type["transferred"].detail["target"] == "采购"
    assert by_type["disagreed"].detail["ai_suggestion"]["target_domain"] == "erp"
    assert by_type["disagreed"].detail["human_chose"]["action"] == "tag"
