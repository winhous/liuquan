"""Action 消费者注册表（详设-v0.2 §5 + 详设-v0.3 §5.4：链完成由注册的消费者
把产出落业务库）。

引擎不直接写业务库——链完成后由调用方（engine/server.py，常驻进程）按
产出 action_id 从本表取消费者并调用（消费者为纯代码，AI 全程不碰库）。
v0.2：tm.proposal -> TM 转交器；v0.3 新增：crm.candidate -> CRM 候选消费者
（todo_generate 产出候选落 crm.todo_candidate，决策 19/26）。

消费者契约（R21 契约依赖）：async (proposal: dict, **注入) -> ConsumeOutcome；
audit_lookup / whitelist / biz_client / registry 全部由调用方注入（R12，
测试零网络零真服务）。
"""

from __future__ import annotations

from .crm_candidate import consume_todo_candidates
from .tm_proposal import (
    ConsumeOutcome,
    Consumer,
    consume_task_proposal,
)

__all__ = [
    "CONSUMERS",
    "ConsumeOutcome",
    "Consumer",
    "consume_task_proposal",
    "consume_todo_candidates",
]

# action_id -> 消费者（v0.2 tm.proposal；v0.3 + crm.candidate，决策 19；
# loader L10 target 白名单 {tm.proposal, crm.todo_candidate}）
CONSUMERS: dict[str, Consumer] = {
    "tm.proposal": consume_task_proposal,
    "crm.candidate": consume_todo_candidates,
}
