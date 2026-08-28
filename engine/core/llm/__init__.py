"""engine.core.llm：PydanticAI 调用层（详设-v0.1 §7；T8）。

全仓唯一合法持有 base_url/密钥形态处（lint P2 豁免子树）：
models.yaml 只存 ``env:`` 前缀引用（R20），真值在加载期由本层从环境变量
解析，业务代码永远接触不到模型配置（R4）。

公共 API：
- ``load_models(path)`` / ``ModelRegistry`` / ``ModelConfig`` /
  ``ModelsConfigError``：models.yaml 加载与拒载（§7.1）
- ``agent_factory(registry, alias, output_type, *, agent_cls=None)``：
  按 models.yaml 建 Agent；agent_cls 为测试桩注入点（§13）
- ``call_llm(...)`` / ``LLMCallResult`` / ``LLMError`` 族：
  重试/超时/re-ask/审计门/用量（§7.2、§7.3、§14）
- ``AuditGate`` / ``AuditGateError``：先审计后调用约束（§7.3）
"""

from __future__ import annotations

from .agent import (
    LLMCallResult,
    LLMError,
    LLMRetryExhaustedError,
    LLMValidationError,
    agent_factory,
    call_llm,
)
from .audit_gate import AuditGate, AuditGateError
from .models_config import (
    ModelConfig,
    ModelRegistry,
    ModelsConfigError,
    load_models,
)

__all__ = [
    "AuditGate",
    "AuditGateError",
    "LLMCallResult",
    "LLMError",
    "LLMRetryExhaustedError",
    "LLMValidationError",
    "ModelConfig",
    "ModelRegistry",
    "ModelsConfigError",
    "agent_factory",
    "call_llm",
    "load_models",
]
