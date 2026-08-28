"""T5 状态机：相位枚举 + §2.2 转换表（dict 写死）+ 转换守卫。

纯逻辑模块（详设-v0.1 §2）：不碰 DB、不碰 LLM、不做任何落盘。
转换表把「谁在什么条件下到哪」全部代码写死，AI 无权决定下一步；
转换前的落盘动作（检查点 / error 记录）由调用方（runner）负责，本模块不管。

接口约定：
- `transition(current, event, ctx)` 查表返回目标相位；不在表内一律抛
  PhaseTransitionError（终态后转换 / 未知事件 / 条件矛盾 / 分支条件缺失 /
  resume 目标非法）。
- 转换条件由调用方经 TransitionContext 传入：事件决定表行，ctx 决定分支
  （re-ask 剩余 -> REASON 还是 FAILED；ACT 重试剩余 -> ACT 还是 FAILED；
  resume 目标 = 检查点记录的相位）。本模块只判合法性 + 返回目标相位。

对应详设 §2.2 相位转换表逐行：
INIT->REASON(policy 全绿) / INIT->FAILED(校验不过) /
REASON->ACT(输出过类型校验) / REASON->REASON(re-ask<上限) /
REASON->FAILED(re-ask 耗尽或 LLM 异常且重试耗尽) /
ACT->OBSERVE(副作用成功) / ACT->ACT(副作用失败且 attempt<上限) /
ACT->FAILED(重试耗尽) / OBSERVE->VERIFY(读回成功) /
VERIFY->DONE(断言全绿) / VERIFY->FAILED(任一断言红) /
任意运行相位->PAUSED(人工 pause) / PAUSED->恢复(检查点记录的相位)。
不存在 VERIFY->REASON 回路（v0.1 不做自动返工）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple


class Phase(str, Enum):
    """引擎工序相位（详设 §2.1/§2.2）。DONE/FAILED 为终态；PAUSED 非终态可恢复。"""

    INIT = "INIT"  # Policy 校验 + 上下文装配（代码干活）
    REASON = "REASON"  # 调 LLM 产出结构化输出（AI 唯一出现点）
    ACT = "ACT"  # 执行副作用（写库 / 产出提案，代码干活）
    OBSERVE = "OBSERVE"  # 读回副作用结果（代码干活）
    VERIFY = "VERIFY"  # 业务断言（代码写死，宁失败不假成功）
    DONE = "DONE"  # 终态：断言全绿
    FAILED = "FAILED"  # 终态：显式失败（带原因）
    PAUSED = "PAUSED"  # 可恢复暂停（非终态，详设 §2.2 原文）


# ---- 转换事件（对应 §2.2 表的条件列，调用方按相位产出触发） ----
EVENT_POLICY_OK = "policy_ok"  # INIT：Policy 全绿
EVENT_POLICY_FAILED = "policy_failed"  # INIT：校验不过
EVENT_OUTPUT_VALID = "output_valid"  # REASON：输出过类型校验
EVENT_OUTPUT_INVALID = "output_invalid"  # REASON：校验失败（分支：re-ask 剩余）
EVENT_LLM_FAILED = "llm_failed"  # REASON：LLM 异常且重试耗尽
EVENT_SIDE_EFFECT_OK = "side_effect_ok"  # ACT：副作用成功
EVENT_SIDE_EFFECT_FAILED = "side_effect_failed"  # ACT：副作用失败（分支：attempt 剩余）
EVENT_READBACK_OK = "readback_ok"  # OBSERVE：读回成功
EVENT_ASSERTIONS_OK = "assertions_ok"  # VERIFY：断言全绿
EVENT_ASSERTIONS_FAILED = "assertions_failed"  # VERIFY：任一断言红
EVENT_PAUSE = "pause"  # 任意运行相位：人工暂停
EVENT_RESUME = "resume"  # PAUSED：人工恢复


class BranchRule(NamedTuple):
    """条件分支规则（§2.2 的 re-ask / ACT 重试两处分支）：

    ctx.<field> 未提供抛错（fail-closed）；> 0 到 yes（重试/重问），否则到 no。
    """

    yes: Phase
    no: Phase
    field: str


class PhaseTransitionError(ValueError):
    """非法相位转换：不在详设 §2.2 转换表内（终态后转换 / 未知事件 / 条件矛盾等）。"""


# PAUSED+resume 的哨兵：目标相位 = 检查点记录的相位（ctx.resume_phase）
_RESUME = object()

# 运行相位 / 终态（PAUSED 两者都不是：可恢复暂停态，详设 §2.2）
RUNNING_PHASES: frozenset[Phase] = frozenset(
    {Phase.INIT, Phase.REASON, Phase.ACT, Phase.OBSERVE, Phase.VERIFY}
)
TERMINAL_PHASES: frozenset[Phase] = frozenset({Phase.DONE, Phase.FAILED})

# 相位转换表（详设 §2.2 原文 dict 写死；AI 无权决定下一步）。
# 值：目标相位 | BranchRule（分支）| _RESUME（目标在 ctx.resume_phase）。
TRANSITION_TABLE: dict[Phase, dict[str, Phase | BranchRule | object]] = {
    Phase.INIT: {
        EVENT_POLICY_OK: Phase.REASON,
        EVENT_POLICY_FAILED: Phase.FAILED,
        EVENT_PAUSE: Phase.PAUSED,
    },
    Phase.REASON: {
        EVENT_OUTPUT_VALID: Phase.ACT,
        EVENT_OUTPUT_INVALID: BranchRule(Phase.REASON, Phase.FAILED, "reask_remaining"),
        EVENT_LLM_FAILED: Phase.FAILED,
        EVENT_PAUSE: Phase.PAUSED,
    },
    Phase.ACT: {
        EVENT_SIDE_EFFECT_OK: Phase.OBSERVE,
        EVENT_SIDE_EFFECT_FAILED: BranchRule(Phase.ACT, Phase.FAILED, "attempt_left"),
        EVENT_PAUSE: Phase.PAUSED,
    },
    Phase.OBSERVE: {
        EVENT_READBACK_OK: Phase.VERIFY,
        EVENT_PAUSE: Phase.PAUSED,
    },
    Phase.VERIFY: {
        EVENT_ASSERTIONS_OK: Phase.DONE,
        EVENT_ASSERTIONS_FAILED: Phase.FAILED,
        EVENT_PAUSE: Phase.PAUSED,
    },
    Phase.PAUSED: {
        EVENT_RESUME: _RESUME,
    },
}


@dataclass(frozen=True)
class TransitionContext:
    """转换条件快照：调用方（runner）按相位产出传入，本模块只读取判分支。

    字段语义（对应详设 §2.2 条件列）：
    - policy_ok: INIT 的 Policy 校验结果（真/假；None = 调用方未提供，事件名兜底）
    - result_valid: REASON 输出是否过类型校验
    - reask_remaining: 剩余 re-ask 次数（校验失败时决定 REASON 还是 FAILED）
    - attempt_left: 剩余 ACT 重试次数（副作用失败时决定 ACT 还是 FAILED）
    - side_effect_ok: ACT 副作用成败
    - assertions_ok: VERIFY 断言成败
    - resume_phase: PAUSED resume 的目标相位（检查点记录的相位，必须是运行相位）
    """

    policy_ok: bool | None = None
    result_valid: bool | None = None
    reask_remaining: int | None = None
    attempt_left: int | None = None
    side_effect_ok: bool | None = None
    assertions_ok: bool | None = None
    resume_phase: Phase | None = None


# 事件 -> (ctx 字段, 期望值)：提供了对应字段就校验一致性，矛盾即抛（fail-closed）
_CONDITION_EXPECT: dict[str, tuple[str, bool]] = {
    EVENT_POLICY_OK: ("policy_ok", True),
    EVENT_POLICY_FAILED: ("policy_ok", False),
    EVENT_OUTPUT_VALID: ("result_valid", True),
    EVENT_OUTPUT_INVALID: ("result_valid", False),
    EVENT_SIDE_EFFECT_OK: ("side_effect_ok", True),
    EVENT_SIDE_EFFECT_FAILED: ("side_effect_ok", False),
    EVENT_ASSERTIONS_OK: ("assertions_ok", True),
    EVENT_ASSERTIONS_FAILED: ("assertions_ok", False),
}


def is_running(phase: Phase) -> bool:
    """phase 是否运行相位（INIT/REASON/ACT/OBSERVE/VERIFY）。"""
    return phase in RUNNING_PHASES


def is_terminal(phase: Phase) -> bool:
    """phase 是否终态（DONE/FAILED）。PAUSED 非终态可恢复。"""
    return phase in TERMINAL_PHASES


def transition(
    current: Phase, event: str, ctx: TransitionContext | None = None
) -> Phase:
    """相位转换守卫（详设 §2.2）：查写死的转换表，返回目标相位；非法转换抛错。

    调用方（runner）负责转换前的落盘（检查点 / error 记录），本模块只判合法性。
    """
    if ctx is None:
        ctx = TransitionContext()
    if current in TERMINAL_PHASES:
        raise PhaseTransitionError(
            f"终态 {current.value} 之后不允许任何转换（详设 §2.2，DONE/FAILED 不可恢复）"
        )
    rules = TRANSITION_TABLE.get(current)
    if rules is None:
        raise PhaseTransitionError(f"未知相位 {current!r}（不在详设 §2.2 转换表）")
    rule = rules.get(event)
    if rule is None:
        raise PhaseTransitionError(
            f"非法转换：相位 {current.value} 上事件 {event!r} 不在转换表内"
            "（详设 §2.2 代码写死，AI 无权决定下一步）"
        )
    _check_condition_consistency(event, ctx)
    return _resolve_rule(rule, ctx)


def _check_condition_consistency(event: str, ctx: TransitionContext) -> None:
    """事件与 ctx 对应布尔条件矛盾即抛（fail-closed；字段未提供则跳过）。"""
    pair = _CONDITION_EXPECT.get(event)
    if pair is None:
        return
    field, expected = pair
    actual = getattr(ctx, field)
    if actual is not None and actual != expected:
        raise PhaseTransitionError(
            f"条件矛盾：事件 {event!r} 要求 ctx.{field}={expected!r}，"
            f"调用方传入 {actual!r}"
        )


def _resolve_rule(rule: Phase | BranchRule | object, ctx: TransitionContext) -> Phase:
    """解析表项：普通目标直接返回；分支读 ctx 计数；_RESUME 读 ctx.resume_phase。"""
    if rule is _RESUME:
        return _resolve_resume(ctx)
    if isinstance(rule, BranchRule):
        return _resolve_branch(rule, ctx)
    return rule


def _resolve_branch(rule: BranchRule, ctx: TransitionContext) -> Phase:
    remaining = getattr(ctx, rule.field)
    if remaining is None:
        raise PhaseTransitionError(
            f"条件缺失：事件分支需要 ctx.{rule.field}"
            "（调用方必须传入剩余次数，fail-closed）"
        )
    return rule.yes if remaining > 0 else rule.no


def _resolve_resume(ctx: TransitionContext) -> Phase:
    target = ctx.resume_phase
    if target is None:
        raise PhaseTransitionError(
            "resume 需要 ctx.resume_phase（检查点记录的相位，详设 §2.2）"
        )
    if target not in RUNNING_PHASES:
        raise PhaseTransitionError(
            f"resume 目标 {target.value} 非法：只能恢复到运行相位（详设 §2.2）"
        )
    return target
