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
    "Scheduler",
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

# whitelist 提取键（任务书技术定：链 input 里含 ref_id 或 id 的字符串值；
# v0.4 集成验收修复：+customer_id——提醒链 provider 的 ReminderCustomer 业务
# 对象 id 字段名为 customer_id，白名单需收集否则提醒 evidence 被误判幻觉）
_REF_ID_KEYS = frozenset({"ref_id", "id", "customer_id"})


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
    """递归收集 dict/list 里所有 ref_id/id/customer_id 字段的字符串值（决策 16 白名单）。

    任务书技术定（v0.2）：白名单 = 本链喂给 AI 的数据集合（对象 id），简化为
    链 input 递归收集含 ref_id 或 id 的字符串值（demo 链 input 的 ref_id 即
    白名单；真实业务 v0.3 由 Context provider 声明数据集合）。
    v0.4（集成验收修复）：提醒链 provider 返回 ReminderCustomer 的业务对象 id
    字段名为 customer_id（对齐 crm 域对象口径），白名单收集需含该键——
    否则提醒 evidence ref_id=customer_id 被误判「幻觉证据」拒落。
    """
    if out is None:
        out = set()
    if isinstance(value, dict):
        for key, item in value.items():
            # v0.3：provider 返回 id 为 int（MessageBrief.id），统一转 str 收集
            # （evidence ref_id 是 str；白名单与引用必须同形态，决策 16③）
            if key in _REF_ID_KEYS and item is not None:
                out.add(str(item))
            _collect_ref_ids(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_ref_ids(item, out)
    return out


def _extract_candidate(output: dict[str, Any] | None) -> dict[str, Any] | None:
    """从链末步 output 提取 TodoCandidateResult（详设-v0.3 §5.4：候选消费者）。

    判据：dict 含 "todos" 列表 -> 候选产出（crm.candidate 消费者处理）；
    缺键/类型错返回 None（宁失败不假成功）。
    """
    if not isinstance(output, dict):
        return None
    if not isinstance(output.get("todos"), list):
        return None
    return output


def _extract_suggestion(output: dict[str, Any] | None) -> dict[str, Any] | None:
    """从链末步 output 提取 SuggestionResult（详设-v0.6 §5.3：选品消费者）。

    判据：dict 含 "proposals" 列表 -> 选品产出（scrape.suggest 消费者处理）；
    缺键/类型错返回 None（宁失败不假成功）。v0.6 批 4 修通：suggest 链此前
    未接转交分发（断点 6）。
    """
    if not isinstance(output, dict):
        return None
    if not isinstance(output.get("proposals"), list):
        return None
    return output


def _extract_batch_result(output: dict[str, Any] | None) -> dict[str, Any] | None:
    """从链末步 output 提取 ScrapeBatchResult（详设-v0.6 §5.3：下载链消费者）。

    判据：dict 同时含 "links" 与 "image_ids" 列表 -> 下载链产出
    （scrape.download_done 消费者处理：from_queue=true 清定时队列）。
    """
    if not isinstance(output, dict):
        return None
    if not isinstance(output.get("links"), list):
        return None
    if not isinstance(output.get("image_ids"), list):
        return None
    return output


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
    output: dict[str, Any] | None
    steps_output: list[dict[str, Any]] = [],  # 终态后：链产物（含 TaskProposal）
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


# ---- schedule 接口契约（详设 §9.2）----


def _validate_cron(cron: str) -> str:
    """校验 daily cron（M H * * * 形）；合法返回原串，非法抛 HTTPException 422。

    v0.4 仅支持每天跑（分/时两段为数字，其余为 *），不引第三方 cron 库。
    复用 next_run_time 的校验逻辑（统一口径），失败时友好提示。
    """
    try:
        _db.next_run_time(cron, datetime.now(timezone.utc))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"cron 校验失败：{exc}"
        ) from exc
    return cron


class ScheduleSummary(BaseModel):
    """GET /api/engine/schedules 每条调度项（详设 §9.2）。"""

    id: int
    chain_id: str
    name: str
    cron: str
    enabled: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    last_status: str = ""


class ScheduleListResponse(BaseModel):
    """GET /api/engine/schedules 响应（详设 §9.2）。"""

    schedules: list[ScheduleSummary]


class ScheduleCreateRequest(BaseModel):
    """POST /api/engine/schedules 请求体（详设 §9.2）。"""

    chain_id: str = Field(min_length=1)
    schedule: str = Field(min_length=1, description="5 段 daily cron，如 '0 7 * * *'")
    name: str = Field(min_length=1, max_length=100, default="")


class ScheduleCreateResponse(BaseModel):
    """POST /api/engine/schedules 响应 201（详设 §9.2）。"""

    id: int


class ScheduleToggleResponse(BaseModel):
    """POST /api/engine/schedules/{id}/toggle 响应（详设 §9.2）。"""

    enabled: bool


class ScheduleTimeRequest(BaseModel):
    """POST /api/engine/schedules/{id}/time 请求体（详设 §9.2）。"""

    schedule: str = Field(min_length=1, description="5 段 daily cron")


class ScheduleTimeResponse(BaseModel):
    """POST /api/engine/schedules/{id}/time 响应（详设 §9.2）。"""

    next_run_at: datetime


class ScheduleRunResponse(BaseModel):
    """POST /api/engine/schedules/{id}/run 响应（详设 §9.2）。"""

    engine_task_id: str


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
        """任务终态后的 TM 转交器钩子（详设 §5 + §10.3）：DONE 且末步产出提案/候选/提醒时转交。

        - 非 DONE 直接返回（失败/暂停任务不转交）
        - 末步产出三种形态（详设 §10.3 v0.4 扩展）：
          1) TaskProposal（action_id 查消费者）-> tm.proposal / crm.candidate
          2) TodoCandidateResult（todos 键）-> crm.candidate
          3) ReminderResult（reminders 键）-> tm.schedule（详设 §10.3）
        - 消费者 None = 防御跳过
        - ConsumeOutcome.status == rejected -> 任务标 failed
        - skipped_* -> 任务保持 done
        """
        if result.status != "done":
            return
        task = await _db.get_task(self._engine, result.task_id)
        if task is None:
            return
        step = await _db.get_last_step(self._engine, result.task_id)
        step_output = step.output if step is not None else None
        # v0.3：链末步产出两种——TaskProposal / TodoCandidateResult
        # v0.4：新增第三种——ReminderResult（详设 §10.3）
        # v0.6 批 4：新增两种——SuggestionResult（scrape.suggest，断点 6 修通）/
        #   ScrapeBatchResult（scrape.download_done，详设-v0.6 §5.3）
        proposal = _extract_proposal(step_output)
        candidate = None
        reminders = None
        suggestion = None
        batch_result = None
        consumer_key = None
        if proposal is not None:
            consumer_key = proposal.action_id
        else:
            candidate = _extract_candidate(step_output)
            if candidate is not None:
                consumer_key = "crm.candidate"
            else:
                reminders = _extract_reminders(step_output)
                if reminders is not None:
                    consumer_key = "tm.schedule"
                else:
                    suggestion = _extract_suggestion(step_output)
                    if suggestion is not None:
                        consumer_key = "scrape.suggest"
                    else:
                        batch_result = _extract_batch_result(step_output)
                        if batch_result is not None:
                            consumer_key = "scrape.download_done"
        if consumer_key is None:
            return  # 非 suggest 链，无转交
        consumer = self._consumers.get(consumer_key) if consumer_key else None
        if consumer is None:
            return
        whitelist = await self._context_whitelist(result.task_id)
        if not whitelist:
            whitelist = _collect_ref_ids(task.input)
        audit_ids = proposal.source.audit_ids if proposal is not None else []
        lookup = await self._audit_lookup_for(audit_ids)
        kwargs: dict[str, Any] = {
            "registry": self._registry,
            "whitelist": whitelist,
            "audit_lookup": lookup,
        }
        if self._biz_client is not None:
            kwargs["biz_client"] = self._biz_client
        if candidate is not None:
            kwargs["source"] = await self._candidate_source(result.task_id, task)
        elif reminders is not None:
            # v0.4 §10.3：reminder 消费者 source 注入（chain_id/engine_task_id/
            # worker_id/audit_ids + 从 reminders[0] 补 customer_id/reminder_date）
            kwargs["source"] = await self._reminder_source(result.task_id, task, reminders)
        elif suggestion is not None:
            # v0.6 §5.3：选品消费者 source 注入（SourceTrace 追溯：chain_id/
            # engine_task_id/worker_id/audit_ids——LLM 输出 proposals 无 source，
            # 禁幻觉三件套的 audit_ids 可查依赖它）
            kwargs["source"] = await self._scrape_source(result.task_id, task)
        elif batch_result is not None:
            # v0.6 §5.3：下载链消费者注入 task（读 task.input.from_queue/batch_id）
            kwargs["task"] = task
            # v0.6 批 7（详设 §15.2）：下载链消费者还要拿 connectors（含 quark）+
            # storage_dir（定位链接文件夹）执行夸克上传——从注入 runner 读取透传
            # （runner 装配的 ctx.connectors 与扒图存储根同源，详见 §5.5/§15.1）
            kwargs["connectors"] = getattr(self._runner, "connectors", None)
            kwargs["storage_dir"] = getattr(self._runner, "storage_dir", None)
        try:
            payload = (
                proposal if proposal is not None
                else candidate if candidate is not None
                else reminders if reminders is not None
                else suggestion if suggestion is not None
                else batch_result
            )
            outcome = await consumer(payload, **kwargs)
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

    async def _scrape_source(self, task_id: int, task: Any) -> dict:
        """v0.6 §5.3：scrape.suggest 消费者 source 注入（SourceTrace 追溯）。

        chain_id/engine_task_id/worker_id/audit_ids——product_suggestion 是
        LLM 工序（reason: llm），audit_ids = 本任务 REASON 审计记录（禁幻觉
        三件套的 audit_ids 可查依赖它；转交器按非 reason:none 工序强制校验）。
        """
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
            "chain_id": chain_id,
            "engine_task_id": _fmt_task(task_id),
            "worker_id": worker_id,
            "audit_ids": audit_ids,
        }

    async def _reminder_source(
        self, task_id: int, task: Any, reminders: dict[str, Any]
    ) -> dict:
        """v0.4 §10.3：reminder 消费者 source 注入（链上下文 + 首条 reminder 信息）。"""
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
        # 从首条 reminder 补 customer_id/reminder_date（技术定：消费者用
        # task.input 的 trigger_date + provider 白名单）
        first_reminder = (reminders.get("reminders") or [{}])[0]
        trigger_date = (task.input or {}).get("trigger_date", "")
        return {
            "chain_id": chain_id,
            "engine_task_id": _fmt_task(task_id),
            "worker_id": worker_id,
            "customer_id": first_reminder.get("customer_id"),
            "reminder_date": trigger_date,
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


# ---- 调度器（详设 §9.3）----

# ReminderResult 判据键（末步 output 含 "reminders" -> 走 tm.schedule 消费者）
_REMINDER_KEYS = frozenset({"reminders"})

# ---- v0.6 §5.4：调度器 input 模板（T7，定时触发落地）----
# 按 chain_id 构建定时/立即运行的任务 input：
# - scrape_download_chain：{urls: [], batch_id: "sched-<ts>", from_queue: True,
#   upload_netdisk: <netdisk.upload_default 设置键，缺省 true>}
#   （定时扒：urls 显式空列表——链步骤 input 表达式 task.input.urls 缺键会解析失败
#    （runner _path_get 引用路径不存在即抛，集成验收真跑实锤任务 failed），
#    置空列表让表达式解析通过；urls 实际从定时队列读，链成功完成清队列，详设 §5.4。
#    upload_netdisk 批 8（详设 §15.2/§15.6 批 8 技术定）：不再硬编码 true，改读
#    引擎启动 engine-params 返回的 netdisk.upload_default（缺省 true，用户拍板
#    「未来扒的都要传」）——_ENGINE_INPUT_DEFAULTS 快照装配，重启引擎生效）
# - 其余链（crm_reminder_chain / seo_healthcheck_chain）：{"trigger_date": 今天}
#   行为不变（v0.4/v0.5 测试锁住）
_SCHEDULE_INPUT_TEMPLATES: dict[
    str, Callable[[str, dict[str, Any]], dict[str, Any]]
] = {
    "scrape_download_chain": lambda ts, d: {
        "urls": [],
        "batch_id": f"sched-{ts}",
        "from_queue": True,
        "upload_netdisk": _as_bool(d.get("netdisk.upload_default", True)),
    },
}

# 批 8：定时 input 模板缺省参数快照（netdisk.upload_default，缺省 true）——
# 引擎启动 _build_app 读 engine-params 后经 _apply_engine_params 装配；
# 测试可显式传 defaults 或临时改本快照（照 engine.* 参数语义：重启生效）。
_ENGINE_INPUT_DEFAULTS: dict[str, Any] = {"netdisk.upload_default": True}


def _as_bool(value: object, default: bool = True) -> bool:
    """容错布尔化（engine-params JSON 值为 bool；字符串形态 'false' 等兜底）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return default


def _apply_engine_params(params: dict[str, Any]) -> None:
    """把引擎参数快照装配到模块级定时 input 缺省（仅 _build_app 生产启动调用）。"""
    _ENGINE_INPUT_DEFAULTS["netdisk.upload_default"] = _as_bool(
        params.get("netdisk.upload_default", True)
    )


def _schedule_input_for(
    chain_id: str,
    ts: str,
    today_str: str,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """定时/立即运行的任务 input（按 chain_id 模板；默认 trigger_date=今天）。

    批 8：defaults = 引擎参数快照（netdisk.upload_default → 定时 input
    upload_netdisk）；None → 用模块级 _ENGINE_INPUT_DEFAULTS（启动装配值）。
    """
    template = _SCHEDULE_INPUT_TEMPLATES.get(chain_id)
    if template is not None:
        return template(ts, defaults if defaults is not None else _ENGINE_INPUT_DEFAULTS)
    return {"trigger_date": today_str}


def _schedule_time_to_cron(hhmm: str) -> str:
    """'HH:MM' → daily cron（'M H * * *'，不补前导零；照 web _scrape_time_cron 口径）。

    engine-params 返回的 scrape.schedule_time（如 '08:30'）→ '30 8 * * *'
    （种子 cron 来源，详设-v0.6 §5.4/§8；非法值回退默认 '0 7 * * *'）。
    """
    try:
        h, m = (int(x) for x in str(hhmm).strip().split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return "0 7 * * *"
        return f"{m} {h} * * *"
    except (ValueError, TypeError):
        return "0 7 * * *"


def _extract_reminders(output: dict[str, Any] | None) -> dict[str, Any] | None:
    """从链末步 output 提取 ReminderResult（详设 §10.3；task 第三种终态产出）。

    判据：dict 含 "reminders" 列表 -> 提醒产出（tm.schedule 消费者处理）；
    缺键/类型错返回 None。
    """
    if not isinstance(output, dict):
        return None
    if not isinstance(output.get("reminders"), list):
        return None
    return output


class Scheduler:
    """调度器：常驻 tick，到点触发定时链入队（详设 §9.3）。

    构造注入 engine/registry + 可注入 interval（默认 30s）与 now 提供者
    （测试桩时钟）；start()/stop() 管理 asyncio.Task 生命周期。

    tick 逻辑：
    1) ensure_seed_schedules()（空表种子，幂等）
    2) 查 enabled 且 next_run_at <= now 且 (last_run_at IS NULL OR last_run_at < next_run_at) 的链
    3) 逐条：mark_schedule_run + create_task
    4) 触发异常 -> last_status='failed'，不阻塞其他链
    """

    def __init__(
        self,
        engine: AsyncEngine,
        registry: Registry,
        *,
        interval: float = 30.0,
        now_fn: Callable[[], datetime] | None = None,
        cron_overrides: dict[str, str] | None = None,  # v0.6 §5.4：种子 cron 覆盖（引擎启动读设置）
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._interval = max(1.0, float(interval))
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._cron_overrides = dict(cron_overrides or {})
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """启动常驻 tick（幂等：已在运行不重复建任务）。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="engine-scheduler")

    async def stop(self) -> None:
        """停止常驻 tick（幂等）。"""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def tick(self) -> None:
        """单次 tick 执行体（详设 §9.3，可供测试直接调用）。"""
        await _db.ensure_seed_schedules(
            self._engine, cron_overrides=self._cron_overrides
        )
        now = self._now_fn()
        today_str = now.strftime("%Y-%m-%d")
        ts = now.strftime("%Y%m%d%H%M%S")
        rows = await _db.list_schedules(self._engine)
        for row in rows:
            if not row.enabled:
                continue
            if row.next_run_at is None:
                # 初始化锚点（新建/种子链，详设 §9.3）：算下一个 HH:MM 落库，
                # 不立即触发（新链从下次计划开始，验收「种子链开箱可用」）
                initial = _db.next_run_time(row.cron, now)
                await _db.update_schedule(
                    self._engine, row.id, next_run_at=initial, set_next_run_at=True
                )
                continue
            if row.next_run_at > now:
                continue
            if row.last_run_at is not None and row.last_run_at >= row.next_run_at:
                continue
            # 到点触发
            try:
                new_next = _db.next_run_time(row.cron, now)
                await _db.mark_schedule_run(
                    self._engine,
                    row.id,
                    last_run_at=row.next_run_at,
                    next_run_at=new_next,
                )
                task_id = await _db.create_task(
                    self._engine,
                    chain_id=row.chain_id,
                    trigger_type="schedule",
                    trigger_ref=str(row.id),
                    # v0.6 §5.4（T7）：input 按 chain_id 模板（scrape_download_chain →
                    # {batch_id: "sched-<ts>", from_queue: true}；其余链 trigger_date 不变）
                    input_=_schedule_input_for(row.chain_id, ts, today_str),
                )
                print(
                    f"scheduler: 触发 {row.chain_id} (schedule={row.id}) "
                    f"-> task {_fmt_task(task_id)}"
                )
            except Exception as exc:
                # 触发异常：last_status='failed' 记录，不阻塞其他链
                try:
                    async with _db._sessions(self._engine)() as session, session.begin():
                        from engine.core.db import Schedule as _Schedule
                        from sqlalchemy import update as _update

                        await session.execute(
                            _update(_Schedule)
                            .where(_Schedule.id == row.id)
                            .values(
                                last_status="failed",
                                updated_at=_db.func.now(),
                            )
                        )
                except Exception:
                    pass  # 记录失败也异常 -> 忽略，不阻塞
                print(
                    f"scheduler: 链 {row.chain_id} (schedule={row.id}) 触发失败："
                    f"{type(exc).__name__}: {exc}"
                )

    async def _run(self) -> None:
        """常驻 tick 循环（详设 §9.3）。"""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"scheduler tick 异常：{type(exc).__name__}: {exc}")
            await asyncio.sleep(self._interval)


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
            # v0.3：链各步骤输出（译文/快照在中间步骤，web apply 落库用）
            steps_output=[
                {"worker_id": s.worker_id, "output": s.output}
                for s in await _db.list_steps(engine, row.id)
            ],
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

    # ---- schedule 管理接口（详设 §9.2）----

    @app.get("/api/engine/schedules", response_model=ScheduleListResponse)
    async def list_schedules() -> ScheduleListResponse:
        """GET /api/engine/schedules：定时链列表（web 设置页数据源）。"""
        rows = await _db.list_schedules(engine)
        return ScheduleListResponse(
            schedules=[
                ScheduleSummary(
                    id=r.id,
                    chain_id=r.chain_id,
                    name=r.name,
                    cron=r.cron,
                    enabled=r.enabled,
                    last_run_at=r.last_run_at,
                    next_run_at=r.next_run_at,
                    last_status=r.last_status,
                )
                for r in rows
            ]
        )

    @app.post(
        "/api/engine/schedules",
        response_model=ScheduleCreateResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_schedule(payload: ScheduleCreateRequest) -> ScheduleCreateResponse:
        """POST /api/engine/schedules：新增定时链（详设 §9.2）。

        校验：chain_id 必须命中 registry.chains（否则 422）；cron 必须 daily 形；
        创建时计算 next_run_at 并落库。
        """
        if payload.chain_id not in registry.chains:
            raise HTTPException(status_code=422, detail="chain_id not in registry")
        cron = _validate_cron(payload.schedule)
        now = datetime.now(timezone.utc)
        next_run = _db.next_run_time(cron, now)
        name = payload.name or payload.chain_id
        schedule_id = await _db.create_schedule(
            engine,
            chain_id=payload.chain_id,
            name=name,
            cron=cron,
            enabled=True,
        )
        await _db.update_schedule(engine, schedule_id, cron=cron)
        # 创建后单独更新 next_run_at（create_schedule 不设此字段）
        async with _db._sessions(engine)() as session, session.begin():
            from engine.core.db import Schedule as _Schedule
            from sqlalchemy import update as _update

            await session.execute(
                _update(_Schedule)
                .where(_Schedule.id == schedule_id)
                .values(next_run_at=next_run, updated_at=_db.func.now())
            )
        return ScheduleCreateResponse(id=schedule_id)

    @app.post("/api/engine/schedules/{schedule_id}/toggle", response_model=ScheduleToggleResponse)
    async def toggle_schedule(schedule_id: int) -> ScheduleToggleResponse:
        """POST /api/engine/schedules/{id}/toggle：enabled 翻转（详设 §9.2）。"""
        row = await _db.get_schedule(engine, schedule_id)
        if row is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        new_enabled = not row.enabled
        await _db.update_schedule(engine, schedule_id, enabled=new_enabled)
        return ScheduleToggleResponse(enabled=new_enabled)

    @app.post("/api/engine/schedules/{schedule_id}/time", response_model=ScheduleTimeResponse)
    async def update_schedule_time(schedule_id: int, payload: ScheduleTimeRequest) -> ScheduleTimeResponse:
        """POST /api/engine/schedules/{id}/time：改 cron + 重算 next_run_at（详设 §9.2）。"""
        row = await _db.get_schedule(engine, schedule_id)
        if row is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        cron = _validate_cron(payload.schedule)
        now = datetime.now(timezone.utc)
        next_run = _db.next_run_time(cron, now)
        await _db.update_schedule(engine, schedule_id, cron=cron)
        async with _db._sessions(engine)() as session, session.begin():
            from engine.core.db import Schedule as _Schedule
            from sqlalchemy import update as _update

            await session.execute(
                _update(_Schedule)
                .where(_Schedule.id == schedule_id)
                .values(next_run_at=next_run, updated_at=_db.func.now())
            )
        return ScheduleTimeResponse(next_run_at=next_run)

    @app.post("/api/engine/schedules/{schedule_id}/run", response_model=ScheduleRunResponse)
    async def run_schedule(schedule_id: int) -> ScheduleRunResponse:
        """POST /api/engine/schedules/{id}/run：立即运行（决策 37-7）。

        create_task 入队（trigger_type=manual_schedule, trigger_ref=schedule_id；
        input 按 chain_id 模板——v0.6 §5.4：scrape_download_chain →
        {batch_id: "sched-<ts>", from_queue: true}，其余链 trigger_date 今天）；
        不动 last_run_at/next_run_at。
        """
        row = await _db.get_schedule(engine, schedule_id)
        if row is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        ts = now.strftime("%Y%m%d%H%M%S")
        task_id = await _db.create_task(
            engine,
            chain_id=row.chain_id,
            trigger_type="manual_schedule",
            trigger_ref=str(schedule_id),
            input_=_schedule_input_for(row.chain_id, ts, today_str),
        )
        return ScheduleRunResponse(engine_task_id=_fmt_task(task_id))


async def _read_engine_params_from_biz(
    biz_client: BizApiClient | None,
) -> dict[str, Any]:
    """启动时经 biz_client 读 web 侧引擎参数（详设 §7.3/§8）。

    成功返回 {default_max_attempts, default_timeout_s, backoff_cap, llm_model,
    vision_model, scrape.storage_dir, scrape.schedule_time,
    netdisk.upload_default}；
    失败（BizApiError/网络）回退默认 + warning 不阻塞启动。

    v0.5 §7.1 扩展：+ llm_model / vision_model（模型名覆盖）。
    v0.6 §5.5 扩展：+ scrape.storage_dir（connector 落盘根）/ scrape.schedule_time
    （种子 cron，'HH:MM' 字符串）。
    v0.6 批 8 扩展：+ netdisk.upload_default（定时 input upload_netdisk 缺省，
    读设置键缺省 true——§15.2/§15.6 批 8 技术定）。
    """
    defaults: dict[str, Any] = {
        "default_max_attempts": 2,
        "default_timeout_s": 30.0,
        "backoff_cap": 30.0,
        "llm_model": "deepseek-chat",
        "vision_model": "qwen-vl-max",
        "scrape.storage_dir": "/opt/liuquan/scrape/",
        "scrape.schedule_time": "07:00",
        "netdisk.upload_default": True,
    }
    if biz_client is None:
        return defaults
    try:
        resp = await biz_client.get("/settings/engine-params")
        if resp.status_code == 200:
            data = resp.json()
            return {
                "default_max_attempts": int(data.get("max_attempts", 2)),
                "default_timeout_s": float(data.get("timeout_s", 30.0)),
                "backoff_cap": float(data.get("backoff_cap", 30.0)),
                "llm_model": str(data.get("llm_model", "deepseek-chat")),
                "vision_model": str(data.get("vision_model", "qwen-vl-max")),
                "scrape.storage_dir": str(
                    data.get("scrape.storage_dir", "/opt/liuquan/scrape/")
                ),
                "scrape.schedule_time": str(
                    data.get("scrape.schedule_time", "07:00")
                ),
                "netdisk.upload_default": _as_bool(
                    data.get("netdisk.upload_default", True)
                ),
            }
        print(
            f"warning: 读取引擎参数失败（HTTP {resp.status_code}），回退默认值"
        )
    except Exception as exc:
        print(
            f"warning: 读取引擎参数异常（{type(exc).__name__}: {exc}），"
            "回退默认值"
        )
    return defaults


def _make_lifespan(
    consumer: QueueConsumer,
    scheduler: Scheduler | None = None,
    biz_client: BizApiClient | None = None,
    runner_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> Callable[[FastAPI], Any]:
    """FastAPI lifespan：启动时崩溃恢复 + 读引擎参数注入 runner + 启动消费循环 + 调度器。

    runner_factory: 可选——接收 engine_params dict（{default_max_attempts,
    default_timeout_s, backoff_cap}，读取失败时已回退默认），返回新 TaskRunner。
    默认构造 runner 路径（create_app 未显式注入 runner）传它：lifespan 内读取
    web 侧引擎参数后**重建 runner 并替换 consumer._runner**（TaskRunner 构造后
    无状态只读，替换安全），使 settings 的 engine.* 参数真正生效（详设 §7.3：
    页面改引擎参数 -> 重启引擎生效）；读取失败 -> params 为默认值，重建等价
    原 runner（决策 34 表无回退默认）。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 读取引擎参数（详设 §7.3：启动时读一次）并注入 runner
        if biz_client is not None and runner_factory is not None:
            engine_params = await _read_engine_params_from_biz(biz_client)
            app.state.engine_params = engine_params
            consumer._runner = runner_factory(engine_params)
        for line in await consumer.recover():
            print(line)
        consumer.start()
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
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
    scheduler_interval: float = 30.0,
    scheduler_enabled: bool = True,
    scheduler_now_fn: Callable[[], datetime] | None = None,
    scheduler_cron_overrides: dict[str, str] | None = None,  # v0.6 §5.4：种子 cron 覆盖
) -> FastAPI:
    """构造引擎常驻 FastAPI app（注入式设计，规范 R12）。

    三接口 + schedule 五接口 + 后台队列消费循环 + TM 转交器钩子 + 调度器
    全部装配；依赖全部可注入：
    - engine：引擎库 AsyncEngine（必填）
    - registry：真注册表（必填；POST 链登记校验 / GET registry 清单 / 转交
      注入的 Action 声明与工序声明）
    - runner：执行器；None 时按 runner_kwargs 构造真 TaskRunner（生产路径
      传 agent_factory/model_registry/repo_root/writable_check 等）；测试
      可注入桩（真 TaskRunner + FakeAgent 或脚本桩）
    - consumers：Action 消费者注册表；None 时取 engine.actions.CONSUMERS
    - concurrency：队列消费并发上限（§2.3：2）；poll_interval：空队列退避秒
    - scheduler_interval：调度器 tick 间隔秒（默认 30）；scheduler_enabled：
      是否启动调度器（测试可禁用）；scheduler_now_fn：桩时钟（测试用）
    - scheduler_cron_overrides：v0.6 §5.4 种子 cron 覆盖（scrape_download_chain
      从设置读 scrape.schedule_time，默认 '0 7 * * *'）

    返回的 app.state.consumer / app.state.scheduler 即 QueueConsumer/Scheduler
    （lifespan 自动 start/stop；ASGITransport 测试不跑 lifespan，可手动
    consumer.start()/stop()、scheduler.tick()）。
    """
    if runner is None:
        if not runner_kwargs:
            raise ValueError("create_app 需要 runner 或 runner_kwargs（常驻消费依赖执行器）")
        runner = TaskRunner(engine, registry, **runner_kwargs)
        # 默认构造路径：lifespan 读取引擎参数后重建 runner 并替换（真正注入，
        # 详设 §7.3 engine.* 参数；runner_kwargs 显式键优先，读取参数只补缺省）。
        # v0.6 §5.5：重建时 connectors 按 scrape.storage_dir 装配（connector 落盘根）。
        _runner_param_keys = frozenset(
            {
                "default_max_attempts",
                "default_timeout_s",
                "backoff_cap",
                "llm_model",
                "vision_model",
            }
        )

        def default_runner_factory(params: dict[str, Any]) -> TaskRunner:
            from engine.connectors import build_connectors

            merged = {
                **runner_kwargs,
                **{k: v for k, v in params.items() if k in _runner_param_keys},
                "connectors": build_connectors(params.get("scrape.storage_dir")),
                # v0.6 §15.1（批 6）：扒图存储根注入 runner → EngineContext.storage_dir
                # （batch_image_download 归集建链接文件夹用，与 connector 落盘根同源）
                "storage_dir": params.get("scrape.storage_dir"),
            }
            return TaskRunner(engine, registry, **merged)

    else:
        default_runner_factory = None
    consumer = QueueConsumer(
        engine=engine,
        registry=registry,
        runner=runner,
        consumers=consumers if consumers is not None else CONSUMERS,
        biz_client=biz_client,
        concurrency=concurrency,
        poll_interval=poll_interval,
    )
    scheduler = None
    if scheduler_enabled:
        scheduler = Scheduler(
            engine,
            registry,
            interval=scheduler_interval,
            now_fn=scheduler_now_fn,
            cron_overrides=scheduler_cron_overrides,
        )
    app = FastAPI(
        title="liuquan-engine",
        lifespan=_make_lifespan(
            consumer,
            scheduler=scheduler,
            biz_client=biz_client,
            runner_factory=default_runner_factory,
        ),
    )
    app.state.engine = engine
    app.state.registry = registry
    app.state.consumer = consumer
    app.state.scheduler = scheduler
    _register_routes(app, engine, registry)
    return app


# ---- 入口（uvicorn 启动，port 从 .env LIUQUAN_ENGINE_API_URL 或缺省）----


def main() -> None:
    """常驻进程入口：装配真实依赖 + uvicorn 启动（详设 §2.3，port 从 .env）。

    真实依赖（全部从 .env + registry 构建，代码零 URL/IP/密钥字面量，P2）：
    引擎库 create_engine / 业务库 create_tm_engine / 真注册表 / 真 Agent
    工厂。lifespan 自动完成崩溃恢复 + 启动消费循环。

    v0.6 §5.3/§5.5（批 4）：启动先经 biz_client 读 engine-params——
    scrape.storage_dir 装配 connector 落盘根（build_connectors）+ scrape.schedule_time
    转种子 cron（scrape_download_chain 定时扒图）；biz_client 注入 runner
    （EngineContext.biz_client，T4 方案 A）。
    """
    app, port = asyncio.run(_build_app())
    import uvicorn  # noqa: PLC0415  # 常驻进程入口才需要 uvicorn

    uvicorn.run(app, host=_DEFAULT_HOST, port=port)


async def _build_app() -> tuple[Any, int]:
    """装配真实依赖并返回 (app, port)（uvicorn.run 需在 asyncio.run 之外调用）。"""
    repo_root = _repo_root()
    load_dotenv(repo_root / ".env")  # models.yaml 的 env: 引用在加载期解析（CLI 同款）
    engine = _db.create_engine()
    registry = load_registry(repo_root)
    model_registry = load_models(repo_root / "models.yaml")

    # v0.6 §5.3：biz_client 写接口客户端（决策 26；引擎零业务库连接串）
    biz_client = BizApiClient()

    # v0.6 §5.5：启动读 engine-params（storage_dir → connector 装配；schedule_time → 种子 cron）
    engine_params = await _read_engine_params_from_biz(biz_client)
    storage_dir = engine_params.get("scrape.storage_dir")
    schedule_time = engine_params.get("scrape.schedule_time")
    # v0.6 批 8（详设 §15.2/§15.6 批 8 技术定）：netdisk.upload_default →
    # 模块级定时 input 缺省快照（重启引擎生效，照 engine.* 参数同语义）
    _apply_engine_params(engine_params)

    # v0.5 §5 + v0.6 §5.5：装配 connectors 注册表（工序按 id 引用外部资源；
    # 落盘根 = settings 的 scrape.storage_dir）
    from engine.connectors import build_connectors  # noqa: PLC0415
    connectors = build_connectors(storage_dir)

    runner = TaskRunner(
        engine,
        registry,
        agent_factory=_llm_agent_factory(),
        model_registry=model_registry,
        repo_root=repo_root,
        writable_check=lambda: True,
        providers=build_providers(),  # 决策 26 读取接口化：provider = HTTP 调 web 读接口
        connectors=connectors,  # v0.5 §5：外部资源连接器
        biz_client=biz_client,  # v0.6 §5.3：业务写接口客户端（T4 方案 A）
        storage_dir=storage_dir,  # v0.6 §15.1（批 6）：扒图存储根（归集建链接文件夹）
    )
    app = create_app(
        engine=engine,
        registry=registry,
        runner=runner,
        biz_client=biz_client,  # 决策 26 写入接口化：消费者经 HTTP 写接口落库
        scheduler_cron_overrides={
            "scrape_download_chain": _schedule_time_to_cron(schedule_time)
        },
    )
    return app, _engine_port(repo_root)


if __name__ == "__main__":
    main()
