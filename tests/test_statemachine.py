"""T5 状态机转换守卫测试（详设-v0.1 §2.1/§2.2）。

覆盖（任务 T5 指定，全部以详设 §2.2 相位转换表为唯一依据）：
- 全链路合法序列 INIT->REASON->ACT->OBSERVE->VERIFY->DONE
- REASON 校验失败：re-ask 剩余 -> REASON；耗尽 -> FAILED
- ACT 副作用失败：attempt 剩余 -> ACT；耗尽 -> FAILED
- 任意运行相位 -> PAUSED（人工 pause）；PAUSED -> resume 恢复到检查点相位
- 反向（非法转换必须抛 PhaseTransitionError）：
  终态 DONE/FAILED 后任何转换 / VERIFY->REASON 回路 / INIT 直跳 VERIFY /
  未知事件 / PAUSED 非 resume 事件 / 条件矛盾 / 分支条件缺失 / resume 目标非法

断言只断状态转换本身；落盘（检查点/error）由 runner 负责，本测试不涉及。
"""

from __future__ import annotations

import pytest

from engine.core.statemachine import (
    EVENT_ASSERTIONS_FAILED,
    EVENT_ASSERTIONS_OK,
    EVENT_LLM_FAILED,
    EVENT_OUTPUT_INVALID,
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
    TRANSITION_TABLE,
    TransitionContext,
    transition,
)

_RUNNING = [Phase.INIT, Phase.REASON, Phase.ACT, Phase.OBSERVE, Phase.VERIFY]


# ---- 合法转换（正向） ----


def test_full_chain_legal_sequence_to_done() -> None:
    """全链路：INIT->REASON->ACT->OBSERVE->VERIFY->DONE（详设 §2.2 逐行）。"""
    assert (
        transition(Phase.INIT, EVENT_POLICY_OK, TransitionContext(policy_ok=True))
        is Phase.REASON
    )
    assert (
        transition(Phase.REASON, EVENT_OUTPUT_VALID, TransitionContext(result_valid=True))
        is Phase.ACT
    )
    assert (
        transition(
            Phase.ACT, EVENT_SIDE_EFFECT_OK, TransitionContext(side_effect_ok=True)
        )
        is Phase.OBSERVE
    )
    assert transition(Phase.OBSERVE, EVENT_READBACK_OK) is Phase.VERIFY
    assert (
        transition(Phase.VERIFY, EVENT_ASSERTIONS_OK, TransitionContext(assertions_ok=True))
        is Phase.DONE
    )


def test_init_policy_failed_to_failed() -> None:
    """INIT 校验不过 -> FAILED（不可重试，输入有问题，详设 §2.1）。"""
    assert (
        transition(Phase.INIT, EVENT_POLICY_FAILED, TransitionContext(policy_ok=False))
        is Phase.FAILED
    )


def test_reason_reask_remaining_stays_reason() -> None:
    """REASON 校验失败且 re-ask 剩余 -> REASON（重问，详设 §2.2）。"""
    assert (
        transition(
            Phase.REASON,
            EVENT_OUTPUT_INVALID,
            TransitionContext(result_valid=False, reask_remaining=1),
        )
        is Phase.REASON
    )


def test_reason_reask_exhausted_to_failed() -> None:
    """REASON re-ask 耗尽 -> FAILED（详设 §2.2）。"""
    assert (
        transition(
            Phase.REASON,
            EVENT_OUTPUT_INVALID,
            TransitionContext(result_valid=False, reask_remaining=0),
        )
        is Phase.FAILED
    )


def test_reason_llm_failed_to_failed() -> None:
    """REASON LLM 异常且重试耗尽 -> FAILED（详设 §2.2 / §7.2 无静默降级）。"""
    assert transition(Phase.REASON, EVENT_LLM_FAILED) is Phase.FAILED


def test_act_failure_attempt_remaining_stays_act() -> None:
    """ACT 副作用失败且 attempt 剩余 -> ACT（退避后重试本相位，详设 §2.2）。"""
    assert (
        transition(
            Phase.ACT,
            EVENT_SIDE_EFFECT_FAILED,
            TransitionContext(side_effect_ok=False, attempt_left=1),
        )
        is Phase.ACT
    )


def test_act_failure_attempt_exhausted_to_failed() -> None:
    """ACT 重试耗尽 -> FAILED（详设 §2.2）。"""
    assert (
        transition(
            Phase.ACT,
            EVENT_SIDE_EFFECT_FAILED,
            TransitionContext(side_effect_ok=False, attempt_left=0),
        )
        is Phase.FAILED
    )


@pytest.mark.parametrize("phase", _RUNNING)
def test_any_running_phase_pause_to_paused(phase: Phase) -> None:
    """任意运行相位 + 人工 pause -> PAUSED（详设 §2.2）。"""
    assert transition(phase, EVENT_PAUSE) is Phase.PAUSED


@pytest.mark.parametrize("target", _RUNNING)
def test_paused_resume_restores_checkpoint_phase(target: Phase) -> None:
    """PAUSED + resume -> 恢复到检查点记录的相位（详设 §2.2，非终态可恢复）。"""
    assert (
        transition(Phase.PAUSED, EVENT_RESUME, TransitionContext(resume_phase=target))
        is target
    )


# ---- 非法转换（反向，必须抛 PhaseTransitionError） ----


@pytest.mark.parametrize("terminal", [Phase.DONE, Phase.FAILED])
@pytest.mark.parametrize("event", [EVENT_PAUSE, EVENT_RESUME, EVENT_OUTPUT_VALID])
def test_terminal_phase_any_transition_raises(terminal: Phase, event: str) -> None:
    """DONE/FAILED 之后任何转换都抛（终态不可恢复，详设 §2.2）。"""
    with pytest.raises(PhaseTransitionError):
        transition(terminal, event)


def test_verify_to_reason_loop_forbidden() -> None:
    """不存在 VERIFY->REASON 回路（详设 §2.2 原文：v0.1 不做自动返工）。"""
    with pytest.raises(PhaseTransitionError):
        transition(
            Phase.VERIFY,
            EVENT_OUTPUT_INVALID,
            TransitionContext(result_valid=False, reask_remaining=1),
        )


def test_init_cannot_jump_to_verify() -> None:
    """INIT 直跳 VERIFY 抛（只许按表 INIT->REASON/FAILED/PAUSED）。"""
    with pytest.raises(PhaseTransitionError):
        transition(Phase.INIT, EVENT_READBACK_OK)


def test_unknown_event_raises() -> None:
    """未知事件抛（转换表代码写死，AI 无权决定下一步）。"""
    with pytest.raises(PhaseTransitionError):
        transition(Phase.INIT, "no_such_event")


def test_paused_accepts_only_resume() -> None:
    """PAUSED 只接受 resume；再 pause 抛。"""
    with pytest.raises(PhaseTransitionError):
        transition(Phase.PAUSED, EVENT_PAUSE)


def test_contradictory_condition_raises() -> None:
    """事件与 ctx 条件矛盾抛（fail-closed：宁失败不假成功）。"""
    with pytest.raises(PhaseTransitionError):
        transition(Phase.INIT, EVENT_POLICY_OK, TransitionContext(policy_ok=False))


def test_branch_condition_missing_raises() -> None:
    """分支事件缺剩余次数抛（调用方必须传 reask_remaining/attempt_left）。"""
    with pytest.raises(PhaseTransitionError):
        transition(
            Phase.REASON, EVENT_OUTPUT_INVALID, TransitionContext(result_valid=False)
        )


def test_resume_requires_running_phase() -> None:
    """resume 目标必须是运行相位；缺目标 / 指到终态都抛。"""
    with pytest.raises(PhaseTransitionError):
        transition(Phase.PAUSED, EVENT_RESUME)
    with pytest.raises(PhaseTransitionError):
        transition(Phase.PAUSED, EVENT_RESUME, TransitionContext(resume_phase=Phase.DONE))


def test_table_has_no_verify_to_reason_and_no_terminal_rows() -> None:
    """转换表结构断言：VERIFY 无输出回路事件；终态不是任何转换的起点。"""
    assert EVENT_OUTPUT_INVALID not in TRANSITION_TABLE[Phase.VERIFY]
    assert EVENT_OUTPUT_VALID not in TRANSITION_TABLE[Phase.VERIFY]
    assert Phase.DONE not in TRANSITION_TABLE
    assert Phase.FAILED not in TRANSITION_TABLE
