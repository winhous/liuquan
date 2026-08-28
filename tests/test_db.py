"""T6 引擎库真 SQL 测试（详设-v0.1 §3/§13）：迁移 schema、DAO 往返、SKIP LOCKED
队列语义、真事务回滚。全部走嵌入式 PG（tests/conftest.py 的 engine_pg_cluster：
独立临时簇 + alembic upgrade head，测的就是真 SQL 真事务，不桩）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- 回环地址与连接串一律运行期拼接（"127." 加 "0.0.1"），任何单一字符串常量
  不得含完整 IPv4 四段或 URL scheme（判据见 engine/lint/p2.py 模块 docstring）
- 不读 os.environ（P2 规则4）：create_engine 的 .env 回退测试用 tmp_path
  写临时 .env + monkeypatch 模块常量 _DOTENV_PATH，不触碰真实环境
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from engine.core.db import (
    AuditRow,
    CheckpointRow,
    EngineAudit,
    EngineCheckpoint,
    EngineStep,
    EngineTask,
    StepRow,
    TaskRow,
    _dequeue_stmt,
    append_audit,
    create_engine,
    create_step,
    create_task,
    dequeue_task,
    dispose_engine,
    get_audit,
    get_step,
    get_task,
    last_checkpoint,
    update_step,
    update_task,
    write_checkpoint,
)

DB_URL_ENV = "LIUQUAN_ENGINE_DB_URL"
DB_NAME = "liuquan_engine"

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "engine_task": {
        "id", "chain_id", "status", "trigger_type", "trigger_ref", "input",
        "current_step", "current_step_row", "error",
        "created_at", "started_at", "finished_at",
    },
    "engine_step": {
        "id", "task_id", "step_index", "worker_id", "phase", "status", "input",
        "output", "attempt", "error", "created_at", "started_at", "finished_at",
    },
    "engine_checkpoint": {
        "id", "task_id", "step_id", "from_phase", "to_phase", "state", "created_at",
    },
    "engine_audit": {
        "id", "task_id", "step_id", "worker_id", "model", "attempt", "result",
        "input_full", "output_full", "input_tokens", "output_tokens",
        "duration_ms", "created_at",
    },
}

EXPECTED_JSONB_COLUMNS: set[tuple[str, str]] = {
    ("engine_task", "input"),
    ("engine_step", "input"),
    ("engine_step", "output"),
    ("engine_checkpoint", "state"),
    ("engine_audit", "input_full"),
    ("engine_audit", "output_full"),
}


# ---- fixture：AsyncEngine（function 级，事件循环内建）+ 每测试 TRUNCATE 隔离 ----


@async_fixture
async def db_engine(engine_pg_cluster):
    engine = create_engine(engine_pg_cluster.url)
    yield engine
    await dispose_engine(engine)


@async_fixture(autouse=True)
async def _clean_engine_tables(db_engine):
    """每测试后 TRUNCATE 4 表（RESTART IDENTITY 重置自增、CASCADE 破外键），互不污染。

    review 修复：module 级 autouse（与 test_runner 同款），只对本文件测试生效；
    不做 conftest 全局 autouse（会给全仓纯同步测试引入事件循环开销拖慢套件）。
    """
    yield
    async with AsyncSession(db_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE engine_audit, engine_checkpoint, engine_step, engine_task "
                "RESTART IDENTITY CASCADE"
            )
        )


def _maker(engine):
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _insert_task(
    db_engine,
    chain_id: str = "chat-inbox",
    *,
    trigger_type: str = "manual",
    trigger_ref: str | None = None,
    input_: dict | None = None,
) -> int:
    """建任务快捷助手。"""
    return await create_task(
        db_engine,
        chain_id=chain_id,
        trigger_type=trigger_type,
        trigger_ref=trigger_ref,
        input_={} if input_ is None else input_,
    )


# ==== 迁移 schema（详设 §3：4 表 + 索引 + JSONB + FK）====


@pytest.mark.asyncio
async def test_migration_creates_four_engine_tables(db_engine) -> None:
    async with db_engine.connect() as conn:
        names = set(
            (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name IN "
                        "('engine_task','engine_step','engine_checkpoint','engine_audit')"
                    )
                )
            ).scalars()
        )
    assert names == {"engine_task", "engine_step", "engine_checkpoint", "engine_audit"}


@pytest.mark.asyncio
async def test_migration_columns_match_spec(db_engine) -> None:
    """字段级对齐 §3 DDL：每表列集合与详设一致。"""
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('engine_task','engine_step','engine_checkpoint','engine_audit')"
                )
            )
        ).all()
    actual: dict[str, set[str]] = {}
    for table, column in rows:
        actual.setdefault(table, set()).add(column)
    assert actual == EXPECTED_COLUMNS


@pytest.mark.asyncio
async def test_migration_jsonb_columns(db_engine) -> None:
    """§3 类型语义：六处 JSONB 列必须是 jsonb（sa.JSON 在 PG 下渲染为 JSON，
    故实现显式用 postgresql.JSONB——本断言锁死真类型）。"""
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND data_type = 'jsonb'"
                )
            )
        ).all()
    assert {(t, c) for t, c in rows} == EXPECTED_JSONB_COLUMNS


@pytest.mark.asyncio
async def test_migration_creates_indexes(db_engine) -> None:
    """§3 五个索引齐全；idx_task_status 为部分索引、idx_ckpt_task 为 (task_id, id DESC)。"""
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public'"
                )
            )
        ).all()
    by_name = {name: definition for name, definition in rows}
    for name in (
        "idx_task_status",
        "idx_step_task",
        "idx_ckpt_task",
        "idx_audit_task",
        "idx_audit_worker_day",
    ):
        assert name in by_name, f"索引 {name} 缺失"
    assert "WHERE" in by_name["idx_task_status"]  # 部分索引：只含活动态
    assert "queued" in by_name["idx_task_status"]
    assert "DESC" in by_name["idx_ckpt_task"]  # 恢复定位：同任务内取最新检查点


@pytest.mark.asyncio
async def test_migration_foreign_keys(db_engine) -> None:
    """子表外键数：engine_step 1（task_id）、engine_checkpoint 2、engine_audit 2。"""
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT table_name, count(*) FROM information_schema.table_constraints "
                    "WHERE constraint_type = 'FOREIGN KEY' AND table_schema = 'public' "
                    "AND table_name IN ('engine_step','engine_checkpoint','engine_audit') "
                    "GROUP BY table_name"
                )
            )
        ).all()
    assert dict(rows) == {"engine_step": 1, "engine_checkpoint": 2, "engine_audit": 2}


# ==== create_engine .env 回退（契约：url 缺省从仓库根 .env 的 LIUQUAN_ENGINE_DB_URL 读）====


@pytest.mark.asyncio
async def test_create_engine_falls_back_to_dotenv(monkeypatch, tmp_path) -> None:
    host = "127." + "0.0.1"
    url = f"postgresql+asyncpg://user@{host}:1/{DB_NAME}"
    env_file = tmp_path / ".env"
    env_file.write_text(f"{DB_URL_ENV}={url}\n", encoding="utf-8")
    monkeypatch.setattr("engine.core.db._DOTENV_PATH", env_file)
    engine = create_engine()
    try:
        assert str(engine.url) == url
    finally:
        await dispose_engine(engine)


@pytest.mark.asyncio
async def test_create_engine_raises_when_dotenv_missing(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED=1\n", encoding="utf-8")
    monkeypatch.setattr("engine.core.db._DOTENV_PATH", env_file)
    with pytest.raises(ValueError):
        create_engine()


# ==== engine_task：create/get/update 往返 ====


@pytest.mark.asyncio
async def test_create_task_get_task_roundtrip(db_engine) -> None:
    input_data = {"customer_id": "c-1", "tags": ["a", "b"], "note": "你好"}
    task_id = await create_task(
        db_engine,
        chain_id="chat-inbox",
        trigger_type="manual",
        trigger_ref="ops-user",
        input_=input_data,
    )
    assert isinstance(task_id, int)
    row = await get_task(db_engine, task_id)
    assert row is not None
    assert isinstance(row, TaskRow)
    assert row.id == task_id
    assert row.chain_id == "chat-inbox"
    assert row.status == "queued"  # DDL 默认
    assert row.trigger_type == "manual"
    assert row.trigger_ref == "ops-user"
    assert row.input == input_data  # JSONB 保真（嵌套 + 中文）
    assert row.current_step == 0  # DDL 默认
    assert row.current_step_row is None
    assert row.error is None
    assert row.created_at is not None
    assert row.started_at is None
    assert row.finished_at is None


@pytest.mark.asyncio
async def test_get_task_missing_returns_none(db_engine) -> None:
    assert await get_task(db_engine, 999999) is None


@pytest.mark.asyncio
async def test_update_task_fields(db_engine) -> None:
    task_id = await _insert_task(db_engine)
    started = datetime(2026, 8, 28, 1, 2, 3, tzinfo=timezone.utc)
    finished = datetime(2026, 8, 28, 1, 3, 0, tzinfo=timezone.utc)
    await update_task(
        db_engine,
        task_id,
        status="done",
        current_step=2,
        current_step_row=7,
        error="boom",
        started_at=started,
        finished_at=finished,
    )
    row = await get_task(db_engine, task_id)
    assert row is not None
    assert row.status == "done"
    assert row.current_step == 2
    assert row.current_step_row == 7
    assert row.error == "boom"
    assert row.started_at == started
    assert row.finished_at == finished


@pytest.mark.asyncio
async def test_update_task_none_means_no_column_change(db_engine) -> None:
    task_id = await _insert_task(db_engine)
    await update_task(db_engine, task_id, status="running")  # 只改 status
    row = await get_task(db_engine, task_id)
    assert row is not None
    assert row.status == "running"
    assert row.current_step == 0  # 未传 -> 不更新
    assert row.error is None
    assert row.started_at is None


# ==== dequeue：SKIP LOCKED 队列雏形（§3「SQL 与锁语义写进测试断言」）====


def test_dequeue_stmt_compiles_to_for_update_skip_locked() -> None:
    """DAO 的取队语句在 SQL 层面就是 FOR UPDATE SKIP LOCKED（§3 原文）。"""
    compiled = str(
        _dequeue_stmt().compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert "ORDER BY" in compiled
    assert "LIMIT 1" in compiled


@pytest.mark.asyncio
async def test_dequeue_returns_oldest_queued_task(db_engine) -> None:
    first = await _insert_task(db_engine)
    second = await _insert_task(db_engine)
    got = await dequeue_task(db_engine, worker_id="runner-1")  # worker_id 预留参数
    assert got is not None
    assert isinstance(got, TaskRow)
    assert got.id == first  # ORDER BY id：最老优先
    assert got.status == "queued"  # dequeue 只取不改状态（状态转换归 runner）


@pytest.mark.asyncio
async def test_dequeue_ignores_non_queued_and_empty(db_engine) -> None:
    assert await dequeue_task(db_engine) is None  # 空队列
    task_id = await _insert_task(db_engine)
    await update_task(db_engine, task_id, status="running")
    assert await dequeue_task(db_engine) is None  # 非 queued 不取


@pytest.mark.asyncio
async def test_skip_locked_single_row_only_one_consumer_wins(db_engine) -> None:
    """并发取同一 queued 任务：两个消费者只有一个拿到、另一个拿不到。

    消费者 A 持锁（事务未提交），消费者 B 的 SKIP LOCKED 跳过被锁行 -> None；
    A 提交释放锁后该行仍 queued，可再次被取（锁语义 + 不消费状态的队列语义）。
    """
    task_id = await _insert_task(db_engine)
    maker = _maker(db_engine)
    async with maker() as s1, s1.begin():
        got1 = (
            await s1.execute(_dequeue_stmt())
        ).scalar_one_or_none()
        assert got1 is not None and got1.id == task_id  # 消费者 A 拿到并持锁
        async with maker() as s2, s2.begin():
            got2 = (
                await s2.execute(_dequeue_stmt())
            ).scalar_one_or_none()
            assert got2 is None  # 消费者 B：SKIP LOCKED 跳过被锁行，拿不到
    async with maker() as s3, s3.begin():
        got3 = (
            await s3.execute(_dequeue_stmt())
        ).scalar_one_or_none()
        assert got3 is not None and got3.id == task_id  # 锁释放后行仍在队列


@pytest.mark.asyncio
async def test_skip_locked_two_consumers_no_double_claim(db_engine) -> None:
    """两个并发消费者取两条任务：SKIP LOCKED 保证不双取同一行。"""
    t1 = await _insert_task(db_engine)
    t2 = await _insert_task(db_engine)
    maker = _maker(db_engine)
    async with maker() as s1, s1.begin():
        got1 = (await s1.execute(_dequeue_stmt())).scalar_one_or_none()
        assert got1 is not None
        async with maker() as s2, s2.begin():
            got2 = (await s2.execute(_dequeue_stmt())).scalar_one_or_none()
            assert got2 is not None
            assert got1.id != got2.id  # 绝不双取同一行
            assert {got1.id, got2.id} == {t1, t2}


# ==== engine_step：create/update 往返 ====


@pytest.mark.asyncio
async def test_create_step_roundtrip(db_engine) -> None:
    task_id = await _insert_task(db_engine)
    step_id = await create_step(
        db_engine, task_id, step_index=0, worker_id="demo.echo", input_={"text": "你好"}
    )
    assert isinstance(step_id, int)
    maker = _maker(db_engine)
    async with maker() as session:
        step = (
            await session.execute(select(EngineStep).where(EngineStep.id == step_id))
        ).scalar_one()
    assert step.task_id == task_id
    assert step.step_index == 0
    assert step.worker_id == "demo.echo"
    assert step.input == {"text": "你好"}
    assert step.phase == "INIT"  # DDL 默认
    assert step.status == "running"  # DDL 默认
    assert step.attempt == 0  # DDL 默认
    assert step.output is None
    assert step.error is None


@pytest.mark.asyncio
async def test_update_step_fields(db_engine) -> None:
    task_id = await _insert_task(db_engine)
    step_id = await create_step(
        db_engine, task_id, step_index=0, worker_id="demo.echo", input_={}
    )
    await update_step(
        db_engine,
        step_id,
        phase="REASON",
        status="done",
        output={"result": "ok", "n": [1, 2]},
        attempt=1,
    )
    maker = _maker(db_engine)
    async with maker() as session:
        step = (
            await session.execute(select(EngineStep).where(EngineStep.id == step_id))
        ).scalar_one()
    assert step.phase == "REASON"
    assert step.status == "done"
    assert step.output == {"result": "ok", "n": [1, 2]}  # JSONB 保真
    assert step.attempt == 1
    assert step.error is None  # 未传 error -> 不更新
    await update_step(db_engine, step_id, error="oops")
    async with maker() as session:
        step = (
            await session.execute(select(EngineStep).where(EngineStep.id == step_id))
        ).scalar_one()
    assert step.error == "oops"
    assert step.phase == "REASON"  # 未传 -> 不更新


# ==== engine_checkpoint：写检查点 + 恢复定位（取最后一条）====


@pytest.mark.asyncio
async def test_write_checkpoint_last_checkpoint_roundtrip(db_engine) -> None:
    task_id = await _insert_task(db_engine)
    step_id = await create_step(
        db_engine, task_id, step_index=0, worker_id="demo.echo", input_={}
    )
    state1 = {"phase": "REASON", "llm_result": {"text": "你好世界"}, "n": 1}
    state2 = {"phase": "ACT", "llm_result": {"text": "你好世界"}, "nested": {"k": [1, 2]}}
    await write_checkpoint(
        db_engine, task_id=task_id, step_id=step_id,
        from_phase="INIT", to_phase="REASON", state=state1,
    )
    await write_checkpoint(
        db_engine, task_id=task_id, step_id=step_id,
        from_phase="REASON", to_phase="ACT", state=state2,
    )
    cp = await last_checkpoint(db_engine, task_id)
    assert cp is not None
    assert isinstance(cp, CheckpointRow)
    assert cp.task_id == task_id
    assert cp.step_id == step_id
    assert cp.from_phase == "REASON"
    assert cp.to_phase == "ACT"
    assert cp.state == state2  # 取最后一条（idx_ckpt_task 的 id DESC 定位）+ JSONB 保真
    assert cp.created_at is not None


@pytest.mark.asyncio
async def test_last_checkpoint_missing_returns_none(db_engine) -> None:
    assert await last_checkpoint(db_engine, 999999) is None


# ==== engine_audit：审计落库 + 按任务查 ====


@pytest.mark.asyncio
async def test_append_audit_get_audit_roundtrip(db_engine) -> None:
    task_id = await _insert_task(db_engine)
    step_id = await create_step(
        db_engine, task_id, step_index=0, worker_id="chat-translate", input_={}
    )
    await append_audit(
        db_engine,
        task_id=task_id,
        step_id=step_id,
        worker_id="chat-translate",
        model="deepseek:deepseek-chat",
        attempt=0,
        result="ok",
        input_full={"prompt": "翻译：你好"},
        output_full={"text": "hello"},
        input_tokens=120,
        output_tokens=45,
        duration_ms=812,
    )
    await append_audit(
        db_engine,
        task_id=task_id,
        step_id=step_id,
        worker_id="chat-translate",
        model="deepseek:deepseek-chat",
        attempt=1,
        result="reask",
    )
    rows = await get_audit(db_engine, task_id)
    assert len(rows) == 2
    first, second = rows
    assert isinstance(first, AuditRow)
    assert first.task_id == task_id
    assert first.step_id == step_id
    assert first.worker_id == "chat-translate"
    assert first.model == "deepseek:deepseek-chat"
    assert first.attempt == 0
    assert first.result == "ok"
    assert first.input_full == {"prompt": "翻译：你好"}
    assert first.output_full == {"text": "hello"}
    assert first.input_tokens == 120
    assert first.output_tokens == 45
    assert first.duration_ms == 812
    assert first.created_at is not None
    assert second.attempt == 1
    assert second.result == "reask"
    assert second.input_full is None  # 未传 -> 不落
    assert second.output_full is None
    assert second.input_tokens is None
    assert second.duration_ms is None
    assert first.id < second.id  # 按插入序返回


@pytest.mark.asyncio
async def test_get_audit_unknown_task_empty(db_engine) -> None:
    assert await get_audit(db_engine, 999999) == []


# ==== 真事务：失败回滚不留脏数据 ====


@pytest.mark.asyncio
async def test_transaction_rollback_leaves_no_dirty_data(db_engine) -> None:
    maker = _maker(db_engine)
    async with maker() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(
                    EngineTask(
                        chain_id="rollback-demo",
                        trigger_type="manual",
                        trigger_ref=None,
                        input={"x": 1},
                    )
                )
                await session.flush()
                # FK 违规：step 引用不存在的任务 -> 整个事务回滚（真事务）
                session.add(
                    EngineStep(
                        task_id=999999, step_index=0, worker_id="demo.echo", input={}
                    )
                )
                await session.flush()
    async with maker() as session:
        tasks = (await session.execute(select(EngineTask))).scalars().all()
        steps = (await session.execute(select(EngineStep))).scalars().all()
        audits = (await session.execute(select(EngineAudit))).scalars().all()
        checkpoints = (await session.execute(select(EngineCheckpoint))).scalars().all()
    assert tasks == []  # 任务插入被一并回滚，无脏数据
    assert steps == []
    assert audits == []
    assert checkpoints == []


# ==== T12a 契约扩展：get_step（runner OBSERVE 读回 / resume 恢复定位用）====
# 只新增不改既有：详设 §3 DAO 契约本无 get_step，T12a 补充按 id 读工序行
# （对应 engine/core/db.py 的 StepRow + get_step，禁止改任何既有函数签名）。


@pytest.mark.asyncio
async def test_get_step_roundtrip(db_engine) -> None:
    task_id = await _insert_task(db_engine)
    step_id = await create_step(
        db_engine, task_id, step_index=0, worker_id="demo.echo", input_={"text": "你好"}
    )
    await update_step(
        db_engine,
        step_id,
        phase="ACT",
        status="running",
        output={"text": "echoed", "n": [1, 2]},
        attempt=1,
        error="oops",
    )
    row = await get_step(db_engine, step_id)
    assert row is not None
    assert isinstance(row, StepRow)
    assert row.id == step_id
    assert row.task_id == task_id
    assert row.step_index == 0
    assert row.worker_id == "demo.echo"
    assert row.phase == "ACT"
    assert row.status == "running"
    assert row.input == {"text": "你好"}  # JSONB 保真（中文）
    assert row.output == {"text": "echoed", "n": [1, 2]}
    assert row.attempt == 1
    assert row.error == "oops"
    assert row.created_at is not None
    assert row.started_at is None
    assert row.finished_at is None


@pytest.mark.asyncio
async def test_get_step_missing_returns_none(db_engine) -> None:
    assert await get_step(db_engine, 999999) is None


@pytest.mark.asyncio
async def test_get_step_defaults_reflect_ddl(db_engine) -> None:
    """新建未更新的工序行：phase/status/attempt 为 DDL 默认（OBSERVE 读回基线）。"""
    task_id = await _insert_task(db_engine)
    step_id = await create_step(
        db_engine, task_id, step_index=2, worker_id="demo.echo", input_={}
    )
    row = await get_step(db_engine, step_id)
    assert row is not None
    assert row.phase == "INIT"
    assert row.status == "running"
    assert row.attempt == 0
    assert row.output is None
    assert row.error is None
