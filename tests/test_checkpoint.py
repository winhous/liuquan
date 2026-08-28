"""T9 检查点测试（详设-v0.1 §2.3 崩溃恢复；任务 T9 checkpoint.py）。

覆盖（全部以详设 §2.3 为唯一依据）：
- write 把「相位已产出结果 + 续跑所需内存状态」打包进 state JSON
  （相位枚举归一化为字符串落盘；task_id 由构造绑定）
- resume_point 取最后一条检查点，返回 {step_id, from_phase, to_phase, state}
- 无检查点返回 None；按 task 隔离；state 往返保真
- REASON 已成功的 LLM 结果（Pydantic Model 形态）必须进 state——
  续跑不重烧 token（验收断言 A4 的前奏，行为在此固化）
- 反向：不可 JSON 序列化的产出/内存状态拒绝落盘（宁失败不假成功）

DAO 用桩（FakeCheckpointDAO 住 tests/，R12 构造注入），零网络零 DB。
断言只断检查点读写与恢复定位本身；runner 侧行为不在本测试范围。
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from engine.core.checkpoint import CheckpointManager, CheckpointRow, ResumePoint
from engine.core.statemachine import Phase


class _ReasonOutput(BaseModel):
    """REASON 相位产出（LLM 结果）的最小形态：结构对齐工序输出 Model。"""

    summary: str
    confidence: float


class FakeCheckpointDAO:
    """CheckpointDAO 桩（住 tests/，R12 构造注入）。

    state_as_json=True 时把 state 以 JSON 文本形态存储，模拟 DAO 返回
    未解析 JSONB 的形态（resume_point 需兼容 dict 与 str 两种 state）。
    """

    def __init__(self, *, state_as_json: bool = False) -> None:
        self.state_as_json = state_as_json
        self.rows: list[CheckpointRow] = []

    async def write_checkpoint(
        self,
        *,
        task_id: int,
        step_id: int,
        from_phase: str,
        to_phase: str,
        state: object,
    ) -> None:
        stored = json.dumps(state) if self.state_as_json else state
        self.rows.append(
            CheckpointRow(
                id=len(self.rows) + 1,
                task_id=task_id,
                step_id=step_id,
                from_phase=from_phase,
                to_phase=to_phase,
                state=stored,
            )
        )

    async def last_checkpoint(self, task_id: int) -> CheckpointRow | None:
        matched = [row for row in self.rows if row.task_id == task_id]
        return matched[-1] if matched else None


def _make_manager(dao: FakeCheckpointDAO, *, task_id: int = 7) -> CheckpointManager:
    return CheckpointManager(dao, task_id=task_id)


# ---- write：相位产出 + 内存状态打包进 state JSON ----

@pytest.mark.asyncio
async def test_write_packs_phase_output_and_memory_state() -> None:
    """write 把相位产出与内存状态打包进 state（§2.3：相位已产出结果 +
    续跑所需内存状态 JSON），相位归一化为字符串落盘。"""
    dao = FakeCheckpointDAO()
    mgr = _make_manager(dao, task_id=7)

    await mgr.write(
        3,
        Phase.REASON,
        Phase.ACT,
        phase_output=_ReasonOutput(summary="需要补充发票", confidence=0.9),
        memory_state={"reask_remaining": 2, "accumulated": ["a", "b"]},
    )

    assert len(dao.rows) == 1
    row = dao.rows[0]
    assert row.task_id == 7
    assert row.step_id == 3
    assert row.from_phase == "REASON"  # Phase 枚举归一化为字符串
    assert row.to_phase == "ACT"
    assert row.state["phase_output"] == {"summary": "需要补充发票", "confidence": 0.9}
    assert row.state["memory_state"] == {"reask_remaining": 2, "accumulated": ["a", "b"]}


@pytest.mark.asyncio
async def test_write_accepts_string_phases() -> None:
    """字符串相位原样透传（DAO 往返形态）。"""
    dao = FakeCheckpointDAO()
    mgr = _make_manager(dao)

    await mgr.write(1, "INIT", "REASON", phase_output=None, memory_state={})

    assert dao.rows[0].from_phase == "INIT"
    assert dao.rows[0].to_phase == "REASON"


# ---- resume_point：恢复定位 ----

@pytest.mark.asyncio
async def test_resume_point_returns_last_checkpoint() -> None:
    """取最后一条检查点，返回 {step_id, from_phase, to_phase, state}（§2.3）。"""
    dao = FakeCheckpointDAO()
    mgr = _make_manager(dao, task_id=7)
    await mgr.write(1, Phase.INIT, Phase.REASON, phase_output=None, memory_state={"n": 1})
    await mgr.write(2, Phase.REASON, Phase.ACT, phase_output={"done": True}, memory_state={"n": 2})

    point = await mgr.resume_point(7)

    assert isinstance(point, ResumePoint)
    assert point.step_id == 2
    assert point.from_phase == "REASON"
    assert point.to_phase == "ACT"
    assert point.state == {"phase_output": {"done": True}, "memory_state": {"n": 2}}


@pytest.mark.asyncio
async def test_resume_point_no_checkpoint_returns_none() -> None:
    """无检查点返回 None（无恢复点，任务从头跑）。"""
    dao = FakeCheckpointDAO()
    mgr = _make_manager(dao, task_id=7)

    assert await mgr.resume_point(7) is None


@pytest.mark.asyncio
async def test_resume_point_isolated_by_task() -> None:
    """恢复定位按 task 隔离（不同任务互不串检查点）。"""
    dao = FakeCheckpointDAO()
    mgr = _make_manager(dao, task_id=7)
    await mgr.write(1, Phase.INIT, Phase.REASON, phase_output=None, memory_state={})

    assert await mgr.resume_point(9) is None
    assert await mgr.resume_point(7) is not None


# ---- state 保真 ----

@pytest.mark.asyncio
async def test_resume_point_state_roundtrip_fidelity() -> None:
    """state 往返保真：写入的相位产出与内存状态原样取回。"""
    dao = FakeCheckpointDAO()
    mgr = _make_manager(dao, task_id=7)
    phase_output = {"result": "ok", "items": [1, 2, 3], "nested": {"k": "v"}}
    memory_state = {"attempt": 1, "retries": 2}

    await mgr.write(2, Phase.REASON, Phase.ACT, phase_output=phase_output, memory_state=memory_state)

    point = await mgr.resume_point(7)
    assert point.state == {
        "phase_output": {"result": "ok", "items": [1, 2, 3], "nested": {"k": "v"}},
        "memory_state": {"attempt": 1, "retries": 2},
    }


@pytest.mark.asyncio
async def test_resume_point_parses_json_string_state() -> None:
    """DAO 返回 JSON 文本形态的 state 时同样解析（兼容 JSONB 未解析形态）。"""
    dao = FakeCheckpointDAO(state_as_json=True)
    mgr = _make_manager(dao, task_id=7)

    await mgr.write(1, Phase.INIT, Phase.REASON, phase_output={"ready": True}, memory_state={})

    point = await mgr.resume_point(7)
    assert point.state == {"phase_output": {"ready": True}, "memory_state": {}}


# ---- REASON 结果进 state（A4 前奏：续跑不重烧 token 的行为固化） ----

@pytest.mark.asyncio
async def test_reason_llm_result_lands_in_checkpoint_state() -> None:
    """REASON 已成功的 LLM 结果必须进检查点 state（详设 §2.3 关键保证）。

    验收断言 A4 依赖此行为：REASON 完成后崩溃，resume 时 runner 从
    state["phase_output"] 取回 LLM 结果直接续跑，不重烧 token——
    「state 里带 LLM 结果」在此固化。
    """
    dao = FakeCheckpointDAO()
    mgr = _make_manager(dao, task_id=7)
    llm_result = _ReasonOutput(summary="需要补充发票", confidence=0.98)

    await mgr.write(3, Phase.REASON, Phase.ACT, phase_output=llm_result, memory_state={})

    point = await mgr.resume_point(7)
    assert point.state["phase_output"] == {"summary": "需要补充发票", "confidence": 0.98}


# ---- 反向：不可序列化产出拒绝落盘 ----

@pytest.mark.asyncio
async def test_write_rejects_non_json_safe_phase_output() -> None:
    """反向：不可 JSON 序列化的相位产出 -> TypeError，不落半成品检查点。"""
    dao = FakeCheckpointDAO()
    mgr = _make_manager(dao)

    with pytest.raises(TypeError):
        await mgr.write(1, Phase.REASON, Phase.ACT, phase_output=object(), memory_state={})

    assert dao.rows == []


@pytest.mark.asyncio
async def test_write_rejects_non_json_safe_memory_state() -> None:
    """反向：内存状态含不可序列化值 -> TypeError，不落半成品检查点。"""
    dao = FakeCheckpointDAO()
    mgr = _make_manager(dao)

    with pytest.raises(TypeError):
        await mgr.write(
            1, Phase.REASON, Phase.ACT, phase_output=None, memory_state={"bad": object()}
        )

    assert dao.rows == []
