"""§6.3 Action：引擎 -> 业务的「你可以建议我做什么」（详设-v0.1 §6.3）。

公共契约，v0.1 定稿即冻结。ActionDeclaration 登记进注册表
（engine/registry/actions/<域>.yaml，T4b 落地）；ActionResult 是引擎
运行时产出，随提案走。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ActionDeclaration(BaseModel):
    """动作声明：业务侧对外暴露的可建议动作（transaction 不可登记）。"""

    action_id: str  # 如 crm.create_followup_task
    domain: str
    risk: Literal["read", "suggest", "write"]  # transaction 不可登记（loader 拒载，L7）
    output_model: str  # 产出数据 Model 名
    target: str  # 一期唯一合法值 tm.proposal（loader 校验：引擎对业务的影响只有一个出口）


class ActionResult(BaseModel):
    """动作结果：引擎运行时产出，随提案走。"""

    action_id: str
    risk: str
    payload: dict[str, Any]  # 须过该 action 声明的 output_model 校验
