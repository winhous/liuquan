"""§6.2 Context：业务 -> 引擎的「我能给你看什么」（详设-v0.1 §6.2）。

两半结构：
- 声明：登记进注册表（详设 §4.3 的 context/<域>.yaml，T4b 落地）；
- 实现：业务侧提供 ``async def provide(params: Model) -> Model``，
  按 provider 标识注册（本模块的 ContextProvider 协议）。

引擎运行时按工序声明的 context 列表拉取，域不匹配直接拒绝。
Context 是只读供给，实现方保证无副作用。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


@runtime_checkable
class ContextProvider(Protocol):
    """Context provider 实现侧协议（只读供给，实现方保证无副作用）。

    实现类须提供 ``async def provide(self, params: BaseModel) -> BaseModel``：
    - params：工序声明里 context 条目声明的查询参数 Model；
    - 返回：该 provider 声明的 returns Model（数据经 Pydantic 校验后返回）。
    """

    async def provide(self, params: BaseModel) -> BaseModel: ...
