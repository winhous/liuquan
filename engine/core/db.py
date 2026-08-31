"""引擎库数据访问层（T6；详设-v0.1 §3 引擎库 4 表 + §3 队列雏形 SKIP LOCKED）。

- ORM 模型（Base + 5 映射类）与迁移同源：字段/类型/默认值/索引与
  migrations/engine/versions/ 的 DDL 逐列对齐（JSONB 显式用 postgresql.JSONB——
  sa.JSON 在 PG 方言下渲染为 JSON 而非 JSONB，见 §3 类型语义，schema 测试锁死）。
- DAO 全部 async（async_sessionmaker + AsyncSession，T3 技术定：引擎 asyncio 化）；
  每个函数一次调用 = 一个事务，成功即提交。
- 队列雏形：dequeue_task 用 SELECT ... FOR UPDATE SKIP LOCKED 取 queued 任务，
  只取不改状态（状态转换归 runner），worker_id 参数 v0.1 预留。
- v0.4 扩展：Schedule ORM + DAO（详设 §5/§9.1），next_run_time 纯代码计算。

接口契约由主代理技术定（T9 checkpoint/audit、T12 runner 均依赖），不得擅改。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import NamedTuple

from dotenv import dotenv_values

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Select,
    Text,
    desc,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 引擎库连接串变量名（R20：值只存 .env；本模块经 dotenv_values 读仓库根 .env
# 文件，不触碰 os.environ——P2 规则4 的合法来源即「.env 文件」）
_DB_URL_ENV = "LIUQUAN_ENGINE_DB_URL"

# 仓库根 .env 路径（engine/core/db.py 上溯两级；不依赖 cwd）
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOTENV_PATH = _REPO_ROOT / ".env"


class Base(DeclarativeBase):
    """引擎库 ORM 基类（与 Alembic env.py 的 target_metadata 同源）。"""


class EngineTask(Base):
    """engine_task：一次触发 = 一条，一条 = 一条链的完整执行（详设 §3）。"""

    __tablename__ = "engine_task"
    __table_args__ = (
        Index(
            "idx_task_status",
            "status",
            postgresql_where=text("status IN ('queued','running','paused')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chain_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'queued'")
    )
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_ref: Mapped[str | None] = mapped_column(Text)
    input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    current_step: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("'0'")
    )
    current_step_row: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EngineStep(Base):
    """engine_step：工序实例，每次执行一条（重试 = 新行，attempt+1）。"""

    __tablename__ = "engine_step"
    __table_args__ = (Index("idx_step_task", "task_id", "step_index"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("engine_task.id"), nullable=False, index=False
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'INIT'")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'running'")
    )
    input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    output: Mapped[dict | None] = mapped_column(JSONB)
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("'0'")
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EngineCheckpoint(Base):
    """engine_checkpoint：相位转换前落盘，崩溃/暂停恢复的依据（§2.3）。"""

    __tablename__ = "engine_checkpoint"
    __table_args__ = (
        Index("idx_ckpt_task", "task_id", desc("id")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("engine_task.id"), nullable=False
    )
    step_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("engine_step.id"), nullable=False
    )
    from_phase: Mapped[str] = mapped_column(Text, nullable=False)
    to_phase: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EngineAudit(Base):
    """engine_audit：每次 LLM 调用一行（ok/reask/failed 都记，§7.3/§8）。"""

    __tablename__ = "engine_audit"
    __table_args__ = (
        Index("idx_audit_task", "task_id"),
        Index("idx_audit_worker_day", "worker_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("engine_task.id"), nullable=False
    )
    step_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("engine_step.id"), nullable=False
    )
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("'0'")
    )
    result: Mapped[str] = mapped_column(Text, nullable=False)
    input_full: Mapped[dict | None] = mapped_column(JSONB)
    output_full: Mapped[dict | None] = mapped_column(JSONB)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Schedule(Base):
    """schedule：定时触发底座（v0.4 详设 §5）。

    每行 = 一条定时链配置；enabled 开关；cron 限定 daily 形（"M H * * *"）；
    last_run_at/next_run_at 为幂等锚点（调度器维护）。
    ORM 与迁移 0002 逐列同源（R22）。
    """

    __tablename__ = "schedule"
    __table_args__ = (
        CheckConstraint(
            "length(name) BETWEEN 1 AND 100",
            name="chk_schedule_name",
        ),
        CheckConstraint(
            "last_status IN ('','done','failed','skipped')",
            name="chk_schedule_last_status",
        ),
        Index("idx_schedule_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chain_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    cron: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'0 7 * * *'")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---- Row 返回类型（NamedTuple；TaskRow/CheckpointRow/AuditRow/ScheduleRow 契约）----


class TaskRow(NamedTuple):
    id: int
    chain_id: str
    status: str
    trigger_type: str
    trigger_ref: str | None
    input: dict
    current_step: int
    current_step_row: int | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class CheckpointRow(NamedTuple):
    id: int
    task_id: int
    step_id: int
    from_phase: str
    to_phase: str
    state: dict
    created_at: datetime


class StepRow(NamedTuple):
    """engine_step 行（T12a 契约扩展，对齐详设 §3 表结构；供 runner OBSERVE 读回）。"""

    id: int
    task_id: int
    step_index: int
    worker_id: str
    phase: str
    status: str
    input: dict
    output: dict | None
    attempt: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AuditRow(NamedTuple):
    id: int
    task_id: int
    step_id: int
    worker_id: str
    model: str
    attempt: int
    result: str
    input_full: dict | None
    output_full: dict | None
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    created_at: datetime


@dataclass
class ScheduleRow:
    """schedule 行（v0.4 详设 §5；R22 同源，对齐 Schedule ORM）。"""

    id: int
    chain_id: str
    name: str
    cron: str
    enabled: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_status: str
    created_at: datetime
    updated_at: datetime


# ---- 队列取队语句（§3「SELECT ... FOR UPDATE SKIP LOCKED」；测试同源断言）----


def _dequeue_stmt() -> Select[tuple[EngineTask]]:
    """取最老一条 queued 任务并锁行（SKIP LOCKED：并发消费者跳过被锁行）。"""
    return (
        select(EngineTask)
        .where(EngineTask.status == "queued")
        .order_by(EngineTask.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


# ---- 引擎创建/销毁 ----


def create_engine(url: str | None = None) -> AsyncEngine:
    """建 AsyncEngine；url 缺省从仓库根 .env 的 LIUQUAN_ENGINE_DB_URL 读（未设报错）。

    连接串是敏感值（含密码）：R20 规定值只存 .env；本函数经 dotenv_values
    读 .env 文件（P2 规则4 的合法来源），不触碰 os.environ——P2 对 db.py
    的 URL/IP 字面量检查保持生效（禁止连接串硬编码进代码）。
    """
    if url is None:
        url = dotenv_values(_DOTENV_PATH).get(_DB_URL_ENV)
        if not url:
            raise ValueError(
                f".env 的 {_DB_URL_ENV} 未设置：create_engine 需要引擎库连接串"
                "（规范 R20：值只存 .env，cp .env.example .env 后填入）"
            )
    return create_async_engine(url)


async def dispose_engine(engine: AsyncEngine) -> None:
    """释放连接池。"""
    await engine.dispose()


# ---- engine_task ----


async def create_task(
    engine: AsyncEngine,
    *,
    chain_id: str,
    trigger_type: str,
    trigger_ref: str | None,
    input_: dict,
) -> int:
    """建任务入队（status 由 DDL 默认 queued），返回 task_id。"""
    async with _sessions(engine)() as session, session.begin():
        task = EngineTask(
            chain_id=chain_id,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            input=input_,
        )
        session.add(task)
        await session.flush()
        return task.id


async def get_task(engine: AsyncEngine, task_id: int) -> TaskRow | None:
    """按 task_id 查任务；不存在返回 None。"""
    async with _sessions(engine)() as session:
        task = await session.get(EngineTask, task_id)
        return _task_row(task) if task is not None else None


async def dequeue_task(
    engine: AsyncEngine, *, worker_id: str | None = None
) -> TaskRow | None:
    """取队：SELECT ... FOR UPDATE SKIP LOCKED 取最老 queued 任务（§3 队列雏形）。

    只取不改状态（状态转换归 runner）；worker_id 参数 v0.1 预留（不落库）。
    """
    async with _sessions(engine)() as session, session.begin():
        task = (await session.execute(_dequeue_stmt())).scalar_one_or_none()
        return _task_row(task) if task is not None else None


async def claim_task(
    engine: AsyncEngine, *, worker_id: str | None = None
) -> TaskRow | None:
    """原子认领最老一条 queued 任务（v0.2 常驻 server 队列消费；详设-v0.2 §2.3）。

    与 dequeue_task 的区别：认领事务内把行置 running（SELECT ... FOR UPDATE
    SKIP LOCKED + UPDATE 同事务）——双消费者不会重取同一任务（变更日志 M4：
    v0.1 dequeue 只取不改，双消费者可重取同一任务；v0.2 常驻 runner 需原子
    认领，本函数即该落地，测试断言锁死）。worker_id 参数预留（不落库）。
    """
    async with _sessions(engine)() as session, session.begin():
        task = (await session.execute(_dequeue_stmt())).scalar_one_or_none()
        if task is None:
            return None
        task.status = "running"
        task.started_at = datetime.now(timezone.utc)
        await session.flush()
        return _task_row(task)


async def update_task(
    engine: AsyncEngine,
    task_id: int,
    *,
    status: str | None = None,
    current_step: int | None = None,
    current_step_row: int | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    """更新任务列；None = 不更新该列。"""
    values: dict = {}
    if status is not None:
        values[EngineTask.status] = status
    if current_step is not None:
        values[EngineTask.current_step] = current_step
    if current_step_row is not None:
        values[EngineTask.current_step_row] = current_step_row
    if error is not None:
        values[EngineTask.error] = error
    if started_at is not None:
        values[EngineTask.started_at] = started_at
    if finished_at is not None:
        values[EngineTask.finished_at] = finished_at
    if not values:
        return
    async with _sessions(engine)() as session, session.begin():
        await session.execute(
            update(EngineTask).where(EngineTask.id == task_id).values(values)
        )


# ---- engine_step ----


async def create_step(
    engine: AsyncEngine,
    task_id: int,
    *,
    step_index: int,
    worker_id: str,
    input_: dict,
) -> int:
    """建工序实例（phase/status/attempt 由 DDL 默认），返回 step_id。"""
    async with _sessions(engine)() as session, session.begin():
        step = EngineStep(
            task_id=task_id,
            step_index=step_index,
            worker_id=worker_id,
            input=input_,
        )
        session.add(step)
        await session.flush()
        return step.id


async def update_step(
    engine: AsyncEngine,
    step_id: int,
    *,
    phase: str | None = None,
    status: str | None = None,
    output: dict | None = None,
    attempt: int | None = None,
    error: str | None = None,
) -> None:
    """更新工序列；None = 不更新该列。"""
    values: dict = {}
    if phase is not None:
        values[EngineStep.phase] = phase
    if status is not None:
        values[EngineStep.status] = status
    if output is not None:
        values[EngineStep.output] = output
    if attempt is not None:
        values[EngineStep.attempt] = attempt
    if error is not None:
        values[EngineStep.error] = error
    if not values:
        return
    async with _sessions(engine)() as session, session.begin():
        await session.execute(
            update(EngineStep).where(EngineStep.id == step_id).values(values)
        )


async def get_step(engine: AsyncEngine, step_id: int) -> StepRow | None:
    """按 step_id 查工序实例；不存在返回 None。

    T12a 契约扩展（只新增不改既有）：详设 §3 的 DAO 契约本无 get_step，
    runner 的 OBSERVE 相位（v0.1 语义 = 读回 engine_step.output 确认副作用
    已落库）与 resume 恢复定位需要按 id 读工序行，故补充本函数 + StepRow
    类型；不触碰任何既有函数签名。
    """
    async with _sessions(engine)() as session:
        step = await session.get(EngineStep, step_id)
        return _step_row(step) if step is not None else None


async def list_steps(engine: AsyncEngine, task_id: int) -> list[StepRow]:
    """任务全部步骤（v0.3：GET /api/engine/tasks/{id} 的 steps_output，供 web 取
    链各步骤输出——译文/快照在中间步骤，apply 落库需要）。"""
    async with AsyncSession(engine) as session:
        rows = (
            (
                await session.execute(
                    select(EngineStep)
                    .where(EngineStep.task_id == task_id)
                    .order_by(EngineStep.step_index.asc(), EngineStep.id.asc())
                )
            )
            .scalars()
            .all()
        )
    return [_step_row(r) for r in rows]


async def get_last_step(engine: AsyncEngine, task_id: int) -> StepRow | None:
    """取该任务最后一条工序实例（链产物定位：末步 output = 链产物，v0.2 T5）。

    按 step_index 倒序取最后一条（重试 = 新行的语义下同 index 取后建者）。
    """
    async with _sessions(engine)() as session:
        step = (
            await session.execute(
                select(EngineStep)
                .where(EngineStep.task_id == task_id)
                .order_by(EngineStep.step_index.desc(), EngineStep.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _step_row(step) if step is not None else None


async def list_tasks_by_status(engine: AsyncEngine, status: str) -> list[TaskRow]:
    """按状态列任务（插入序；v0.2 常驻 server 崩溃恢复：找遗留 running 任务）。"""
    async with _sessions(engine)() as session:
        rows = (
            await session.execute(
                select(EngineTask)
                .where(EngineTask.status == status)
                .order_by(EngineTask.id)
            )
        ).scalars()
        return [_task_row(t) for t in rows]


# ---- engine_checkpoint（恢复定位：取该任务最后一条）----


async def write_checkpoint(
    engine: AsyncEngine,
    *,
    task_id: int,
    step_id: int,
    from_phase: str,
    to_phase: str,
    state: dict,
) -> None:
    """相位转换前落检查点（§2.3：续跑所需最小状态）。"""
    async with _sessions(engine)() as session, session.begin():
        session.add(
            EngineCheckpoint(
                task_id=task_id,
                step_id=step_id,
                from_phase=from_phase,
                to_phase=to_phase,
                state=state,
            )
        )


async def last_checkpoint(engine: AsyncEngine, task_id: int) -> CheckpointRow | None:
    """取该任务最后一条检查点（恢复定位用）；无则 None。"""
    async with _sessions(engine)() as session:
        cp = (
            await session.execute(
                select(EngineCheckpoint)
                .where(EngineCheckpoint.task_id == task_id)
                .order_by(EngineCheckpoint.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _checkpoint_row(cp) if cp is not None else None


# ---- engine_audit（每次 LLM 调用一行，审计永久保留 §8）----


async def append_audit(
    engine: AsyncEngine,
    *,
    task_id: int,
    step_id: int,
    worker_id: str,
    model: str,
    attempt: int,
    result: str,
    input_full: dict | None = None,
    output_full: dict | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
) -> None:
    """审计落库（先审计后调用 §7.3：审计不可写 = 调用不允许发生）。"""
    async with _sessions(engine)() as session, session.begin():
        session.add(
            EngineAudit(
                task_id=task_id,
                step_id=step_id,
                worker_id=worker_id,
                model=model,
                attempt=attempt,
                result=result,
                input_full=input_full,
                output_full=output_full,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
            )
        )


async def get_audit(engine: AsyncEngine, task_id: int) -> list[AuditRow]:
    """按任务查全部审计条目（插入序）。"""
    async with _sessions(engine)() as session:
        rows = (
            await session.execute(
                select(EngineAudit)
                .where(EngineAudit.task_id == task_id)
                .order_by(EngineAudit.id)
            )
        ).scalars()
        return [_audit_row(a) for a in rows]


# ---- schedule（v0.4 详设 §5/§9.1）----


async def list_schedules(engine: AsyncEngine) -> list[ScheduleRow]:
    """查全部定时链（插入序）。"""
    async with _sessions(engine)() as session:
        rows = (
            await session.execute(
                select(Schedule).order_by(Schedule.id)
            )
        ).scalars()
        return [_schedule_row(s) for s in rows]


async def get_schedule(engine: AsyncEngine, schedule_id: int) -> ScheduleRow | None:
    """按 id 查定时链；不存在返回 None。"""
    async with _sessions(engine)() as session:
        s = await session.get(Schedule, schedule_id)
        return _schedule_row(s) if s is not None else None


async def create_schedule(
    engine: AsyncEngine,
    *,
    chain_id: str,
    name: str,
    cron: str = "0 7 * * *",
    enabled: bool = True,
) -> int:
    """建定时链，返回 schedule_id。"""
    async with _sessions(engine)() as session, session.begin():
        s = Schedule(
            chain_id=chain_id,
            name=name,
            cron=cron,
            enabled=enabled,
        )
        session.add(s)
        await session.flush()
        return s.id


async def update_schedule(
    engine: AsyncEngine,
    schedule_id: int,
    *,
    name: str | None = None,
    cron: str | None = None,
    enabled: bool | None = None,
    next_run_at: datetime | None = None,
    set_next_run_at: bool = False,
) -> None:
    """更新定时链列；None = 不更新该列（只更新传入字段）。

    next_run_at 语义（避免与 name 等「None=不动」混淆）：set_next_run_at=False
    时忽略 next_run_at；set_next_run_at=True 时把 next_run_at 写为该值（可写
    None 清空）。供调度器初始化 NULL 锚点（详设 §9.3）。
    """
    values: dict = {}
    if name is not None:
        values[Schedule.name] = name
    if cron is not None:
        values[Schedule.cron] = cron
    if enabled is not None:
        values[Schedule.enabled] = enabled
    if set_next_run_at:
        values[Schedule.next_run_at] = next_run_at
    if not values:
        return
    values[Schedule.updated_at] = func.now()
    async with _sessions(engine)() as session, session.begin():
        await session.execute(
            update(Schedule).where(Schedule.id == schedule_id).values(values)
        )


async def mark_schedule_run(
    engine: AsyncEngine,
    schedule_id: int,
    *,
    last_run_at: datetime,
    next_run_at: datetime,
) -> None:
    """推进锚点：last_run_at/next_run_at 更新，last_status 清为空串。"""
    async with _sessions(engine)() as session, session.begin():
        await session.execute(
            update(Schedule)
            .where(Schedule.id == schedule_id)
            .values(
                last_run_at=last_run_at,
                next_run_at=next_run_at,
                last_status="",
                updated_at=func.now(),
            )
        )


async def ensure_seed_schedules(engine: AsyncEngine) -> None:
    """幂等种子：schedule 表为空时插入 crm_reminder_chain（详设 §5）；非空不插。"""
    async with _sessions(engine)() as session:
        count = (await session.execute(select(func.count(Schedule.id)))).scalar_one()
    if count == 0:
        await create_schedule(
            engine,
            chain_id="crm_reminder_chain",
            name="CRM 未跟进提醒",
            cron="0 7 * * *",
            enabled=True,
        )


def next_run_time(cron: str, after: datetime) -> datetime:
    """纯代码计算下一次每天 HH:MM 的 UTC 时刻（详设 §5）。

    cron 限定 daily 形「M H * * *」：第 0/1 段为分/时数字，第 2-4 段为 *。
    返回 after 之后下一个每天 HH:MM 的 UTC 时刻。
    边界：after 正好等于某天 HH:MM 时取下一个；跨天正常。
    不引第三方 cron 库。
    """
    parts = cron.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron 必须 5 段，实际 {len(parts)}：{cron!r}")
    minute_str, hour_str = parts[0], parts[1]
    if not (minute_str.isdigit() and hour_str.isdigit()):
        raise ValueError(
            f"daily cron 第 0/1 段须为数字，实际 minute={minute_str!r} hour={hour_str!r}"
        )
    if parts[2] != "*" or parts[3] != "*" or parts[4] != "*":
        raise ValueError(
            f"v0.4 仅支持 daily cron（第 2-4 段均为 *），实际：{cron!r}"
        )
    minute, hour = int(minute_str), int(hour_str)
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        raise ValueError(f"minute/hour 越界：minute={minute} hour={hour}")

    # 确保 after 是 UTC naive 用于计算
    after_utc = after.replace(tzinfo=None) if after.tzinfo else after
    # 今天 HH:MM
    target = after_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target > after_utc:
        return target.replace(tzinfo=timezone.utc)
    # after 正好等于或晚于今天 HH:MM → 取明天
    return (target + timedelta(days=1)).replace(tzinfo=timezone.utc)


# ---- 内部助手 ----


def _sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """每次调用一个 sessionmaker；expire_on_commit=False 保证返回行提交后可读。"""
    return async_sessionmaker(engine, expire_on_commit=False)


def _task_row(task: EngineTask) -> TaskRow:
    return TaskRow(
        id=task.id,
        chain_id=task.chain_id,
        status=task.status,
        trigger_type=task.trigger_type,
        trigger_ref=task.trigger_ref,
        input=task.input,
        current_step=task.current_step,
        current_step_row=task.current_step_row,
        error=task.error,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


def _step_row(step: EngineStep) -> StepRow:
    return StepRow(
        id=step.id,
        task_id=step.task_id,
        step_index=step.step_index,
        worker_id=step.worker_id,
        phase=step.phase,
        status=step.status,
        input=step.input,
        output=step.output,
        attempt=step.attempt,
        error=step.error,
        created_at=step.created_at,
        started_at=step.started_at,
        finished_at=step.finished_at,
    )


def _checkpoint_row(cp: EngineCheckpoint) -> CheckpointRow:
    return CheckpointRow(
        id=cp.id,
        task_id=cp.task_id,
        step_id=cp.step_id,
        from_phase=cp.from_phase,
        to_phase=cp.to_phase,
        state=cp.state,
        created_at=cp.created_at,
    )


def _audit_row(a: EngineAudit) -> AuditRow:
    return AuditRow(
        id=a.id,
        task_id=a.task_id,
        step_id=a.step_id,
        worker_id=a.worker_id,
        model=a.model,
        attempt=a.attempt,
        result=a.result,
        input_full=a.input_full,
        output_full=a.output_full,
        input_tokens=a.input_tokens,
        output_tokens=a.output_tokens,
        duration_ms=a.duration_ms,
        created_at=a.created_at,
    )


def _schedule_row(s: Schedule) -> ScheduleRow:
    return ScheduleRow(
        id=s.id,
        chain_id=s.chain_id,
        name=s.name,
        cron=s.cron,
        enabled=s.enabled,
        last_run_at=s.last_run_at,
        next_run_at=s.next_run_at,
        last_status=s.last_status,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )
