"""先审计后调用约束（详设-v0.1 §7.3；T8）。

§7.3 原文：「审计不可写 = 调用不允许发生（先审计后调用的顺序约束，写进测试）」。

本模块只定义接口与门错误类型；真实现（连引擎库 engine_audit 表）属
engine/core/audit.py 任务。``call_llm`` 在触发任何 LLM 调用前调用
``AuditGate.ensure_writable()``：审计通道不可写时抛 ``AuditGateError``，
调用被拒绝——顺序约束由测试断言「gate 拒绝时 agent 一次都没被调」锁定。
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["AuditGate", "AuditGateError"]


class AuditGateError(Exception):
    """审计不可写：调用 LLM 前检查失败，本次调用被拒绝（§7.3）。"""


class AuditGate(Protocol):
    """审计可写性检查接口（§7.3 先审计后调用）。

    实现方（engine/core/audit.py 任务）在审计通道不可写时抛
    ``AuditGateError``；可写则正常返回。测试可注入实现该协议的桩，
    被测代码（call_llm）对审计实现零感知（R12 构造注入）。
    """

    def ensure_writable(self) -> None:
        """审计不可写 -> 抛 AuditGateError；可写 -> 正常返回。"""
        ...
