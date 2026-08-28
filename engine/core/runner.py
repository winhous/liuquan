"""T12a 执行链 runner（详设-v0.1 §2 状态机 / §2.3 崩溃恢复 / §4.2 链 / §13；任务 T12a）。

组装 T5（statemachine/policy）/ T8（llm）/ T9（checkpoint/audit）/ T6（db DAO）
全部部件：队列取出（SKIP LOCKED）-> 逐工序状态机循环 -> 检查点/审计落库 ->
崩溃恢复（resume 不重烧 token，验收断言 A4）。公共 API（PhaseLine / TaskResult /
TaskRunner）是给 T12b CLI 的契约，签名不可擅改。

依赖注入（规范 R12 构造注入，测试全用桩）：
- agent_factory（必填）：T8 的 ``agent_factory(registry, alias, output_type)``，
  测试注入包装成 FakeAgent 工厂
- model_registry：models.yaml 的 ModelRegistry——agent_factory 第一参 +
  审计 model 字段（实际模型串）来源（模型串永不内联，R11）
- repo_root：注册表仓库根——prompt.md / config/ 目录内容从
  engine/registry/workers/<域>/<工序>/ 读取（loader L8 已保证文件在位）
- audit_gate（AuditGate）：先审计后调用门（§7.3），透传给 call_llm
- writable_check：audit_gate 缺省时包装成 DbAuditGate 的探活函数
- providers：Context provider 实现注册表 {实现标识: async provide(params)}，
  缺省空 dict（无实现 = context_data 留空，v0.1 demo 语义）
- backoff：ACT 相位重试 / LLM 重试的退避基值（测试注入 0，不真等 5s/10s）

技术决策（记录，供变更日志）：
- 链入参在 run() 入口过链声明的 input Model（校验不过 = ValueError 不入队）；
  工序入参在 INIT 相位过工序 input Model（不过 = POLICY_FAILED -> FAILED，
  不可重试——输入有问题，§2.1）。
- check_policy 的 context 域 token = 各 context 引用 id 的点分前缀
  （c.id.split(".")[0]，裸域 token 原样；policy 判据与 §5.2 同源）。
- REASON 的输出类型校验 = call_llm 结果对 output Model 的二次校验（桩 Agent
  不代做 PydanticAI 的 output_type 强制，真 Agent 已保证，宁失败不假成功）；
  LLM 层异常（含非网络类，无静默降级）一律记审计 result=failed 后
  EVENT_LLM_FAILED -> FAILED，任务必达终态。
- OBSERVE v0.1 语义 = get_step(step_id) 读回 engine_step.output 已落库
  （db.py 契约扩展：新增 get_step + StepRow，见 test_db 补测）。
- VERIFY v0.1 业务断言 = 输出非空（dict 非空且至少一个非空字段值）+
  输出过 output Model 校验（宁失败不假成功）。
- PAUSED 与崩溃恢复同一条路径（§2.3 人工暂停 = 优雅崩溃）：pause 在相位
  循环顶部检查（request_pause()），落检查点（from_phase=待执行相位，
  to_phase=PAUSED，含内存状态）后转 PAUSED；resume 目标相位 = 检查点
  to_phase（PAUSED 时取 from_phase），经状态机 PAUSED+EVENT_RESUME 校验。
- 转换表（§2.2）无 OBSERVE->FAILED 行：OBSERVE 读回失败无对应事件，
  runner 按「event=None 直接终态」处理（§2.1 读不回 = FAILED，写了个寂寞）。
- ACT 重试 = 转换表 EVENT_SIDE_EFFECT_FAILED 的分支（attempt_left），
  attempt 计数进检查点 memory_state；重试上限 = worker.retry.max_attempts。
- 检查点只写在「非 FAILED」的转换前（§2.2：FAILED 行记 error 不落检查点）；
  VERIFY->DONE 落终态检查点。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from engine.core import db as _db
from engine.core.audit import AuditRecorder, DbAuditGate
from engine.core.checkpoint import CheckpointManager
from engine.core.context import EngineContext
from engine.core.llm import LLMError, call_llm
from engine.core.policy import check_policy
from engine.core.statemachine import (
    EVENT_ASSERTIONS_FAILED,
    EVENT_ASSERTIONS_OK,
    EVENT_LLM_FAILED,
    EVENT_OUTPUT_VALID,
    EVENT_PAUSE,
    EVENT_POLICY_FAILED,
    EVENT_POLICY_OK,
    EVENT_READBACK_OK,
    EVENT_RESUME,
    EVENT_SIDE_EFFECT_FAILED,
    EVENT_SIDE_EFFECT_OK,
    Phase,
    PhaseTransitionError,
    TERMINAL_PHASES,
    TransitionContext,
    transition,
)

__all__ = ["PhaseLine", "TaskResult", "TaskRunner"]

# §14 技术定缺省（worker.yaml 未声明 retry 时；RetrySpec 缺省同源）
_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_TIMEOUT_S = 30.0
# ACT 重试退避封顶（§14 指数退避 5s→30s）
_BACKOFF_CAP_S = 30.0
# 模型名 -> Model 类解析搜索序（loader L3 同源）
_MODEL_SEARCH_MODULES = ("models.workers", "models.contract", "models")

# 链步骤 input 引用表达式（§4.2：task.input(.字段) / steps[n].output(.字段)）
_EXPR_TASK_RE = re.compile(r"^task\.input(?:\.(.+))?$")
_EXPR_STEP_RE = re.compile(r"^steps\[(\d+)\]\.output(?:\.(.+))?$")
# context provider params 的 input 字段引用（§4.1：customer_id: input.customer_id）
_INPUT_REF_RE = re.compile(r"^input\.(.+)$")
# prompt.md 模板变量（{input.*} {context.<provider_id>.*} {config.*}）
_PLACEHOLDER_RE = re.compile(
    r"\{([A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*)\}"
)


@dataclass(frozen=True)
class PhaseLine:
    """相位流水输出（A1「输出相位流水」；CLI 按 ``[phase] message`` 打印）。"""

    phase: str
    message: str


@dataclass(frozen=True)
class TaskResult:
    """一次任务执行的终态结果（run / resume 公共返回）。"""

    task_id: int
    chain_id: str
    status: str  # done / failed / paused
    error: str | None
    phase_lines: list[PhaseLine]


@dataclass
class _StepOutcome:
    """单工序执行结果（内部）：status done/failed/paused + 相位流水 + 产出。"""

    status: str
    lines: list[PhaseLine]
    output: dict | None
    error: str | None


# ---- DAO 适配（engine/core/db.py 的函数族适配为 T9 的协议形态，R21 契约依赖）----


class _DbCheckpointDAO:
    """CheckpointDAO 真实现（checkpoint.py 声明协议的 db.py 子集）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def write_checkpoint(
        self, *, task_id: int, step_id: int, from_phase: str, to_phase: str, state: Any
    ) -> None:
        await _db.write_checkpoint(
            self._engine,
            task_id=task_id,
            step_id=step_id,
            from_phase=from_phase,
            to_phase=to_phase,
            state=state,
        )

    async def last_checkpoint(self, task_id: int) -> Any:
        return await _db.last_checkpoint(self._engine, task_id)


class _DbAuditDAO:
    """AuditDAO 真实现（audit.py 声明协议的 db.py 子集）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def append_audit(
        self,
        *,
        task_id: int,
        step_id: int,
        worker_id: str,
        model: str,
        attempt: int,
        result: str,
        input_full: Any,
        output_full: Any,
        input_tokens: int | None,
        output_tokens: int | None,
        duration_ms: int | None,
    ) -> None:
        await _db.append_audit(
            self._engine,
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

    async def get_audit(self, task_id: int) -> list:
        return await _db.get_audit(self._engine, task_id)


class TaskRunner:
    """执行链 runner（§11：队列取出 + 逐工序执行；§2.3 崩溃恢复）。"""

    def __init__(
        self,
        engine: AsyncEngine,
        registry: Any,
        *,
        agent_factory: Callable[..., Any],
        model_registry: Any | None = None,
        repo_root: str | Path | None = None,
        audit_gate: Any | None = None,
        writable_check: Callable[[], bool] | None = None,
        providers: dict[str, Callable[..., Any]] | None = None,
        backoff: float = 0.0,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._agent_factory = agent_factory
        self._model_registry = model_registry
        self._repo_root = Path(repo_root).resolve() if repo_root is not None else None
        self._providers = dict(providers or {})
        self._backoff = backoff
        self._audit = AuditRecorder(_DbAuditDAO(engine))
        self._checkpoint_dao = _DbCheckpointDAO(engine)
        self._audit_gate = audit_gate
        if self._audit_gate is None and writable_check is not None:
            self._audit_gate = DbAuditGate(writable_check)
        self._pause_requested = False
        self._prompt_cache: dict[str, str] = {}
        self._config_cache: dict[str, dict[str, Any]] = {}

    # ---- 公共 API（T12b CLI 契约）----

    async def run(
        self,
        chain_id: str,
        input_: dict,
        *,
        trigger_type: str = "manual",
        trigger_ref: str | None = None,
    ) -> TaskResult:
        """建任务入队（SKIP LOCKED 取自己这条）并同步执行到终态（§10 CLI 语义）。"""
        chain = self._registry.chains.get(chain_id)
        if chain is None:
            raise ValueError(f"链 {chain_id} 未在注册表登记（registry-check 应已拦截）")
        chain_input_cls = self._resolve_model(chain.input.model)
        if chain_input_cls is None:
            raise ValueError(
                f"链 input Model {chain.input.model} 不可解析（loader L3 应已拦截）"
            )
        try:
            chain_input_cls.model_validate(input_)
        except ValidationError as exc:
            raise ValueError(
                f"链入参未过 {chain.input.model} 校验，不入队：{exc}"
            ) from exc

        task_id = await _db.create_task(
            self._engine,
            chain_id=chain_id,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            input_=input_,
        )
        # 取自己这条（SKIP LOCKED 最老优先）；遇孤儿（创建后未被取走即崩溃的
        # 任务）先标记失败清理再重试，防止毒化队列（review M8 修复）
        row: Any = None
        for _ in range(20):
            row = await _db.dequeue_task(self._engine)
            if row is None or row.id == task_id:
                break
            await _db.update_task(
                self._engine,
                row.id,
                status="failed",
                error="孤儿任务（创建后未被取走即崩溃，被后续 run 清理）",
            )
        if row is None or row.id != task_id:
            raise RuntimeError(
                f"dequeue 未取到本任务（task_id={task_id}）——队列被并发消费"
            )
        await _db.update_task(
            self._engine, task_id, status="running", started_at=_now()
        )
        self._pause_requested = False
        lines: list[PhaseLine] = []
        try:
            status, error = await self._run_chain_steps(
                task_id, chain, input_, start_index=0, prior_outputs=[], lines=lines
            )
        except Exception as exc:
            # 意外异常兜底：任何未捕获异常也落终态，不留 running 脏行（review C1 修复）
            status, error = "failed", f"runner 未捕获异常：{exc}"
        return await self._finalize(task_id, chain_id, status, error, lines)

    async def resume(self, task_id: int) -> TaskResult:
        """从最后检查点续跑（§2.3；PAUSED 与崩溃恢复同一条路径）。

        关键保证（验收断言 A4）：REASON 已成功的 LLM 结果在检查点
        state["phase_output"] 里，续跑直接用它、不重调 LLM（不重烧 token）。
        """
        task = await _db.get_task(self._engine, task_id)
        if task is None:
            raise ValueError(f"任务 {task_id} 不存在")
        if task.status in ("done", "failed"):
            return TaskResult(task_id, task.chain_id, task.status, task.error, [])
        chain = self._registry.chains.get(task.chain_id)
        if chain is None:
            raise ValueError(
                f"任务 {task_id} 的链 {task.chain_id} 未在注册表登记"
            )
        point = await CheckpointManager(
            self._checkpoint_dao, task_id=task_id
        ).resume_point(task_id)
        if point is None:
            raise ValueError(f"任务 {task_id} 无检查点，无法恢复（§2.3）")
        step_row = await _db.get_step(self._engine, point.step_id)
        if step_row is None:
            raise ValueError(f"检查点引用的 step {point.step_id} 不存在（数据不一致）")
        step_index = step_row.step_index
        if step_index >= len(chain.steps):
            raise ValueError(f"检查点 step_index {step_index} 超出链步骤数")

        # 恢复执行期间任务标回 running（review M14 修复：防并发二次 resume）
        await _db.update_task(self._engine, task_id, status="running")
        self._pause_requested = False
        lines: list[PhaseLine] = []
        try:
            to_phase = Phase(point.to_phase)
            target = (
                to_phase if to_phase is not Phase.PAUSED else Phase(point.from_phase)
            )
            if target is Phase.DONE:
                # 崩溃落在「该步已 done 的终态检查点 -> 下一步/任务终态」之间
                # （review M3 修复）：该步输出已落库，直接续跑后续步骤/终态
                prior = await self._completed_step_outputs(task_id, step_index + 1)
                status, error = await self._run_chain_steps(
                    task_id,
                    chain,
                    task.input,
                    start_index=step_index + 1,
                    prior_outputs=prior,
                    lines=lines,
                )
                return await self._finalize(
                    task_id, task.chain_id, status, error, lines
                )
            # 与状态机 PAUSED+EVENT_RESUME 同路径校验（目标必须是运行相位，fail-closed）
            target = transition(
                Phase.PAUSED,
                EVENT_RESUME,
                TransitionContext(resume_phase=target),
            )
            outcome = await self._execute_step(
                task_id,
                point.step_id,
                step_index,
                chain.steps[step_index],
                step_row.input or {},
                resume_phase=target,
                resume_state=point.state,
                chain_id=task.chain_id,
            )
            lines.extend(outcome.lines)
            if outcome.status in ("failed", "paused"):
                return await self._finalize(
                    task_id, task.chain_id, outcome.status, outcome.error, lines
                )
            prior = await self._completed_step_outputs(task_id, step_index + 1)
            status, error = await self._run_chain_steps(
                task_id,
                chain,
                task.input,
                start_index=step_index + 1,
                prior_outputs=prior,
                lines=lines,
            )
            return await self._finalize(task_id, task.chain_id, status, error, lines)
        except Exception as exc:
            # 意外异常兜底：不留 running 脏行（review C1 修复）
            return await self._finalize(
                task_id, task.chain_id, "failed", f"resume 未捕获异常：{exc}", lines
            )

    def request_pause(self) -> None:
        """请求暂停（人工 pause 指令，§2.2 转换表）；相位循环顶部生效。

        v0.1 CLI 不接信号，但 resume 路径与 PAUSED 同一条（人工暂停 =
        主动制造一次"优雅崩溃"，§2.3）。
        """
        self._pause_requested = True

    # ---- 链级编排 ----

    async def _run_chain_steps(
        self,
        task_id: int,
        chain: Any,
        task_input: dict,
        *,
        start_index: int,
        prior_outputs: list[dict],
        lines: list[PhaseLine],
    ) -> tuple[str, str | None]:
        """从 start_index 起顺序执行链步骤；返回 (终态, error)。"""
        outputs: list[dict] = list(prior_outputs)
        for i in range(start_index, len(chain.steps)):
            decl = chain.steps[i]
            step_input = self._resolve_step_input(chain, decl, i, task_input, outputs)
            step_id = await _db.create_step(
                self._engine, task_id, step_index=i, worker_id=decl.worker, input_=step_input
            )
            await _db.update_task(
                self._engine, task_id, current_step=i, current_step_row=step_id
            )
            outcome = await self._execute_step(
                task_id, step_id, i, decl, step_input, chain_id=chain.id
            )
            lines.extend(outcome.lines)
            if outcome.status == "failed":
                return "failed", outcome.error
            if outcome.status == "paused":
                return "paused", None
            outputs.append(outcome.output if outcome.output is not None else {})
        return "done", None

    async def _execute_step(
        self,
        task_id: int,
        step_id: int,
        step_index: int,
        step_decl: Any,
        step_input: dict,
        *,
        resume_phase: Phase | None = None,
        resume_state: dict[str, Any] | None = None,
        chain_id: str | None = None,  # 当前链 id（ACT 装配进 EngineContext，v0.2 T4）
    ) -> _StepOutcome:
        """单工序状态机循环（§2.2 转换表；每相位转换**前**落检查点）。"""
        worker = self._registry.workers.get(step_decl.worker)
        if worker is None:
            raise ValueError(f"工序 {step_decl.worker} 未在注册表登记")
        manager = CheckpointManager(self._checkpoint_dao, task_id=task_id)
        lines: list[PhaseLine] = []
        phase = resume_phase if resume_phase is not None else Phase.INIT
        phase_output: Any = None
        memory: dict[str, Any] = {}
        if resume_state is not None:
            phase_output = resume_state.get("phase_output")
            memory = dict(resume_state.get("memory_state") or {})

        while True:
            if phase in TERMINAL_PHASES:
                break
            if self._pause_requested:
                next_phase = transition(phase, EVENT_PAUSE)
                await manager.write(
                    step_id,
                    phase,
                    next_phase,
                    phase_output=phase_output,
                    memory_state=memory,
                )
                await _db.update_step(
                    self._engine, step_id, phase=next_phase.value, status="paused"
                )
                lines.append(
                    PhaseLine(phase.value, "paused（人工暂停，可 resume 续跑）")
                )
                return _StepOutcome("paused", lines, None, None)

            event: str | None = None
            new_memory: dict[str, Any] | None = None
            error: str | None = None
            if phase is Phase.INIT:
                event, phase_output, new_memory, error = await self._phase_init(
                    task_id, step_id, worker, step_input, lines
                )
            elif phase is Phase.REASON:
                if getattr(worker, "reason", "llm") == "none":
                    # 纯代码工序（worker.yaml 的 reason: none，v0.2 T4 技术定）：
                    # 无 LLM 调用——REASON 直通 ACT（llm_output=None，零 token 成本，
                    # 详设-v0.2 §7 演示链 demo_propose）
                    event = EVENT_OUTPUT_VALID
                    phase_output = None
                    lines.append(
                        PhaseLine("REASON", "skipped（reason: none，纯代码工序无 LLM 调用）")
                    )
                else:
                    event, phase_output, new_memory, error = await self._phase_reason(
                        task_id, step_id, worker, step_input, memory, lines
                    )
            elif phase is Phase.ACT:
                event, phase_output, new_memory, error = await self._phase_act(
                    task_id, step_id, worker, step_input, phase_output, memory, lines,
                    chain_id=chain_id,
                )
            elif phase is Phase.OBSERVE:
                event, phase_output, new_memory, error = await self._phase_observe(
                    step_id, worker, lines
                )
            elif phase is Phase.VERIFY:
                event, phase_output, new_memory, error = await self._phase_verify(
                    step_id, worker, lines
                )
            else:
                raise PhaseTransitionError(f"非法相位：{phase}")
            if new_memory is not None:
                memory = new_memory

            if event is None:
                # §2.2 表无此失败行（OBSERVE 读回失败）：按任务 FAILED 终态处理
                reason = error or f"{phase.value} 相位失败"
                lines.append(PhaseLine(phase.value, f"failed: {reason}"))
                await _db.update_step(
                    self._engine, step_id, status="failed", error=reason
                )
                return _StepOutcome("failed", lines, None, reason)

            next_phase = transition(
                phase, event, _transition_ctx(phase, event, memory, worker)
            )
            if next_phase is not Phase.FAILED:
                # 转换前落检查点（§2.3；FAILED 转换只记 error 不落检查点）
                await manager.write(
                    step_id,
                    phase,
                    next_phase,
                    phase_output=phase_output,
                    memory_state=memory,
                )
            await _db.update_step(self._engine, step_id, phase=next_phase.value)
            if next_phase is Phase.FAILED:
                reason = error or f"{phase.value} 相位失败"
                lines.append(PhaseLine(phase.value, f"failed: {reason}"))
                await _db.update_step(
                    self._engine, step_id, status="failed", error=reason
                )
                return _StepOutcome("failed", lines, None, reason)
            phase = next_phase

        lines.append(PhaseLine("DONE", "done"))
        output = phase_output if isinstance(phase_output, dict) else None
        return _StepOutcome("done", lines, output, None)

    # ---- 相位实现（每相位产出事件 + 相位产出 + 内存状态；§2.1/§2.2）----

    async def _phase_init(
        self,
        task_id: int,
        step_id: int,
        worker: Any,
        step_input: dict,
        lines: list[PhaseLine],
    ) -> tuple[str | None, Any, dict[str, Any], str | None]:
        """INIT：Policy 门禁（域权限/风险/越域读）+ 工序入参类型 + 上下文装配。"""
        # context 域 = provider 声明里的 domain（review M7 修复：不按 id 点分前缀
        # 推导——snake_case id 如 demo_greeting 推导不出 "demo"；声明域才是真相，
        # 与 loader L2 同源）
        context_domains: list[str] = []
        for ref in worker.context:
            decl = self._registry.context_providers.get(ref.id)
            context_domains.append(
                decl.domain.value if decl is not None else ref.id
            )
        policy = check_policy(worker.domain, worker.risk, context_domains)
        if not policy.ok:
            lines.append(PhaseLine("INIT", f"policy failed: {policy.reason}"))
            return EVENT_POLICY_FAILED, None, {}, policy.reason
        input_cls = self._resolve_model(worker.input.model)
        if input_cls is None:
            raise ValueError(
                f"工序 {worker.id} 的 input Model {worker.input.model} 不可解析"
                "（loader L3 应已拦截）"
            )
        try:
            validated = input_cls.model_validate(step_input)
        except ValidationError as exc:
            reason = f"工序入参未过 input Model {input_cls.__name__} 校验：{exc}"
            lines.append(PhaseLine("INIT", f"policy failed: {reason}"))
            return EVENT_POLICY_FAILED, None, {}, reason
        context_data = await self._pull_context(worker, step_input)
        memory = {
            "inputs": validated.model_dump(mode="json"),
            "context_data": {
                key: _json_plain(value) for key, value in context_data.items()
            },
        }
        lines.append(PhaseLine("INIT", policy.reason))  # 如 "policy ok (domain=demo, risk=read)"
        return EVENT_POLICY_OK, None, memory, None

    async def _phase_reason(
        self,
        task_id: int,
        step_id: int,
        worker: Any,
        step_input: dict,
        memory: dict[str, Any],
        lines: list[PhaseLine],
    ) -> tuple[str | None, Any, dict[str, Any], str | None]:
        """REASON：渲染 prompt -> call_llm -> 审计 -> 输出过 output Model 校验。"""
        if self._model_registry is None:
            raise ValueError("TaskRunner 未注入 model_registry，REASON 无法解析模型别名")
        model_config = self._model_registry.resolve(worker.model)
        model_str = model_config.model
        out_cls = self._resolve_model(worker.output.model)
        if out_cls is None:
            raise ValueError(
                f"工序 {worker.id} 的 output Model {worker.output.model} 不可解析"
                "（loader L3 应已拦截）"
            )
        timeout_s = worker.retry.timeout_s if worker.retry else _DEFAULT_TIMEOUT_S
        max_attempts = worker.retry.max_attempts if worker.retry else _DEFAULT_MAX_ATTEMPTS
        try:
            prompt = self._render_prompt(
                worker,
                step_input,
                memory.get("context_data", {}),
                self._config_for(worker),
            )
        except Exception as exc:
            reason = f"prompt 渲染失败：{exc}"
            lines.append(PhaseLine("REASON", f"render failed: {reason}"))
            return EVENT_LLM_FAILED, None, memory, reason
        try:
            agent = self._agent_factory(self._model_registry, worker.model, out_cls)
            result = await call_llm(
                agent,
                prompt,
                timeout_s=timeout_s,
                reask_limit=model_config.reask_limit,
                max_attempts=max_attempts,
                backoff=self._backoff,
                audit_gate=self._audit_gate,
            )
        except Exception as exc:  # LLM 层异常（无静默降级）：显式 FAILED
            reason = f"LLM 调用失败：{exc}"
            await self._audit.record(
                task_id=task_id,
                step_id=step_id,
                worker_id=worker.id,
                model=model_str,
                attempt=0,
                result="failed",
                input_full=prompt,
                output_full={"error": str(exc)},
                phase="REASON",
            )
            lines.append(PhaseLine("REASON", f"llm failed（已审计留痕）：{reason}"))
            return EVENT_LLM_FAILED, None, memory, reason
        llm_output = self._coerce_model(result.output, out_cls)
        if llm_output is None:
            reason = f"LLM 输出未过 output Model {out_cls.__name__} 校验（宁失败不假成功）"
            await self._audit.record(
                task_id=task_id,
                step_id=step_id,
                worker_id=worker.id,
                model=model_str,
                attempt=0,
                result="failed",
                input_full=prompt,
                output_full={"error": reason},
                phase="REASON",
            )
            lines.append(PhaseLine("REASON", f"output invalid: {reason}"))
            return EVENT_LLM_FAILED, None, memory, reason
        await self._audit.record(
            task_id=task_id,
            step_id=step_id,
            worker_id=worker.id,
            model=model_str,
            attempt=0,
            result="ok",
            input_full=prompt,
            output_full=llm_output.model_dump(mode="json"),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_ms=result.duration_ms,
            phase="REASON",
        )
        lines.append(
            PhaseLine(
                "REASON",
                f"llm ok（model={model_str}，tokens_in={result.input_tokens}，"
                f"tokens_out={result.output_tokens}，duration_ms={result.duration_ms}）",
            )
        )
        return EVENT_OUTPUT_VALID, llm_output, memory, None

    async def _phase_act(
        self,
        task_id: int,
        step_id: int,
        worker: Any,
        step_input: dict,
        phase_output: Any,
        memory: dict[str, Any],
        lines: list[PhaseLine],
        *,
        chain_id: str | None = None,  # 当前链 id（TaskProposal.source 追溯，v0.2 T4）
    ) -> tuple[str | None, Any, dict[str, Any], str | None]:
        """ACT：import 工序 run 模块（engine.registry.workers.<域>.<工序>.run），
        构造 EngineContext（inputs 过 input Model 校验；phase_output = REASON 的
        LLM 结果，经 ctx.llm_output 传给 run——工序执行副作用的依据，避免二次
        调 LLM（R4）；2026-08-28 集成修复），输出过 output Model 校验。"""
        attempt = int(memory.get("attempt", 0)) + 1
        memory = {**memory, "attempt": attempt}
        try:
            input_cls = self._resolve_model(worker.input.model)
            if input_cls is None:
                raise ValueError(
                    f"工序 {worker.id} 的 input Model {worker.input.model} 不可解析"
                )
            out_cls = self._resolve_model(worker.output.model)
            if out_cls is None:
                raise ValueError(
                    f"工序 {worker.id} 的 output Model {worker.output.model} 不可解析"
                )
            inputs_model = input_cls.model_validate(step_input)
            context_data = await self._rebuild_context_models(
                worker, memory.get("context_data") or {}
            )
            # llm_output 归一化为 output Model（review M1 修复）：正常路径 phase_output
            # 已是 Model；resume 路径从检查点还原的是 dict——必须还原成 Model 再交给
            # 工序（翻译工序 isinstance 校验依赖它；A4 对真实工序的续跑前提）
            llm_model = (
                self._coerce_model(phase_output, out_cls)
                if phase_output is not None
                else None
            )
            ctx = EngineContext(
                worker_id=worker.id,
                domain=worker.domain.value,
                inputs=inputs_model,
                config=self._config_for(worker),
                context_data=context_data,
                engine=self._engine,
                llm_output=llm_model,
                task_id=task_id,
                step_id=step_id,
                chain_id=chain_id,
            )
            output_value = await self._call_worker_run(worker, inputs_model, ctx)
            output_model = self._coerce_model(output_value, out_cls)
            if output_model is None:
                raise ValueError(
                    f"run 输出未过 output Model {out_cls.__name__} 校验"
                    "（R2：输出即类型，宁失败不假成功）"
                )
            await _db.update_step(
                self._engine, step_id, output=output_model.model_dump(mode="json")
            )
            lines.append(
                PhaseLine("ACT", f"side effect ok（worker={worker.id}，output 已落库）")
            )
            return EVENT_SIDE_EFFECT_OK, output_model, memory, None
        except Exception as exc:
            reason = f"副作用失败：{exc}"
            await _db.update_step(self._engine, step_id, attempt=attempt)
            lines.append(
                PhaseLine("ACT", f"side effect failed（attempt={attempt}）：{exc}")
            )
            return EVENT_SIDE_EFFECT_FAILED, None, memory, reason

    async def _phase_observe(
        self, step_id: int, worker: Any, lines: list[PhaseLine]
    ) -> tuple[str | None, Any, dict[str, Any], str | None]:
        """OBSERVE：读回副作用结果（v0.1 语义 = engine_step.output 已落库）。"""
        row = await _db.get_step(self._engine, step_id)
        output = row.output if row is not None else None
        if not output:
            reason = "OBSERVE 读回失败：engine_step.output 未落库（写了个寂寞，§2.1）"
            lines.append(PhaseLine("OBSERVE", f"readback failed: {reason}"))
            return None, None, {}, reason
        lines.append(
            PhaseLine("OBSERVE", f"readback ok（step={step_id}，output 已落库）")
        )
        return EVENT_READBACK_OK, output, None, None

    async def _phase_verify(
        self, step_id: int, worker: Any, lines: list[PhaseLine]
    ) -> tuple[str | None, Any, dict[str, Any], str | None]:
        """VERIFY：业务断言（v0.1 = 输出非空 + 输出过 output Model 校验）。"""
        row = await _db.get_step(self._engine, step_id)
        output = row.output if row is not None else None
        reasons: list[str] = []
        if not _assert_output_non_empty(output):
            reasons.append(
                "输出非空：engine_step.output 为空或全空字段（宁失败不假成功）"
            )
        out_cls = self._resolve_model(worker.output.model)
        if out_cls is not None:
            try:
                out_cls.model_validate(output if output is not None else {})
            except ValidationError as exc:
                reasons.append(
                    f"输出未过 output Model {out_cls.__name__} 校验：{exc}"
                )
        if reasons:
            reason = "VERIFY 断言失败：" + "；".join(reasons)
            lines.append(PhaseLine("VERIFY", f"assertions failed: {reason}"))
            return EVENT_ASSERTIONS_FAILED, None, {}, reason
        await _db.update_step(self._engine, step_id, status="done")
        lines.append(
            PhaseLine("VERIFY", "assertions ok（输出非空 + output Model 校验通过）")
        )
        return EVENT_ASSERTIONS_OK, output, None, None

    # ---- 装配助手 ----

    async def _pull_context(self, worker: Any, step_input: dict) -> dict[str, BaseModel]:
        """按工序 context 声明拉取 provider（v0.1 provider 注册 = 构造注入 providers）。"""
        out: dict[str, BaseModel] = {}
        for ref in worker.context:
            decl = self._registry.context_providers.get(ref.id)
            if decl is None:
                continue  # loader L2 已保证登记；防御跳过
            impl = self._providers.get(decl.provider)
            if impl is None:
                continue  # 无实现 = context_data 留空（v0.1 demo 语义）
            params = _resolve_provider_params(ref.params, step_input)
            params_cls = self._resolve_model(decl.params.model)
            if params_cls is not None:
                params = params_cls.model_validate(params)
            result = await impl(params)
            result_cls = self._resolve_model(decl.returns.model)
            if result_cls is not None:
                result = result_cls.model_validate(result)
            out[ref.id] = result
        return out

    async def _rebuild_context_models(
        self, worker: Any, raw: dict[str, Any]
    ) -> dict[str, BaseModel]:
        """把内存里的 context_data（JSON 形态）还原为 BaseModel（EngineContext 契约：
        {provider_id: BaseModel}，按 provider 声明的 returns Model 校验）。"""
        out: dict[str, BaseModel] = {}
        for ref in worker.context:
            if ref.id not in raw:
                continue
            value = raw[ref.id]
            decl = self._registry.context_providers.get(ref.id)
            if decl is not None and isinstance(value, dict):
                result_cls = self._resolve_model(decl.returns.model)
                if result_cls is not None:
                    value = result_cls.model_validate(value)
            out[ref.id] = value
        return out

    def _resolve_step_input(
        self, chain: Any, step_decl: Any, step_index: int, task_input: dict, prior_outputs: list[dict]
    ) -> dict:
        """链步骤 input 引用解析（§4.2：省略/task.input 直传；steps[n].output 前序）。"""
        expr = step_decl.input
        if expr is None:
            return dict(task_input)
        if isinstance(expr, str):
            return self._resolve_expr(expr, task_input, prior_outputs)
        return {
            field: self._resolve_expr(e, task_input, prior_outputs)
            for field, e in expr.items()
        }

    def _resolve_expr(self, expr: str, task_input: dict, prior_outputs: list[dict]) -> Any:
        match = _EXPR_TASK_RE.match(expr)
        if match is not None:
            return _path_get(task_input, match.group(1))
        match = _EXPR_STEP_RE.match(expr)
        if match is not None:
            idx = int(match.group(1))
            if idx >= len(prior_outputs):
                raise ValueError(
                    f"步骤引用 steps[{idx}].output 超出已产出步骤数（前序 {len(prior_outputs)} 步）"
                )
            return _path_get(prior_outputs[idx], match.group(2))
        raise ValueError(f"步骤 input 表达式不可解析：{expr!r}（loader L6 应已拦截）")

    async def _call_worker_run(self, worker: Any, inputs_model: BaseModel, ctx: EngineContext) -> Any:
        """import 工序 run 模块并按 P3-1 契约调用（run 支持 async/sync，await 包装）。"""
        module = importlib.import_module(
            f"engine.registry.workers.{worker.domain.value}.{worker.id}.run"
        )
        run_fn = getattr(module, "run", None)
        if run_fn is None:
            raise ValueError(f"工序 {worker.id} 的 run 模块缺少 run 函数（P3-1 工序契约）")
        value = run_fn(inputs_model, ctx)
        if inspect.isawaitable(value):
            value = await value
        return value

    def _render_prompt(
        self, worker: Any, step_input: dict, context_data: dict, config: dict
    ) -> str:
        """prompt.md 模板渲染：{input.*} {context.<provider_id>.*} {config.*}。"""
        template = self._prompt_text(worker)
        namespace = {
            "input": step_input,
            "context": {key: _json_plain(value) for key, value in context_data.items()},
            "config": config,
        }

        def _repl(match: re.Match[str]) -> str:
            node: Any = namespace
            for part in match.group(1).split("."):
                if not isinstance(node, dict) or part not in node:
                    raise ValueError(
                        f"prompt 模板变量 {{{match.group(1)}}} 无法解析"
                        "（fail-closed，宁失败不假成功）"
                    )
                node = node[part]
            return _display_value(node)

        return _PLACEHOLDER_RE.sub(_repl, template)

    def _prompt_text(self, worker: Any) -> str:
        text = self._prompt_cache.get(worker.id)
        if text is None:
            text = (self._worker_dir(worker) / worker.prompt).read_text(encoding="utf-8")
            self._prompt_cache[worker.id] = text
        return text

    def _config_for(self, worker: Any) -> dict[str, Any]:
        cfg = self._config_cache.get(worker.id)
        if cfg is None:
            cfg = _load_config_dir(self._worker_dir(worker), worker.config_dir)
            self._config_cache[worker.id] = cfg
        return cfg

    def _worker_dir(self, worker: Any) -> Path:
        if self._repo_root is None:
            raise ValueError(
                "TaskRunner 未注入 repo_root，无法读取工序 prompt/config（R10 规格外置）"
            )
        return (
            self._repo_root
            / "engine"
            / "registry"
            / "workers"
            / worker.domain.value
            / worker.id
        )

    def _resolve_model(self, name: str) -> type[BaseModel] | None:
        """模型名 -> Model 类（loader L3 搜索序同源；供 output_type/校验用）。"""
        for module_name in _MODEL_SEARCH_MODULES:
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            attr = getattr(module, name, None)
            if isinstance(attr, type) and issubclass(attr, BaseModel):
                return attr
        return None

    def _coerce_model(self, value: Any, model_cls: type[BaseModel]) -> BaseModel | None:
        """结果归一化为 Model：已是 Model 直用；dict 则校验；其余 None（宁失败不假成功）。"""
        if isinstance(value, model_cls):
            return value
        if isinstance(value, dict):
            try:
                return model_cls.model_validate(value)
            except ValidationError:
                return None
        return None

    async def _completed_step_outputs(self, task_id: int, before_index: int) -> list[dict]:
        """取该任务已完成步骤（step_index < before_index）的输出（resume 续跑前序用）。"""
        async with AsyncSession(self._engine) as session:
            rows = (
                await session.execute(
                    select(_db.EngineStep)
                    .where(
                        _db.EngineStep.task_id == task_id,
                        _db.EngineStep.step_index < before_index,
                    )
                    .order_by(_db.EngineStep.step_index)
                )
            ).scalars()
            return [row.output if row.output is not None else {} for row in rows]

    async def _finalize(
        self,
        task_id: int,
        chain_id: str,
        status: str,
        error: str | None,
        lines: list[PhaseLine],
    ) -> TaskResult:
        """任务终态落库（§2.2：update_task(status=done/failed/paused, finished_at)。"""
        if status == "done":
            await _db.update_task(
                self._engine, task_id, status="done", finished_at=_now()
            )
        elif status == "failed":
            await _db.update_task(
                self._engine,
                task_id,
                status="failed",
                error=error,
                finished_at=_now(),
            )
        else:  # paused：非终态（可恢复），不落 finished_at
            await _db.update_task(self._engine, task_id, status="paused")
        return TaskResult(task_id, chain_id, status, error, lines)


# ---- 纯函数助手 ----


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _transition_ctx(phase: Phase, event: str, memory: dict[str, Any], worker: Any) -> TransitionContext:
    """转换条件快照（§2.2 条件列；fail-closed：条件与事件矛盾即抛）。"""
    if phase is Phase.INIT:
        return TransitionContext(policy_ok=event == EVENT_POLICY_OK)
    if phase is Phase.REASON:
        return TransitionContext(result_valid=event == EVENT_OUTPUT_VALID)
    if phase is Phase.ACT:
        max_attempts = worker.retry.max_attempts if worker.retry else _DEFAULT_MAX_ATTEMPTS
        attempt = int(memory.get("attempt", 0))
        # 总尝试 = 1 次初调 + max_attempts 次重试（§14 与 LLM 层 range(max_attempts+1)
        # 同口径）；attempt = 已执行的次数（review M2 修复：原 max_attempts-attempt 少算一次）
        return TransitionContext(
            side_effect_ok=event == EVENT_SIDE_EFFECT_OK,
            attempt_left=max_attempts + 1 - attempt,
        )
    if phase is Phase.VERIFY:
        return TransitionContext(assertions_ok=event == EVENT_ASSERTIONS_OK)
    return TransitionContext()


def _path_get(node: Any, path: str | None) -> Any:
    """沿点分路径取字典值（task.input.x / steps[n].output.x / input.x）。"""
    if not path:
        return node
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"引用路径 {path!r} 不存在（{part!r}）")
        node = node[part]
    return node


def _resolve_provider_params(params: dict[str, str], step_input: dict) -> dict[str, Any]:
    """provider 查询参数（§4.1：值来自 input 字段，形如 input.customer_id）。"""
    out: dict[str, Any] = {}
    for key, expr in (params or {}).items():
        match = _INPUT_REF_RE.match(expr)
        if match is not None:
            out[key] = _path_get(step_input, match.group(1))
        else:
            out[key] = expr  # 字面值原样（params 无静态校验，运行期容错）
    return out


def _json_plain(value: Any) -> Any:
    """Model -> JSON 安全 dict（memory/检查点/EngineContext.context_data 形态统一）。"""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _display_value(value: Any) -> str:
    """prompt 模板变量的展示形态：容器 JSON，标量 str，None 空串。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _load_config_dir(worker_dir: Path, config_dir: str | None) -> dict[str, Any]:
    """工序 config/ 目录 yaml 合并（规格外置；顶层映射逐文件合并）。"""
    if not config_dir:
        return {}
    merged: dict[str, Any] = {}
    base = worker_dir / config_dir
    for path in sorted(base.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data is None:
            continue
        if not isinstance(data, dict):
            raise ValueError(f"config yaml {path} 必须是映射（R10 规格外置）")
        merged.update(data)
    return merged


def _assert_output_non_empty(output: dict | None) -> bool:
    """VERIFY 断言 1「输出非空」：dict 非空且至少一个非空字段值（宁失败不假成功）。"""
    if not output:
        return False
    return any(value is not None and value != "" for value in output.values())
