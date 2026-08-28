"""T9 审计写入与脱敏摘要（详设-v0.1 §3 engine_audit 表 / §7.3 先审计后调用 /
§8 审计与脱敏；任务 T9 audit.py）。

§3 表注释：「成功/重问/失败都记」——record() 对 result 三态
（ok/reask/failed）一视同仁，全部落库（A3 可查）。
§7.3 原文：「审计不可写 = 调用不允许发生（先审计后调用的顺序约束，写进
测试）」——DbAuditGate 实现 engine/core/llm/audit_gate.py 的 AuditGate
Protocol，runner 把它传给 call_llm：任何 LLM 调用前先 ensure_writable()。
§8 审计与脱敏：
- stdout/日志通道：只许 task/worker/phase/状态/模型/token 数/耗时——
  audit_summary 只读安全字段，格式模板不含任何正文形态（结构性不可能
  出现 prompt/输出全文/密钥，测试固化「摘要不含正文与密钥」）
- engine_audit 表通道：允许 prompt 与输出**全文**（input_full/output_full），
  禁止密钥与环境变量值——脱敏责任在写入侧：调用方不传密钥进 prompt 由
  其他层保证（R20，本模块不 scrub 正文，scrub 会破坏审计可抽查性）

依赖设计（技术定）：本模块不 import engine/core/db.py（T6 并行实现中），
只依赖本模块声明的 AuditDAO Protocol（engine/core/db.py 的子集）——
Wave 3 runner 集成时把真 DAO 外观注入即可（R21：契约依赖，R12 构造注入）。

结构：
- AuditRecorder.record(...) -> AuditRecord：写一条审计（三态都记）
- DbAuditGate：审计可写性门（§7.3 先审计后调用，实现 AuditGate Protocol）
- audit_summary(row) -> str：stdout/日志安全摘要（§8）
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol

from engine.core.llm.audit_gate import AuditGateError

__all__ = [
    "AuditDAO",
    "AuditRecord",
    "AuditRecorder",
    "DbAuditGate",
    "audit_summary",
    "AuditGateError",
]

# engine_audit.result 三态（详设 §3 表注释「ok / reask / failed」）
_RESULT_VALUES = frozenset({"ok", "reask", "failed"})


class AuditDAO(Protocol):
    """审计 DAO 外观（engine/core/db.py 的子集，Wave 3 注入真实现）。

    字段对齐 engine_audit 表 11 列（详设 §3）；input_full/output_full 存
    全文（JSONB）。密钥不传进 prompt 由调用方保证（§8 脱敏责任在写入侧）。
    """

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
    ) -> None: ...

    async def get_audit(self, task_id: int) -> list: ...


@dataclass(frozen=True)
class AuditRecord:
    """一条审计记录（对齐 engine_audit 表 11 列 + phase 非表列）。

    phase：非表列（engine_audit 无相位列），由调用方在调用发生时的相位
    传入，仅供 §8 stdout 摘要展示；record() 返回的 AuditRecord 携带，
    经 get_audit 取回的 DB 行无此字段（摘要显示 "-"）。
    """

    task_id: int
    step_id: int
    worker_id: str
    model: str
    attempt: int
    result: str
    input_full: Any = None
    output_full: Any = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    phase: str | None = None


class AuditRecorder:
    """审计写入（§3：成功/重问/失败都记；构造注入 AuditDAO，R12）。"""

    def __init__(self, dao: AuditDAO) -> None:
        self._dao = dao

    async def record(
        self,
        *,
        task_id: int,
        step_id: int,
        worker_id: str,
        model: str,
        attempt: int,
        result: str,
        input_full: Any = None,
        output_full: Any = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        duration_ms: int | None = None,
        phase: str | None = None,
    ) -> AuditRecord:
        """写一条审计（成功/重问/失败都记，result 三态校验）。

        input_full/output_full 存全文（详设 §3）；密钥不传进 prompt 由
        其他层保证（§8 脱敏责任在写入侧，R20），本模块不 scrub 正文。
        phase 非表列（见 AuditRecord），供立即打印安全摘要。
        返回构建的 AuditRecord。
        """
        if result not in _RESULT_VALUES:
            raise ValueError(
                f"非法审计 result：{result!r}（engine_audit.result 只许 "
                "ok/reask/failed，详设 §3）"
            )
        record = AuditRecord(
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
            phase=phase,
        )
        await self._dao.append_audit(
            task_id=record.task_id,
            step_id=record.step_id,
            worker_id=record.worker_id,
            model=record.model,
            attempt=record.attempt,
            result=record.result,
            input_full=record.input_full,
            output_full=record.output_full,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            duration_ms=record.duration_ms,
        )
        return record


class DbAuditGate:
    """审计可写性门（§7.3 先审计后调用；实现 AuditGate Protocol）。

    ensure_writable() 为同步方法（Protocol 定，engine/core/llm/audit_gate.py；
    call_llm 在触发任何 LLM 调用前同步调用）。探活以「注入判定函数」实现：
    可注入对 DB 的一次最小探活（如最小查询的同步包装），Wave 3 runner
    注入真探活；测试注入 lambda。
    未注入探活函数 = 无法证明审计可写 -> fail-closed 抛 AuditGateError
    （宁失败不假成功：审计不可写 = 调用不允许发生）。
    """

    def __init__(self, writable_check: Callable[[], bool] | None = None) -> None:
        self._writable_check = writable_check

    def ensure_writable(self) -> None:
        """审计可写 -> 正常返回；不可写/未配置 -> 抛 AuditGateError。"""
        if self._writable_check is None:
            raise AuditGateError(
                "审计可写性未配置（未注入探活函数），fail-closed 拒绝 LLM 调用（§7.3）"
            )
        if not self._writable_check():
            raise AuditGateError(
                "审计通道不可写：LLM 调用被拒绝（§7.3 原文「审计不可写 = "
                "调用不允许发生」）"
            )


def audit_summary(row: AuditRecord | Mapping[str, Any]) -> str:
    """stdout/日志安全摘要（§8：task/worker/phase/状态/模型/token/耗时）。

    只读安全字段，格式模板不含任何正文形态——prompt/输出全文/密钥结构性
    不可能出现（测试固化「摘要不含正文与密钥」）。phase 缺省（DB 行）显示 "-"。
    """
    d = _row_as_dict(row)
    return (
        f"audit task={_display(d.get('task_id'))} "
        f"step={_display(d.get('step_id'))} "
        f"worker={_display(d.get('worker_id'))} "
        f"phase={_display(d.get('phase'))} "
        f"result={_display(d.get('result'))} "
        f"model={_display(d.get('model'))} "
        f"tokens_in={_display(d.get('input_tokens'))} "
        f"tokens_out={_display(d.get('output_tokens'))} "
        f"duration_ms={_display(d.get('duration_ms'))}"
    )


def _row_as_dict(row: AuditRecord | Mapping[str, Any]) -> dict[str, Any]:
    """行统一为 dict（AuditRecord -> asdict；Mapping 原样拷贝；其余拒绝）。"""
    if isinstance(row, AuditRecord):
        return asdict(row)
    if isinstance(row, Mapping):
        return dict(row)
    raise TypeError(f"audit_summary 不支持的行形态：{type(row).__name__}")


def _display(value: Any) -> str:
    """安全展示：None/空 -> "-"，其余按 str 展示（只用于安全字段）。"""
    if value is None or value == "":
        return "-"
    return str(value)
