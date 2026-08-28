"""EngineContext：工序 run 函数签名（P3-1）的 ctx 类型（详设-v0.1 §4.1、§11；任务 T12a）。

工序契约（规范 R6 / lint P3-1）：``run(inputs: RegisteredModel, ctx: EngineContext) -> RegisteredModel``。
EngineContext 承载「规格外置」与「低耦合」（R10 架构层防线 / R21 契约依赖）：
工序只碰 inputs / config / context_data / engine 原语，不碰数据库连接、
不 import LLM SDK（R4）、不为缺失上游写死假数据（R6）。

字段语义（任务 T12a 技术定，供变更日志）：
- worker_id：工序声明 id（engine/registry/workers/<域>/<工序>/worker.yaml 的 id）
- domain：工序域（字符串形态，policy.Domain.value）
- inputs：已过本工序 input Model 校验的工序入参（Pydantic Model，R2 输出即类型同源）
- config：工序 config/ 目录全部 yaml 合并内容（规格外置：业务规格值只许住这里，
  不许字面量进 run.py——lint P1 词表自动生成的来源）
- context_data：声明的 Context provider 拉取结果 {provider_id: BaseModel}；
  无 provider / provider 实现未注入时为 {}（v0.1 demo 语义）
- engine：AsyncEngine（ACT 写库通道；v0.1 demo 工序不写 = None，引擎库写入
  由 runner 统一经 DAO 做，工序自身不碰连接）
- task_id / step_id：当前任务与工序实例 id（审计/定位用；None = 未关联）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

__all__ = ["EngineContext"]


@dataclass(frozen=True)
class EngineContext:
    """工序运行上下文（构造注入不可变；runner 在 ACT 相位装配后传给 run）。"""

    worker_id: str
    domain: str
    inputs: BaseModel
    config: dict[str, Any]
    context_data: dict[str, Any]
    engine: Any | None = None
    task_id: int | None = None
    step_id: int | None = None
