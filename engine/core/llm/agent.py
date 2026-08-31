"""PydanticAI 调用封装（详设-v0.1 §7.2/§7.3、§13、§14；T8）。

两个公共入口：

- ``agent_factory(registry, alias, output_type, *, agent_cls=None)``
  按 models.yaml 建 Agent。默认建真 PydanticAI Agent（OpenAI 兼容模型，
  DeepSeek 走此路径）；``agent_cls`` 为测试桩注入点（§13：封装暴露
  agent_factory 参数，测试注入桩，被测代码对桩零感知，禁止 monkeypatch
  PydanticAI 内部）。桩构造契约：``agent_cls(config, output_type,
  reask_limit=config.reask_limit)``。
- ``call_llm(agent, prompt, *, timeout_s, reask_limit, max_attempts, backoff,
  audit_gate)``
  重试/超时/降级三级策略（§7.2、§14）：
  - 第 1 层 re-ask：输出不过 output_type 校验 -> PydanticAI 自动带错误信息
    重问（``retries=reask_limit``，上限 2）；耗尽 = 抛 ``LLMValidationError``
  - 第 2 层工序重试：网络错误/超时/5xx -> 指数退避后重调，上限 2 次
    （``max_attempts=2``）；耗尽 = 抛 ``LLMRetryExhaustedError`` 带全部原因
  - 第 3 层链失败：前两层耗尽即显式异常上抛，**无静默降级**（§7.2：
    DeepSeek 不可用 = 任务 FAILED + 显式原因，不产任何假结果）
  - 单次调用超时 ``timeout_s``（默认 30s）：HTTP 层（model_settings）+
    asyncio.wait_for 硬上限双保险
  - ``backoff`` 可注入（测试用 0s 退避，不真等 5s/30s）
  - 先审计后调用（§7.3）：``audit_gate`` 传入时，任何 LLM 调用前先
    ``ensure_writable()``，不可写即拒绝（审计不可写 = 调用不允许发生）
  - 用量上报（§7.3）：从 PydanticAI RunUsage 提取 input/output tokens +
    耗时毫秒，随 ``LLMCallResult`` 一起返回

退避解读（技术定，偏差见报告）：§14「指数退避 5s→30s」实现为
``min(backoff * 2**k, 30s)``——基值 5s 指数增长、封顶 30s（区间描述）；
max_attempts=2 时实际等待 5s、10s。

真 Agent 的 HTTP 客户端显式 ``trust_env=False``（不读环境代理）：
本机 ALL_PROXY 含 socks 协议时 pydantic-ai 默认客户端构造即失败，显式
关闭环境代理使构造结果与运行机网络环境解耦、可复现；客户端由引擎持有
（pydantic-ai 不托管用户传入的 client，CLI 进程随进程退出释放，
v0.2 常驻 runner 再纳入生命周期管理）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

import httpx2
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .audit_gate import AuditGate
from .models_config import ModelConfig, ModelRegistry

__all__ = [
    "LLMCallResult",
    "LLMError",
    "LLMRetryExhaustedError",
    "LLMValidationError",
    "agent_factory",
    "call_llm",
]

T = TypeVar("T")

# 单次调用默认超时（§14 技术定）
DEFAULT_TIMEOUT_S = 30.0
# re-ask 上限（§14）
DEFAULT_REASK_LIMIT = 2
# 工序重试次数（§14：重试 2 次 = 1 次初调 + 2 次重试）
DEFAULT_MAX_ATTEMPTS = 2
# 退避基值（§14：5s 起）
DEFAULT_BACKOFF_S = 5.0
# 退避封顶（§14：→30s）
BACKOFF_CAP_S = 30.0

# v0.1 真路径唯一支持的 provider（OpenAI 兼容协议；扩展 = 此处加分支，
# models.yaml 的 fallback 多模型位 v0.3 后按故障率再议，§7.2）
_SUPPORTED_PROVIDERS = frozenset({"deepseek"})

# 传输层可重试异常的类名兜底（openai/httpx 为 pydantic-ai 传递依赖，
# 不直接 import，按类名判定 + status_code 鸭子判定；builtin 直判）
_RETRYABLE_TRANSPORT_NAMES = frozenset(
    {
        # openai
        "APIConnectionError",
        "APITimeoutError",
        # httpx / httpx2
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "RequestError",
    }
)


class LLMError(Exception):
    """LLM 调用层异常基类（调用方 catch 此族即捕获全部显式失败）。"""


class LLMRetryExhaustedError(LLMError):
    """工序重试耗尽（§7.2 第 2 层）：网络错误/超时/5xx 重试 max_attempts 次后仍失败。

    attempts = 总尝试次数（1 次初调 + 重试次数）；causes = 每次失败的原因
    （审计留痕用）。
    """

    def __init__(self, *, attempts: int, causes: tuple[Exception, ...]) -> None:
        self.attempts = attempts
        self.causes = causes
        detail = "；".join(f"第{i + 1}次: {exc!r}" for i, exc in enumerate(causes))
        super().__init__(
            f"LLM 调用重试耗尽：共 {attempts} 次尝试全部失败（{detail}）"
            "——无静默降级（§7.2），任务应带此显式原因终态"
        )


class LLMValidationError(LLMError):
    """re-ask 耗尽（§7.2 第 1 层）：输出始终不过 output_type 校验
    （PydanticAI UnexpectedModelBehavior 包装，保留原因为 cause）。"""


@dataclass(frozen=True, slots=True)
class LLMCallResult(Generic[T]):
    """一次成功的 LLM 调用结果 + 用量（§7.3：token/耗时随结果上报）。"""

    output: T
    input_tokens: int
    output_tokens: int
    duration_ms: int


def agent_factory(
    registry: ModelRegistry,
    alias: str,
    output_type: type[T],
    *,
    agent_cls: Callable[..., Any] | None = None,
) -> Any:
    """按 models.yaml 建 Agent（§13 构造注入点）。

    ``agent_cls=None``：建真 PydanticAI Agent（OpenAI 兼容模型，provider 须为
    v0.1 支持的 deepseek，否则抛 LLMError——显式失败不静默）。
    ``agent_cls=桩类``：按契约 ``agent_cls(config, output_type,
    reask_limit=config.reask_limit)`` 构造，桩与真 Agent 共用 run 契约
    （``run(prompt, *, model_settings, retries, ...)``），被测代码零感知。
    """
    config = registry.resolve(alias)
    if agent_cls is None:
        return _build_real_agent(config, output_type)
    return agent_cls(config, output_type, reask_limit=config.reask_limit)


def _build_real_agent(config: ModelConfig, output_type: type[T]) -> Agent[T]:
    """真 Agent：models.yaml -> OpenAI 兼容模型 -> PydanticAI Agent。

    模型串（config.model）只来自 models.yaml（规范 R11：代码永不内联模型串）。
    """
    if config.provider not in _SUPPORTED_PROVIDERS:
        raise LLMError(
            f"provider {config.provider!r} 不受支持（v0.1 仅 deepseek，"
            "OpenAI 兼容协议；扩展需改 engine/core/llm/agent.py 并留变更日志）"
        )
    http_client = httpx2.AsyncClient(
        timeout=httpx2.Timeout(timeout=config.timeout_s, connect=5.0),
        trust_env=False,  # 不读环境代理（见模块 docstring 决策说明）
    )
    provider = OpenAIProvider(
        base_url=config.base_url,
        api_key=config.api_key,
        http_client=http_client,
    )
    model = OpenAIChatModel(config.model, provider=provider)
    return Agent(model, output_type=output_type, retries=config.reask_limit)


async def call_llm(
    agent: Any,
    prompt: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    reask_limit: int = DEFAULT_REASK_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF_S,
    backoff_cap: float = BACKOFF_CAP_S,
    audit_gate: AuditGate | None = None,
) -> LLMCallResult[Any]:
    """封装一次 LLM 调用（重试/超时/re-ask/审计门/用量，见模块 docstring）。

    ``agent`` 可以是真 PydanticAI Agent 或 tests/ 注入的桩（二者共用 run
    契约）；``backoff=0`` 可在测试中关闭退避等待。
    ``backoff_cap`` 退避封顶秒（详设 §7.3：engine.backoff_cap 注入点）。
    """
    # 先审计后调用（§7.3）：审计不可写 = 调用不允许发生，任何调用前先检查
    if audit_gate is not None:
        audit_gate.ensure_writable()

    causes: list[Exception] = []
    for attempt in range(max_attempts + 1):  # 1 次初调 + max_attempts 次重试
        try:
            return await _run_once(agent, prompt, timeout_s, reask_limit)
        except Exception as exc:
            if isinstance(exc, UnexpectedModelBehavior):
                # re-ask 耗尽（第 1 层）：不重试、不降级，显式包装上抛
                raise LLMValidationError(
                    f"re-ask 耗尽：输出始终未过 output_type 校验（{exc}）"
                ) from exc
            if not _is_retryable_error(exc):
                raise  # 非网络/超时/5xx：原样上抛（显式失败，不静默）
            causes.append(exc)
            if attempt < max_attempts:
                await asyncio.sleep(_backoff_seconds(backoff, attempt, backoff_cap))
    raise LLMRetryExhaustedError(
        attempts=max_attempts + 1, causes=tuple(causes)
    ) from causes[-1]


async def _run_once(
    agent: Any, prompt: str, timeout_s: float, reask_limit: int
) -> LLMCallResult[Any]:
    """单次尝试：wait_for 硬超时 + model_settings 超时双保险，取用量与耗时。"""
    start = time.monotonic()
    result = await asyncio.wait_for(
        agent.run(
            prompt,
            model_settings={"timeout": timeout_s},
            retries=reask_limit,
        ),
        timeout=timeout_s,
    )
    duration_ms = int((time.monotonic() - start) * 1000)
    input_tokens, output_tokens = _extract_usage(result)
    return LLMCallResult(
        output=result.output,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )


def _extract_usage(result: Any) -> tuple[int, int]:
    """从 RunUsage 提取 input/output tokens（§7.3）。

    2.35.1 中 ``usage`` 是属性；老版本是方法——两者都兼容；
    桩可只带部分字段（缺省按 0 计，不阻断调用）。
    """
    usage = getattr(result, "usage", None)
    if callable(usage):
        usage = usage()
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return input_tokens, output_tokens


def _is_retryable_error(exc: Exception) -> bool:
    """可重试判定（§7.2 第 2 层触发面）：网络错误/超时/5xx。"""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if exc.__class__.__name__ in _RETRYABLE_TRANSPORT_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and 500 <= status < 600


def _backoff_seconds(backoff: float, retry_index: int, cap: float = BACKOFF_CAP_S) -> float:
    """指数退避：``min(backoff * 2**k, cap)``（§14「5s→30s」区间描述）。"""
    return min(backoff * (2 ** retry_index), cap)
