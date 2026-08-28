"""§6.1 ContractEvent：业务 -> 引擎的「我发生了什么」（详设-v0.1 §6.1）。

公共契约，v0.1 定稿即冻结。v0.1 只定义类型与登记位
（engine/registry/events/<域>.yaml，T4b 落地），不做消费。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ContractEvent(BaseModel):
    """业务事件：任何业务事实变化都可发事件，事件登记表决定触发哪条链。"""

    event_type: str  # 注册表登记的事件 id，如 crm.message_received
    domain: str  # crm/tm/erp/seo/scrape
    source: str  # 发出模块标识，如 web.crm
    occurred_at: datetime  # 事件发生时刻（业务侧时刻，非入队时刻）
    payload: dict[str, Any]  # 须符合该 event_type 声明的 payload Model
    dedup_key: str | None = None  # 去重键：同键在 dedup_window 内不重复触发（v0.4 用）
