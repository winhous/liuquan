"""Action 消费者注册表（详设-v0.2 §5：链完成由注册的消费者把提案落业务库）。

引擎不直接写业务库——链完成后由调用方（engine/server.py，v0.2 T5 常驻
进程）按提案的 action_id 从本表取消费者并调用（消费者为纯代码，AI 全程
不碰库）。v0.2 仅一条：tm.proposal -> TM 转交器（engine/actions/tm_proposal.py）。

消费者契约（R21 契约依赖）：async (proposal: TaskProposal, **注入) ->
ConsumeOutcome；audit_lookup / whitelist / tm_engine / registry 全部由
调用方注入（R12，测试零网络零真服务/嵌入式 PG）。
"""

from __future__ import annotations

from .tm_proposal import (
    ConsumeOutcome,
    Consumer,
    consume_task_proposal,
    create_tm_engine,
)

__all__ = [
    "CONSUMERS",
    "ConsumeOutcome",
    "Consumer",
    "consume_task_proposal",
    "create_tm_engine",
]

# action_id -> 消费者（v0.2 仅 tm.proposal 一条；ActionDeclaration.target
# 一期唯一合法值即 tm.proposal，loader L10 锁死，见详设 §6.3）
CONSUMERS: dict[str, Consumer] = {
    "tm.proposal": consume_task_proposal,
}
