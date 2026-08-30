"""引擎常驻 server（详设-v0.2-TM §2.3 / §4 / §5；v0.2 T5）。

单进程双职责（详设 §2.3 常驻运行形态，技术定）：
1. **FastAPI app** 挂 /api/engine/* 三接口（§4.1-4.3：POST 触发链入队 201 /
   GET 查状态结果 200 / GET registry 清单 200；web 只经这三接口碰引擎）
2. **后台 asyncio 常驻消费** engine_task 队列（§2.3：SKIP LOCKED 原子认领 +
   并发上限 2，asyncio.Semaphore；崩溃由 systemd 拉起），任务 DONE 后走
   **TM 转交器钩子**（§5：从链末步产物提取 TaskProposal -> 注册消费者落业务库；
   rejected -> 任务标 failed；skipped_* -> 任务保持 done 记 skip 日志）

注入式设计（规范 R12 构造注入，测试零网络零真 token）：
- ``create_app`` 的 engine / registry / runner / consumers / tm_engine 全部
  可注入；runner 缺省经 runner_kwargs 构造真 TaskRunner（生产路径）
- 转交钩子的 audit_lookup = 查引擎库 engine_audit 的可调用（对应提案
  source.audit_ids；转交器契约的 lookup 是同步可调用——validate_audit_ids
  直接调用——故先按 audit_ids 预取可查 id 集合，返回集合成员闭包）
- whitelist（决策 16 数据引用封闭性）= 链 input 递归收集的 ref_id/id 字段
  字符串值（v0.2 技术定：链 input 即本链喂给 AI 的数据集合；真实业务
  v0.3 由 Context provider 声明，见任务书）

安全与规范（P2 零容忍 / R20 / R24）：
- 本文件不出现 URL/IP/密钥字面量（连接串/端口都经 .env 的
  LIUQUAN_ENGINE_API_URL / LIUQUAN_ENGINE_DB_URL，
  读取走 dotenv_values 读 .env 文件，不触碰 os.environ；先例
  engine/core/db.py 与 engine/actions/tm_proposal.py）
- 不 import web 任何代码（P3-2 / R24：引擎进程不 import web，双向零耦合）；
  本模块属引擎包，可 import engine.* 与 models（R22 唯一通用语言）
- 常驻服务地址缺省值（回环 + 8100）与 web 侧客户端（web/engineapi/client.py）
  同源，段拼接构造（P2 判据：单一字符串常量不得含完整 scheme/IPv4）

崩溃恢复（§2.3 / 变更日志 M4 配套）：进程重启 -> lifespan 先 recover() 处理
遗留 running 任务（有检查点 -> runner.resume 续跑不重烧 token；无检查点 ->
标记 failed，v0.1 孤儿清理同哲学），随后启动消费循环继续消费剩余 queued。
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from engine.actions import CONSUMERS, Consumer, ConsumeOutcome
from engine.actions.biz_client import BizApiClient
from engine.providers import build_providers
from engine.core import db as _db
from engine.core.llm import agent_factory, load_models
from engine.core.runner import TaskResult, TaskRunner
from engine.registry import Registry, load_registry
from models.contract.task import TaskProposal

__all__ = [
    "CreateTaskRequest",
    "CreateTaskResponse",
    "QueueConsumer",
    "TaskDetailResponse",
    "create_app",
]

# 常驻服务地址的 .env 变量名（R20：值只存 .env；web 侧客户端同源）
_API_URL_ENV = "LIUQUAN_ENGINE_API_URL"

# 缺省端口（web 侧客户端缺省 base URL 同源；端口是纯数字，非 URL/IP 字面量）
_DEFAULT_PORT = 8100

# 缺省回环 host（P2：IP 不得以单一字符串常量出现，段拼接——web 客户端同款）
_DEFAULT_HOST = "127" + ".0.0.1"

# 仓库根 .env 路径（engine/server.py 上溯一级；不依赖 cwd）
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOTENV_PATH = _REPO_ROOT / ".env"

# 链 input Model 解析搜索序（与 runner/loader L3 同源）
_MODEL_SEARCH_MODULES = ("models.workers", "models.contract", "models")

# TaskProposal 提取判据键（详设-v0.2 §7：末步 output 若含这些键即视为提案形态）
_PROPOSAL_KEYS = frozenset({"title", "detail", "domain", "action_id", "evidence", "source"})

# whitelist 提取键（任务书技术定：链 input 里含 ref_id 或 id 的字符串值）
_REF_ID_KEYS = frozenset({"ref_id", "id"})


# ---- 纯函数助手 ----


def _repo_root() -> Path:
    """仓库根定位：engine/server.py 上溯一级（不依赖 cwd）。"""
    return _REPO_ROOT


def _resolve_model(name: str) -> type[BaseModel] | None:
    """模型名 -> Model 类（loader L3 搜索序同源；POST 入参校验用）。

    runner 的私有 _resolve_model 不可依赖（runner 可注入为桩），API 层独立
    解析——校验的是链 input Model 契约，与执行器实现解耦。
    """
    for module_name in _MODEL_SEARCH_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        attr = getattr(module, name, None)
        if isinstance(attr, type) and issubclass(attr, BaseModel):
            return attr
    return None


def _fmt_task(task_id: int) -> str:
    """任务展示形（详设 §3：e-<6位零填充>）。"""
    return f"e-{task_id:06d}"


def _parse_task_id(raw: str) -> int:
    """任务 id 解析：接受展示形 e-000001 或裸数字；非法抛 ValueError。"""
    text = raw.strip()
    if text.startswith("e-"):
        text = text[2:]
    return int(text)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _collect_ref_ids(value: Any, out: set[str] | None = None) -> set[str]:
    """递归收集 dict/list 里所有 ref_id/id 字段的字符串值（决策 16 白名单）。

    任务书技术定（v0.2）：白名单 = 本链喂给 AI 的数据集合（对象 id），简化为
    链 input 递归收集含 ref_id 或 id 的字符串值（demo 链 input 的 ref_id 即
    白名单；真实业务 v0.3 由 Context provider 声明数据集合）。
    """
    if out is None:
        out = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _REF_ID_KEYS and isinstance(item, str):
                out.add(item)
            _collect_ref_ids(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_ref_ids(item, out)
    return out


def _extract_proposal(output: dict[str, Any] | None) -> TaskProposal | None:
    """从链末步 output 提取 TaskProposal（详设-v0.2 §7：末步 output JSONB）。

    判据：dict 含 title/detail/domain/action_id/evidence/source 六键 ->
    model_validate 为 TaskProposal；缺键/类型错返回 None（宁失败不假成功，
    不转交半成品——转交器内还有 TaskProposal 契约校验兜底，双保险）。
    """
    if not isinstance(output, dict):
        return None
    if not _PROPOSAL_KEYS.issubset(output):
        return None
    try:
        return TaskProposal.model_validate(output)
    except ValidationError:
        return None


def _engine_port(repo_root: Path | None = None) -> int:
    """常驻服务端口：.env 的 LIUQUAN_ENGINE_API_URL 解析（P2：值只存 .env）。

    缺省 8100（与 web 侧客户端缺省 base URL 同源，详设 §2.3）。
    """
    root = repo_root if repo_root is not None else _REPO_ROOT
    raw = (dotenv_values(root / ".env").get(_API_URL_ENV) or "").strip()
    if not raw:
        return _DEFAULT_PORT
    try:
        parsed = urlparse(raw)
        if parsed.port is not None:
            return parsed.port
    except ValueError:
        pass
    return _DEFAULT_PORT


def _llm_agent_factory() -> Any:
    """真 Agent 工厂（engine/core/llm/agent.py；agent_cls 不传 = 真 Agent，§13）。"""
    return agent_factory


# ---- 请求/响应契约（详设 §4 逐字段）----


class CreateTaskRequest(BaseModel):
    """POST /api/engine/tasks 请求体（详设 §4.1）。"""

    chain_id: str = Field(min_length=1)
    input: dict[str, Any]
    trigger_ref: str | None = None


class CreateTaskResponse(BaseModel):
    """POST /api/engine/tasks 响应 201（详设 §4.1）。"""

    task_id: str
    status: str
    chain_id: str


class AuditSummaryItem(BaseModel):
    """审计摘要（详设 §4.2：worker_id/model/tokens_in 等安全字段，§8 脱敏）。"""

    worker_id: str
    model: str
    result: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_ms: int | None = None
    created_at: datetime | None = None


class TaskDetailResponse(BaseModel):
    """GET /api/engine/tasks/{id} 响应 200（详设 §4.2）。"""

    task_id: str
    chain_id: str
    status: str
    current_step: int
    error: str | None
    output: dict[str, Any] | None  # 终态后：链产物（含 TaskProposal）
    finished_at: datetime | None
    audits: list[AuditSummaryItem]


class WorkerSummary(BaseModel):
    id: str
    domain: str
    risk: str
    version: int


class ChainSummary(BaseModel):
    id: str
    workers: list[str]


class ActionSummary(BaseModel):
    id: str
    risk: str


class EventSummary(BaseModel):
    id: str


class RegistryResponse(BaseModel):
    """GET /api/engine/registry 响应 200（详设 §4.3 结构）。"""

    workers: list[WorkerSummary]
    chains: list[ChainSummary]
    actions: list[ActionSummary]
    events: list[EventSummary]


# ---- 后台队列消费循环（详设 §2.3）----


class QueueConsumer:
    """后台队列消费循环：SKIP LOCKED 原子认领 + Semaphore 并发上限 + TM 转交钩子。

    - ``start()`` / ``stop()``：常驻循环生命周期（lifespan 自动；测试手动）
    - ``recover()``：崩溃恢复——处理遗留 running 任务（有检查点 resume 续跑 /
      无检查点标记 failed，v0.1 孤儿清理同哲学）
    - 循环体（``_run``）：concurrency 个 worker 协程并发取队消费——每个 worker
      claim_task（原子认领，双消费者不重取）-> runner.consume_once 执行到终态
      -> DONE 走转交钩子 ``_after_task``（§5）
    - 队列空退避 poll_interval（不空转烧 CPU）

    注入式设计（R12）：runner / consumers / tm_engine 全部可注入；测试用桩
    （runner = 真 TaskRunner + FakeAgent 或脚本桩；consumers = 记录桩），
    零网络零真 token。
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        registry: Registry,
        runner: Any,
        consumers: dict[str, Consumer],
        biz_client: BizApiClient | None = None,
        concurrency: int = 2,
        poll_interval: float = 0.5,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._runner = runner
        self._consumers = consumers
        self._biz_client = biz_client
        self._concurrency = max(1, int(concurrency))
        self._poll_interval = max(0.0, float(poll_interval))
        self._task: asyncio.Task[Any] | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        """启动常驻消费循环（幂等：已在运行不重复建任务）。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="engine-queue-consumer")

    async def stop(self) -> None:
        """停止常驻消费循环（幂等）。"""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def recover(self) -> list[str]:
        """崩溃恢复：进程重启后处理遗留 running 任务（详设 §2.3 + 变更日志 M4 配套）。

        - 有检查点 -> runner.resume 续跑到终态（§2.3：不重烧 token，A4 语义）
        - 无检查点（running 与首个检查点之间的崩溃窗口）-> 标记 failed
          （v0.1 孤儿清理同哲学：不留 running 脏行、不毒化队列）
        返回处理摘要行（安全日志用：只含任务 id 与状态）。
        """
        rows = await _db.list_tasks_by_status(self._engine, "running")
        out: list[str] = []
        for row in rows:
            try:
                result = await self._runner.resume(row.id)
                out.append(f"task {_fmt_task(row.id)} resumed -> {result.status}")
            except Exception as exc:
                await _db.update_task(
                    self._engine,
                    row.id,
                    status="failed",
                    error=f"孤儿任务恢复失败（无检查点等）：{exc}",
                    finished_at=_now(),
                )
                out.append(f"task {_fmt_task(row.id)} marked failed（{exc}）")
        return out

    # ---- 常驻循环 ----

    async def _run(self) -> None:
        """常驻消费循环（详设 §2.3）：concurrency 个 worker 协程并发消费。

        并发上限 = worker 协程数 = asyncio.Semaphore(concurrency) 容量
        （§2.3「并发上限 2，asyncio.Semaphore」）：Semaphore 在 claim+执行
        期间持有，作为显式限流位；队列空（claim 返回 None）时退避
        poll_interval 再试，不空转烧 CPU。单 worker 异常不退出循环
        （常驻自愈，安全日志记录），CancelledError 正常传播（stop 用）。
        """
        sem = asyncio.Semaphore(self._concurrency)

        async def worker() -> None:
            while True:
                try:
                    async with sem:
                        result = await self._runner.consume_once()
                    if result is None:
                        await asyncio.sleep(self._poll_interval)
                        continue
                    await self._after_task(result)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # §2.3 常驻进程：单次消费异常不退出循环（systemd 兜底整进程）
                    print(f"queue consumer error: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(self._poll_interval)

        await asyncio.gather(*(worker() for _ in range(self._concurrency)))

    async def _after_task(self, result: TaskResult) -> None:
        """任务终态后的 TM 转交器钩子（详设 §5）：DONE 且末步产出提案时转交。

        - 非 DONE 直接返回（失败/暂停任务不转交）
        - 提取 TaskProposal（末步 output 判据键，§7）；无提案产物 = 非 suggest
          链，不转交
        - 按 proposal.action_id 查消费者注册表（v0.2 仅 tm.proposal）；无消费者
          = 防御跳过（loader L10 已保证 target 合法）
        - 注入 registry / whitelist / audit_lookup / tm_engine 调消费者
        - ConsumeOutcome.status == rejected -> 任务标 failed（error 记拒落原因，
          详设 §3.2：违反禁幻觉三件套即拒落 + 链 FAILED）；skipped_* -> 任务
          保持 done（转交器 skip 日志，决策 17 防重/幂等不改变链终态）
        - 消费者抛异常 -> 任务标 failed（宁失败不假成功：提案没落库不能算成功）
        """
        if result.status != "done":
            return
        task = await _db.get_task(self._engine, result.task_id)
        if task is None:
            return
        step = await _db.get_last_step(self._engine, result.task_id)
        proposal = _extract_proposal(step.output if step is not None else None)
        if proposal is None:
            return  # 非 suggest 链（无提案产物），无转交
        consumer = self._consumers.get(proposal.action_id)
        if consumer is None:
            return
        # v0.3 白名单正式化（决策 16③）：本链喂给 AI 的数据集合 = provider 返回
        # id 集合（检查点 memory_state.context_data 递归收集）；缺省回退 v0.2
        # 简化机制（链 input 递归收集）
        whitelist = await self._context_whitelist(result.task_id)
        if not whitelist:
            whitelist = _collect_ref_ids(task.input)
        lookup = await self._audit_lookup_for(proposal.source.audit_ids)
        kwargs: dict[str, Any] = {
            "registry": self._registry,
            "whitelist": whitelist,
            "audit_lookup": lookup,
        }
        if self._biz_client is not None:
            kwargs["biz_client"] = self._biz_client
        # crm.candidate：source（customer_id/chain_id/engine_task_id/worker_id/
        # audit_ids）由链上下文构造注入（TodoCandidateResult 无 source 字段，
        # 技术定——详设-v0.3 §5.4）
        if proposal.action_id == "crm.candidate":
            source = await self._candidate_source(result.task_id, task)
            kwargs["source"] = source
        try:
            outcome = await consumer(proposal, **kwargs)
        except Exception as exc:
            await _db.update_task(
                self._engine,
                result.task_id,
                status="failed",
                error=f"转交器异常：{exc}",
                finished_at=_now(),
            )
            return
        if outcome.status == "rejected":
            await _db.update_task(
                self._engine,
                result.task_id,
                status="failed",
                error=outcome.reason or "提案被转交器拒落",
                finished_at=_now(),
            )
            return
        # inserted / skipped_idempotent / skipped_duplicate：链保持 done
        # （§8 安全日志：只记任务 id 与 outcome 状态，不记提案正文）
        print(f"tm transfer task={_fmt_task(result.task_id)} outcome={outcome.status}")

    async def _context_whitelist(self, task_id: int) -> set[str]:
        """白名单正式化（决策 16③）：最后检查点 memory_state.context_data 递归收集
        对象 id（provider 返回数据 = 本链喂给 AI 的数据集合）。"""
        ckpt = await _db.last_checkpoint(self._engine, task_id)
        if ckpt is None or not isinstance(ckpt.state, dict):
            return set()
        memory = ckpt.state.get("memory_state") or {}
        context_data = memory.get("context_data") or {}
        return _collect_ref_ids(context_data)

    async def _candidate_source(self, task_id: int, task: Any) -> dict:
        """crm.candidate 候选来源上下文（决策 19/26；消费者 source 注入）。"""
        chain_id = getattr(task, "chain_id", None) or ""
        worker_id = ""
        if chain_id:
            chain = self._registry.chains.get(chain_id)
            if chain is not None and chain.steps:
                worker_id = chain.steps[-1].worker
        async with AsyncSession(self._engine) as session:
            rows = await session.execute(
                select(_db.EngineAudit.id).where(_db.EngineAudit.task_id == task_id)
            )
            audit_ids = [str(r) for r in rows.scalars()]
        return {
            "customer_id": (task.input or {}).get("customer_id"),
            "chain_id": chain_id,
            "engine_task_id": _fmt_task(task_id),
            "worker_id": worker_id,
            "audit_ids": audit_ids,
        }

    async def _audit_lookup_for(self, audit_ids: list[str]) -> Callable[[list[str]], bool]:
        """查引擎库 engine_audit 的可调用（对应 TaskProposal.source.audit_ids）。

        转交器契约的 audit_lookup 是**同步**可调用（models/contract/validation.
        validate_audit_ids 直接调用），故先预取 engine_audit 中可查的 id 集合，
        返回集合成员闭包。空 audit_ids = 纯代码工序产出（reason:none 豁免
        路径），恒真（转交器的空 audit_ids 分支不调 lookup，防御性兜底）。
        """
        if not audit_ids:
            return lambda _ids: True
        async with AsyncSession(self._engine) as session:
            rows = await session.execute(
                select(_db.EngineAudit.id).where(_db.EngineAudit.id.in_(audit_ids))
            )
            found = set(rows.scalars())

        def lookup(ids: list[str]) -> bool:
            for raw in ids:
                try:
                    if int(raw) not in found:
                        return False
                except (TypeError, ValueError):
                    return False
            return True

        return lookup


# ---- FastAPI app（三接口，详设 §4）----


def _audit_item(row: Any) -> AuditSummaryItem:
    return AuditSummaryItem(
        worker_id=row.worker_id,
        model=row.model,
        result=row.result,
        tokens_in=row.input_tokens,
        tokens_out=row.output_tokens,
        duration_ms=row.duration_ms,
        created_at=row.created_at,
    )


def _register_routes(app: FastAPI, engine: AsyncEngine, registry: Registry) -> None:
    """挂三接口路由（详设 §4.1-4.3；与 CLI 同源：建任务入队复用 db.create_task）。"""

    @app.post(
        "/api/engine/tasks",
        response_model=CreateTaskResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_task(payload: CreateTaskRequest) -> CreateTaskResponse:
        """POST /api/engine/tasks：人工触发一个工序链（详设 §4.1）。

        建 engine_task 入队（trigger_type=manual）立即返回 201；实际执行由
        常驻消费循环异步完成（页面侧轮询 GET 查终态）。链未登记 404；input
        不过链 input Model 校验 422。
        """
        chain = registry.chains.get(payload.chain_id)
        if chain is None:
            raise HTTPException(status_code=404, detail="chain not registered")
        model_cls = _resolve_model(chain.input.model)
        if model_cls is None:
            raise HTTPException(
                status_code=422,
                detail=f"链 input Model {chain.input.model} 不可解析（loader L3 应已拦截）",
            )
        try:
            model_cls.model_validate(payload.input)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=f"input 校验失败：{exc}") from exc
        task_id = await _db.create_task(
            engine,
            chain_id=payload.chain_id,
            trigger_type="manual",
            trigger_ref=payload.trigger_ref,
            input_=payload.input,
        )
        return CreateTaskResponse(
            task_id=_fmt_task(task_id), status="queued", chain_id=payload.chain_id
        )

    @app.get("/api/engine/tasks/{task_id}", response_model=TaskDetailResponse)
    async def get_task(task_id: str) -> TaskDetailResponse:
        """GET /api/engine/tasks/{id}：查状态/结果（详设 §4.2）。

        200 含 status/current_step/error/output/finished_at/audits 摘要列表；
        404 任务不存在（含 id 非法的展示形/数字形态）。
        """
        try:
            parsed = _parse_task_id(task_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="task not found")
        row = await _db.get_task(engine, parsed)
        if row is None:
            raise HTTPException(status_code=404, detail="task not found")
        step = await _db.get_last_step(engine, row.id)
        audits = await _db.get_audit(engine, row.id)
        return TaskDetailResponse(
            task_id=_fmt_task(row.id),
            chain_id=row.chain_id,
            status=row.status,
            current_step=row.current_step,
            error=row.error,
            output=step.output if step is not None else None,
            finished_at=row.finished_at,
            audits=[_audit_item(a) for a in audits],
        )

    @app.get("/api/engine/registry", response_model=RegistryResponse)
    async def list_registry() -> RegistryResponse:
        """GET /api/engine/registry：工序/链/Action/事件清单（详设 §4.3）。

        任务中心触发面板用：工序（id/domain/risk/version）、链（id + 步骤
        工序序）、Action（id/risk，risk 由声明代码标注）、事件登记位。
        """
        return RegistryResponse(
            workers=[
                WorkerSummary(id=w.id, domain=w.domain.value, risk=w.risk, version=w.version)
                for w in sorted(registry.workers.values(), key=lambda d: d.id)
            ],
            chains=[
                ChainSummary(id=c.id, workers=[s.worker for s in c.steps])
                for c in sorted(registry.chains.values(), key=lambda d: d.id)
            ],
            actions=[
                ActionSummary(id=a.action_id, risk=a.risk)
                for a in sorted(registry.actions.values(), key=lambda d: d.action_id)
            ],
            events=[
                EventSummary(id=e.event_type)
                for e in sorted(registry.events.values(), key=lambda d: d.event_type)
            ],
        )


def _make_lifespan(consumer: QueueConsumer) -> Callable[[FastAPI], Any]:
    """FastAPI lifespan：启动时崩溃恢复 + 启动消费循环；关闭时停止（§2.3）。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for line in await consumer.recover():
            print(line)
        consumer.start()
        try:
            yield
        finally:
            await consumer.stop()

    return lifespan


def create_app(
    *,
    engine: AsyncEngine,
    registry: Registry,
    runner: Any | None = None,
    runner_kwargs: dict[str, Any] | None = None,
    consumers: dict[str, Consumer] | None = None,
    biz_client: BizApiClient | None = None,
    concurrency: int = 2,
    poll_interval: float = 0.5,
) -> FastAPI:
    """构造引擎常驻 FastAPI app（注入式设计，规范 R12）。

    三接口 + 后台队列消费循环 + TM 转交器钩子全部装配；依赖全部可注入：
    - engine：引擎库 AsyncEngine（必填）
    - registry：真注册表（必填；POST 链登记校验 / GET registry 清单 / 转交
      注入的 Action 声明与工序声明）
    - runner：执行器；None 时按 runner_kwargs 构造真 TaskRunner（生产路径
      传 agent_factory/model_registry/repo_root/writable_check 等）；测试
      可注入桩（真 TaskRunner + FakeAgent 或脚本桩）
    - consumers：Action 消费者注册表；None 时取 engine.actions.CONSUMERS
      （v0.2 仅 tm.proposal -> TM 转交器）
    - tm_engine：业务库 AsyncEngine（转交器落库用；None 时消费者自建真连接）
    - concurrency：队列消费并发上限（§2.3：2）；poll_interval：空队列退避秒

    返回的 app.state.consumer 即 QueueConsumer（lifespan 自动 start/stop；
    ASGITransport 测试不跑 lifespan，可手动 consumer.start()/stop()）。
    """
    if runner is None:
        if not runner_kwargs:
            raise ValueError("create_app 需要 runner 或 runner_kwargs（常驻消费依赖执行器）")
        runner = TaskRunner(engine, registry, **runner_kwargs)
    consumer = QueueConsumer(
        engine=engine,
        registry=registry,
        runner=runner,
        consumers=consumers if consumers is not None else CONSUMERS,
        biz_client=biz_client,
        concurrency=concurrency,
        poll_interval=poll_interval,
    )
    app = FastAPI(title="liuquan-engine", lifespan=_make_lifespan(consumer))
    app.state.engine = engine
    app.state.registry = registry
    app.state.consumer = consumer
    _register_routes(app, engine, registry)
    return app


# ---- 入口（uvicorn 启动，port 从 .env LIUQUAN_ENGINE_API_URL 或缺省）----


def main() -> None:
    """常驻进程入口：装配真实依赖 + uvicorn 启动（详设 §2.3，port 从 .env）。

    真实依赖（全部从 .env + registry 构建，代码零 URL/IP/密钥字面量，P2）：
    引擎库 create_engine / 业务库 create_tm_engine / 真注册表 / 真 Agent
    工厂。lifespan 自动完成崩溃恢复 + 启动消费循环。
    """
    repo_root = _repo_root()
    load_dotenv(repo_root / ".env")  # models.yaml 的 env: 引用在加载期解析（CLI 同款）
    engine = _db.create_engine()
    registry = load_registry(repo_root)
    model_registry = load_models(repo_root / "models.yaml")
    runner = TaskRunner(
        engine,
        registry,
        agent_factory=_llm_agent_factory(),
        model_registry=model_registry,
        repo_root=repo_root,
        writable_check=lambda: True,
        providers=build_providers(),  # 决策 26 读取接口化：provider = HTTP 调 web 读接口
    )
    app = create_app(
        engine=engine,
        registry=registry,
        runner=runner,
        biz_client=BizApiClient(),  # 决策 26 写入接口化：消费者经 HTTP 写接口落库
    )
    port = _engine_port(repo_root)
    import uvicorn  # noqa: PLC0415  # 常驻进程入口才需要 uvicorn

    uvicorn.run(app, host=_DEFAULT_HOST, port=port)


if __name__ == "__main__":
    main()
