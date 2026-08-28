"""T9 检查点读写与恢复定位（详设-v0.1 §2.3 崩溃恢复；任务 T9 checkpoint.py）。

§2.3 原文要点（本模块全部依据）：
- 每次相位转换**前**写 engine_checkpoint（含：step 定位 + 当前相位 +
  相位已产出结果 + 续跑所需内存状态 JSON）
- 进程崩溃重启 -> resume <task_id>：取该任务最后一条 checkpoint，
  从其记录的相位继续
- 关键保证：**REASON 已成功的 LLM 结果在 checkpoint 里，续跑不重烧 token**
  （验收断言 A4；本模块在测试里固化「state 带 LLM 结果」这一行为）
- PAUSED 与崩溃恢复走同一条路径（人工暂停 = 主动制造一次"优雅崩溃"）

state 打包语义（对齐 engine_checkpoint.state JSONB 列，详设 §3 表注释
「续跑最小状态：相位产出结果+内存上下文」）：
- state = {"phase_output": <相位已产出结果>, "memory_state": <续跑所需内存状态>}
- step 定位（step_id）与当前相位（from_phase/to_phase）是表的独立列，
  不入 state JSON；write 的三个位置参数即承载
- Pydantic Model 形态的相位产出（REASON 的 LLM 结果）经 model_dump
  (mode="json") 序列化；不可 JSON 序列化 -> TypeError（宁失败不假成功，
  不落半成品检查点）

依赖设计（技术定）：本模块不 import engine/core/db.py（T6 并行实现中），
只依赖本模块声明的 CheckpointDAO Protocol（engine/core/db.py 的子集）——
Wave 3 runner 集成时把真 DAO 外观注入即可（R21：契约依赖，R12 构造注入）。

结构：
- CheckpointManager（构造绑定 task_id：runner 每次任务执行构造一个实例）
- write(step_id, from_phase, to_phase, *, phase_output, memory_state)：
  相位转换前落盘
- resume_point(task_id) -> ResumePoint | None：恢复定位（可独立于
  manager 构造前调用，因此显式接收 task_id）
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from engine.core.statemachine import Phase

__all__ = ["CheckpointDAO", "CheckpointRow", "ResumePoint", "CheckpointManager"]


class CheckpointDAO(Protocol):
    """检查点 DAO 外观（engine/core/db.py 的子集，Wave 3 注入真实现）。

    语义（对齐 engine_checkpoint 表，详设 §3）：
    - write_checkpoint：落一条检查点；state 为 JSON 安全 dict
      （JSONB 最终序列化由 DAO 负责）
    - last_checkpoint：返回该任务最后一条检查点（按 id 倒序取最新），
      无则 None；state 可以是 dict 或 JSON 文本（本模块两种都兼容）
    """

    async def write_checkpoint(
        self,
        *,
        task_id: int,
        step_id: int,
        from_phase: str,
        to_phase: str,
        state: Any,
    ) -> None: ...

    async def last_checkpoint(self, task_id: int) -> Any: ...  # CheckpointRow | None


@dataclass(frozen=True)
class CheckpointRow:
    """engine_checkpoint 行（DAO 返回形态，对齐详设 §3 表结构）。

    state：JSONB 已解析为 dict，或原始 JSON 文本（resume_point 两种都兼容）。
    """

    id: int
    task_id: int
    step_id: int
    from_phase: str
    to_phase: str
    state: dict[str, Any] | str
    created_at: datetime | None = None


@dataclass(frozen=True)
class ResumePoint:
    """恢复定位（详设 §2.3）：从检查点记录的相位继续。

    - step_id：恢复定位用（对应 engine_task.current_step_row）
    - from_phase/to_phase：检查点记录的相位转换；runner 以 to_phase 为
      恢复目标相位（经 Phase(...) 转回枚举喂状态机）
    - state：续跑最小状态 JSON——state["phase_output"] = 相位已产出结果
      （REASON 时为 LLM 结果，续跑不重烧 token），state["memory_state"] =
      续跑所需内存状态
    """

    step_id: int
    from_phase: str
    to_phase: str
    state: dict[str, Any]


class CheckpointManager:
    """检查点读写与恢复定位（§2.3）。

    构造注入 CheckpointDAO（R12：测试用 FakeCheckpointDAO，Wave 3 runner
    集成时注入 engine/core/db.py 的真 DAO）。task_id 构造绑定：runner
    每次任务执行构造一个实例，write 时无需重复传。
    """

    def __init__(self, dao: CheckpointDAO, *, task_id: int) -> None:
        self._dao = dao
        self._task_id = task_id

    async def write(
        self,
        step_id: int,
        from_phase: Phase | str,
        to_phase: Phase | str,
        *,
        phase_output: Any = None,
        memory_state: Mapping[str, Any] | None = None,
    ) -> None:
        """相位转换**前**落盘检查点（§2.3）。

        phase_output：相位已产出结果。REASON 已成功的 LLM 结果必须传入
        （Pydantic Model 形态自动序列化）——「续跑不重烧 token」的关键保证
        （验收断言 A4 依赖，测试固化）。不可 JSON 序列化 -> TypeError。
        memory_state：续跑所需内存状态（重试计数 / 累积上下文等）。
        """
        state = _pack_state(phase_output, memory_state)
        await self._dao.write_checkpoint(
            task_id=self._task_id,
            step_id=step_id,
            from_phase=_phase_text(from_phase),
            to_phase=_phase_text(to_phase),
            state=state,
        )

    async def resume_point(self, task_id: int) -> ResumePoint | None:
        """恢复定位（§2.3）：取该任务最后一条检查点，无则 None。

        恢复定位是独立查询（可在构造 manager 前调用），因此显式接收
        task_id；write 使用构造绑定的 task_id。
        """
        row = await self._dao.last_checkpoint(task_id)
        if row is None:
            return None
        return ResumePoint(
            step_id=row.step_id,
            from_phase=row.from_phase,
            to_phase=row.to_phase,
            state=_coerce_state(row.state),
        )


def _phase_text(phase: Phase | str) -> str:
    """相位归一化为字符串（Phase 枚举 -> .value；字符串原样透传）。"""
    return phase.value if isinstance(phase, Phase) else str(phase)


def _pack_state(
    phase_output: Any, memory_state: Mapping[str, Any] | None
) -> dict[str, Any]:
    """打包 state JSON（详设 §3 表注释：相位产出结果 + 内存上下文）。"""
    return {
        "phase_output": _json_safe(phase_output),
        "memory_state": _json_safe(
            dict(memory_state) if memory_state is not None else {}
        ),
    }


def _json_safe(value: Any) -> Any:
    """递归 JSON 安全化：Pydantic Model -> model_dump(mode="json")；
    容器递归；其余必须是 JSON 标量，否则 TypeError（宁失败不假成功）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    raise TypeError(
        f"检查点 state 含不可 JSON 序列化的值：{type(value).__name__} "
        "（宁失败不假成功：不落半成品检查点）"
    )


def _coerce_state(state: dict[str, Any] | str) -> dict[str, Any]:
    """DAO 返回的 state 统一为 dict（JSON 文本则解析；形态不支持 fail-closed）。"""
    if isinstance(state, str):
        try:
            parsed = json.loads(state)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"检查点 state JSON 解析失败：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("检查点 state JSON 必须是对象形态")
        return parsed
    if isinstance(state, dict):
        return dict(state)
    raise TypeError(
        f"检查点 state 形态不支持：{type(state).__name__}（期望 dict 或 JSON 文本）"
    )
