"""T8 PydanticAI 调用层（engine/core/llm/）测试：models.yaml 加载/拒载、env: 解析、
agent_factory 注入点、call_llm 重试/超时/re-ask/usage、审计 gate（详设 §7、§13、§14）。

TDD 正反：加载拒载（非 env: 前缀/缺字段/重复别名/坏 yaml/缺环境变量）、
重试耗尽报错、re-ask 耗尽报错、审计不可写拒绝调用，均有正向与反向断言。

桩家族（FakeAgent 等）只住本文件（tests/，规范 R12：桩只许住 tests/；
lint P3-4 对 tests/ 豁免）。agent_factory 构造注入桩（§13），
被测代码对桩零感知，禁止 monkeypatch PydanticAI 内部。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
样本里的 URL / 密钥直值一律运行期拼接构造（_url/_sk_key），本文件的任何
单一字符串常量不得含完整 URL scheme、不得以 sk- 开头、不得给敏感名赋
字面量、不得直接读 os.environ（只用 monkeypatch.setenv）——否则 lint 扫
真仓库时会把本测试文件自身报红（判据见 engine/lint/p2.py 模块 docstring）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior

from engine.core.llm import (
    AuditGateError,
    LLMCallResult,
    LLMError,
    LLMRetryExhaustedError,
    LLMValidationError,
    ModelConfig,
    ModelRegistry,
    ModelsConfigError,
    agent_factory,
    call_llm,
    load_models,
)


# ---- P2 自检安全的样本构造（运行期拼接，见模块 docstring）----

def _url() -> str:
    """测试用 base_url（运行期拼接，防本文件出现完整 URL scheme 字面量）。"""
    return "ht" + "tps://api.example.invalid/v1"


def _sk_key() -> str:
    """测试用 api_key 直值（运行期拼接，防本文件出现 sk- 开头字面量）。"""
    return "s" + "k-" + "test-key-0123456789abcdef"


def _valid_models_yaml(
    model: str = "deepseek-chat",
    *,
    timeout_s: int = 30,
    reask_limit: int = 2,
) -> str:
    """合法 models.yaml 样本（§7.1 结构；timeout_s/reask_limit 可省）。"""
    return (
        "models:\n"
        "  default:\n"
        "    provider: deepseek\n"
        f"    model: {model}\n"
        "    base_url: env:DEEPSEEK_BASE_URL\n"
        "    api_key: env:DEEPSEEK_API_KEY\n"
        f"    timeout_s: {timeout_s}\n"
        f"    reask_limit: {reask_limit}\n"
    )


def _non_env_api_key_sample() -> str:
    """api_key 出现真值形态（非 env: 前缀）-> 拒载。"""
    return (
        "models:\n"
        "  default:\n"
        "    provider: deepseek\n"
        "    model: deepseek-chat\n"
        "    base_url: env:DEEPSEEK_BASE_URL\n"
        f"    api_key: {_sk_key()}\n"
    )


def _non_env_base_url_sample() -> str:
    """base_url 出现真值形态（非 env: 前缀）-> 拒载。"""
    return (
        "models:\n"
        "  default:\n"
        "    provider: deepseek\n"
        "    model: deepseek-chat\n"
        f"    base_url: {_url()}\n"
        "    api_key: env:DEEPSEEK_API_KEY\n"
    )


def _missing_field_sample() -> str:
    """缺必填字段 model -> 拒载。"""
    return (
        "models:\n"
        "  default:\n"
        "    provider: deepseek\n"
        "    base_url: env:DEEPSEEK_BASE_URL\n"
        "    api_key: env:DEEPSEEK_API_KEY\n"
    )


def _duplicate_alias_sample() -> str:
    """同一别名重复声明 -> 拒载（yaml 默认静默覆盖重复键，需专门检测）。"""
    return (
        "models:\n"
        "  default:\n"
        "    provider: deepseek\n"
        "    model: deepseek-chat\n"
        "    base_url: env:DEEPSEEK_BASE_URL\n"
        "    api_key: env:DEEPSEEK_API_KEY\n"
        "  default:\n"
        "    provider: deepseek\n"
        "    model: deepseek-reasoner\n"
        "    base_url: env:DEEPSEEK_BASE_URL\n"
        "    api_key: env:DEEPSEEK_API_KEY\n"
    )


def _bad_env_ref_sample(field_value: str) -> str:
    """env: 引用不合法（空变量名 / 非法变量名）-> 拒载。"""
    return (
        "models:\n"
        "  default:\n"
        "    provider: deepseek\n"
        "    model: deepseek-chat\n"
        "    base_url: env:DEEPSEEK_BASE_URL\n"
        f"    api_key: {field_value}\n"
    )


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试样本引用的两个环境变量（§7.1 env: 前缀解析）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _url())
    monkeypatch.setenv("DEEPSEEK_API_KEY", _sk_key())


# ---- 桩家族（住 tests/，R12；§13 桩设计）----


class FakeUsage:
    """迷你 RunUsage 形态：input/output tokens（§7.3 用量上报的取数源）。"""

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeRunResult:
    """迷你 AgentRunResult 形态：output + usage（2.35.1 中 usage 是属性）。"""

    def __init__(
        self, output: object, input_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        self.output = output
        self._usage = FakeUsage(input_tokens=input_tokens, output_tokens=output_tokens)

    @property
    def usage(self) -> FakeUsage:
        return self._usage


class FakeAgent:
    """标准桩（§13）：每次 run 返回合法 output，记录调用次数与 prompt。

    构造契约 = agent_factory 的 agent_cls 注入契约：
    ``agent_cls(config, output_type, reask_limit=...)``（§13 注入点）。
    output / usage 可测试后改属性定制。
    """

    def __init__(self, config: ModelConfig, output_type: type, reask_limit: int = 2) -> None:
        self.config = config
        self.output_type = output_type
        self.reask_limit = reask_limit
        self.calls = 0
        self.prompts: list[str] = []
        self.output: object = "fake-ok"
        self.usage = FakeUsage(input_tokens=12, output_tokens=7)

    async def run(
        self,
        prompt: str,
        *,
        model_settings: object | None = None,
        retries: int | None = None,
        **kwargs: object,
    ) -> FakeRunResult:
        self.calls += 1
        self.prompts.append(prompt)
        return FakeRunResult(
            self.output,
            input_tokens=self.usage.input_tokens,
            output_tokens=self.usage.output_tokens,
        )


class CapturingAgent(FakeAgent):
    """捕获桩（§13）：记录每次收到的 prompt（基类已记录，语义子类）。"""


class UnavailableAgent(FakeAgent):
    """失效桩（§13）：默认每次 run 抛 ConnectionError -> 触发工序重试。"""

    def __init__(
        self, config: ModelConfig, output_type: type, reask_limit: int = 2
    ) -> None:
        super().__init__(config, output_type, reask_limit)
        self.failures_left: float = float("inf")  # 测试可设为有限值：先败后成
        self._exc: Exception = ConnectionError("网络不可达（fake）")

    async def run(
        self,
        prompt: str,
        *,
        model_settings: object | None = None,
        retries: int | None = None,
        **kwargs: object,
    ) -> FakeRunResult:
        self.calls += 1
        if self.failures_left > 0:
            self.failures_left -= 1
            raise self._exc
        return FakeRunResult(
            self.output,
            input_tokens=self.usage.input_tokens,
            output_tokens=self.usage.output_tokens,
        )


class BadOutputAgent(FakeAgent):
    """坏输出桩（§13）：模拟 PydanticAI 校验失败自动 re-ask，耗尽抛异常。

    rounds 统计单次 run 内的模型调用轮数（初调 + re-ask 轮）：
    reask_limit=2 -> 3 轮（第 1 轮初调，第 2/3 轮 re-ask，耗尽抛）。
    """

    def __init__(self, config: ModelConfig, output_type: type, reask_limit: int = 2) -> None:
        super().__init__(config, output_type, reask_limit)
        self.rounds = 0

    async def run(
        self,
        prompt: str,
        *,
        model_settings: object | None = None,
        retries: int | None = None,
        **kwargs: object,
    ) -> FakeRunResult:
        budget = retries if retries is not None else self.reask_limit
        for _round in range(budget + 1):
            self.rounds += 1  # 每轮都是"返回类型不符 -> 触发 re-ask"
        raise UnexpectedModelBehavior("输出未过 output_type 校验，re-ask 耗尽（fake）")


class SlowAgent(FakeAgent):
    """慢桩：run 挂起超过调用超时（验证 wait_for 超时 -> 触发工序重试）。"""

    def __init__(
        self, config: ModelConfig, output_type: type, reask_limit: int = 2
    ) -> None:
        super().__init__(config, output_type, reask_limit)
        self.delay = 1.0

    async def run(
        self,
        prompt: str,
        *,
        model_settings: object | None = None,
        retries: int | None = None,
        **kwargs: object,
    ) -> FakeRunResult:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return FakeRunResult(self.output, input_tokens=12, output_tokens=7)


class ValueErrorAgent(FakeAgent):
    """非网络异常桩：抛 ValueError -> 不应被工序重试（只重试网络/超时/5xx）。"""

    async def run(
        self,
        prompt: str,
        *,
        model_settings: object | None = None,
        retries: int | None = None,
        **kwargs: object,
    ) -> FakeRunResult:
        self.calls += 1
        raise ValueError("模型配置错误（fake，不可重试）")


class HttpStatusAgent(FakeAgent):
    """HTTP 状态桩：每次 run 抛带 status_code 的异常（5xx 可重试 / 4xx 不重试）。"""

    def __init__(self, config: ModelConfig, output_type: type, reask_limit: int = 2) -> None:
        super().__init__(config, output_type, reask_limit)
        self.status_code = 500

    async def run(
        self,
        prompt: str,
        *,
        model_settings: object | None = None,
        retries: int | None = None,
        **kwargs: object,
    ) -> FakeRunResult:
        self.calls += 1
        exc = RuntimeError(f"HTTP {self.status_code}（fake）")
        exc.status_code = self.status_code  # type: ignore[attr-defined]
        raise exc


class MethodUsageAgent(FakeAgent):
    """usage 为方法的旧形态桩：验证取数兼容（老版本 usage() 是方法）。"""

    async def run(
        self,
        prompt: str,
        *,
        model_settings: object | None = None,
        retries: int | None = None,
        **kwargs: object,
    ) -> _MethodStyleResult:
        self.calls += 1
        return _MethodStyleResult(self.output)


class _MethodStyleResult:
    """usage 为方法形态的结果（老版本 pydantic-ai usage() 是方法，非属性）。"""

    def __init__(self, output: object) -> None:
        self.output = output
        self._usage = FakeUsage(input_tokens=3, output_tokens=4)

    def usage(self) -> FakeUsage:
        return self._usage


# ---- 审计 gate 桩 ----

class _UnwritableAuditGate:
    """审计不可写桩：ensure_writable 抛 AuditGateError（§7.3 调用被拒）。"""

    def __init__(self) -> None:
        self.checks = 0

    def ensure_writable(self) -> None:
        self.checks += 1
        raise AuditGateError("审计不可写（fake）：磁盘只读")


class _WritableAuditGate:
    """审计可写桩：ensure_writable 正常返回。"""

    def ensure_writable(self) -> None:
        return None


# ==== models.yaml 加载与拒载 ====

def test_load_valid_models_yaml_resolves_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    config = registry.resolve("default")
    assert isinstance(config, ModelConfig)
    assert config.alias == "default"
    assert config.provider == "deepseek"
    assert config.model == "deepseek-chat"
    assert config.base_url == _url()  # env: 前缀已解析为环境变量真值
    assert config.api_key == _sk_key()
    assert config.timeout_s == 30
    assert config.reask_limit == 2


def test_load_env_prefix_read_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env: 引用 = 启动时从环境变量读；文件内容与解析值不同源可证。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _url())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "s" + "k-" + "env-resolved-key")
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = ModelRegistry.load(path)
    assert registry.resolve("default").api_key == "s" + "k-" + "env-resolved-key"
    assert "env-resolved-key" not in path.read_text(encoding="utf-8")  # R20：文件无真值


def test_load_rejects_non_env_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：api_key 出现真值形态（非 env: 前缀）-> 拒载（R20）。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _non_env_api_key_sample())
    with pytest.raises(ModelsConfigError) as excinfo:
        load_models(path)
    assert "env:" in str(excinfo.value)


def test_load_rejects_non_env_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：base_url 出现真值形态 -> 拒载（R20）。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _non_env_base_url_sample())
    with pytest.raises(ModelsConfigError) as excinfo:
        load_models(path)
    assert "env:" in str(excinfo.value)


def test_load_rejects_missing_required_field(tmp_path: Path) -> None:
    """反向：缺必填字段 model -> 拒载。"""
    path = _write_yaml(tmp_path, _missing_field_sample())
    with pytest.raises(ModelsConfigError) as excinfo:
        load_models(path)
    assert "model" in str(excinfo.value)


def test_load_rejects_duplicate_alias(tmp_path: Path) -> None:
    """反向：别名重复 -> 拒载（yaml 默认静默覆盖，须专门检测）。"""
    path = _write_yaml(tmp_path, _duplicate_alias_sample())
    with pytest.raises(ModelsConfigError) as excinfo:
        load_models(path)
    assert "重复" in str(excinfo.value)


def test_load_rejects_bad_yaml_syntax(tmp_path: Path) -> None:
    """反向：yaml 语法坏 -> 拒载。"""
    path = _write_yaml(tmp_path, "models:\n  default: [unclosed\n")
    with pytest.raises(ModelsConfigError):
        load_models(path)


def test_load_rejects_missing_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """反向：env: 引用的环境变量未设置 -> 拒载（fail-fast，引擎不起）。"""
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    with pytest.raises(ModelsConfigError) as excinfo:
        load_models(path)
    assert "DEEPSEEK_BASE_URL" in str(excinfo.value)


def test_load_rejects_bad_env_ref_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：env: 引用格式坏（空变量名/非法变量名）-> 拒载。"""
    _set_env(monkeypatch)
    for bad in ("env:", "env:123BAD", "env:MISSING VAR"):
        path = _write_yaml(tmp_path, _bad_env_ref_sample(bad))
        with pytest.raises(ModelsConfigError):
            load_models(path)


def test_load_rejects_missing_models_key(tmp_path: Path) -> None:
    """反向：顶层缺 models 键 -> 拒载。"""
    path = _write_yaml(tmp_path, "foo: bar\n")
    with pytest.raises(ModelsConfigError):
        load_models(path)


def test_load_rejects_empty_models(tmp_path: Path) -> None:
    """反向：models 为空 -> 拒载（无模型可调，启动失败）。"""
    path = _write_yaml(tmp_path, "models: {}\n")
    with pytest.raises(ModelsConfigError):
        load_models(path)


def test_load_applies_defaults_for_timeout_and_reask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """timeout_s/reask_limit 缺省 -> 默认 30s / 2（§14 技术定）。"""
    _set_env(monkeypatch)
    path = _write_yaml(
        tmp_path,
        (
            "models:\n"
            "  default:\n"
            "    provider: deepseek\n"
            "    model: deepseek-chat\n"
            "    base_url: env:DEEPSEEK_BASE_URL\n"
            "    api_key: env:DEEPSEEK_API_KEY\n"
        ),
    )
    registry = ModelRegistry.load(path)
    config = registry.resolve("default")
    assert config.timeout_s == 30.0
    assert config.reask_limit == 2


def test_resolve_unknown_alias_raises_key_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未知别名 -> ModelsConfigError（v0.5 §7.1：提示识图模型未配置）。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    with pytest.raises(ModelsConfigError, match="未配置"):
        registry.resolve("no-such-alias")


def test_load_rejects_non_positive_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：timeout_s <= 0 / reask_limit 为负 -> 拒载。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml(timeout_s=0))
    with pytest.raises(ModelsConfigError):
        load_models(path)
    path2 = _write_yaml(tmp_path, _valid_models_yaml(reask_limit=-1))
    with pytest.raises(ModelsConfigError):
        load_models(path2)


def test_model_string_switch_zero_code_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A7 前奏：只改 models.yaml 的 model 串（deepseek-chat -> deepseek-reasoner），
    代码零改动，resolve 与新 Agent 都反映新模型串。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml(model="deepseek-chat"))
    assert load_models(path).resolve("default").model == "deepseek-chat"

    path.write_text(
        _valid_models_yaml(model="deepseek-reasoner"), encoding="utf-8"
    )
    registry = load_models(path)
    assert registry.resolve("default").model == "deepseek-reasoner"

    agent = agent_factory(registry, "default", str)
    assert agent.model.model_name == "deepseek-reasoner"


# ==== agent_factory：真 Agent 与桩注入（§13）====

def test_agent_factory_builds_real_pydantic_ai_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认路径：按 models.yaml 建真 PydanticAI Agent（OpenAI 兼容模型）。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str)
    assert type(agent).__module__.startswith("pydantic_ai")
    assert agent.model.model_name == "deepseek-chat"
    assert agent.model.provider.base_url.endswith("/v1/")
    assert agent.model.provider.client.api_key == _sk_key()


def test_agent_factory_injects_stub_via_agent_cls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注入点（§13）：agent_cls=桩类 -> 桩按 config/output_type/reask_limit 构造，
    被测代码对桩零感知。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=FakeAgent)
    assert isinstance(agent, FakeAgent)
    assert agent.config.model == "deepseek-chat"
    assert agent.reask_limit == 2


# ==== call_llm：正常/重试/超时/re-ask/usage/审计 gate ====

@pytest.mark.asyncio
async def test_call_llm_standard_stub_returns_output_with_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """标准桩：返回合法输出 + RunUsage 提取 tokens + 耗时毫秒（§7.3）。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=FakeAgent)
    start = time.monotonic()
    result = await call_llm(agent, "你好，请翻译", backoff=0.0)
    elapsed = time.monotonic() - start
    assert isinstance(result, LLMCallResult)
    assert result.output == "fake-ok"
    assert result.input_tokens == 12
    assert result.output_tokens == 7
    assert result.duration_ms >= 0
    assert elapsed < 1.0  # backoff=0 注入生效，无 5s/10s 等待


@pytest.mark.asyncio
async def test_call_llm_passes_prompt_through_to_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """捕获桩：prompt 原样传给 agent（工序组装好的 prompt 不被改写）。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=CapturingAgent)
    await call_llm(agent, "你好，请翻译", backoff=0.0)
    assert agent.prompts == ["你好，请翻译"]


@pytest.mark.asyncio
async def test_call_llm_retries_connection_error_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失效桩先败 2 次后成功：工序重试（§7.2 第 2 层）拉起，最终返回结果。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=UnavailableAgent)
    agent.failures_left = 2
    result = await call_llm(agent, "prompt", backoff=0.0)
    assert result.output == "fake-ok"
    assert agent.calls == 3  # 1 次初调 + 2 次重试（max_attempts=2）


@pytest.mark.asyncio
async def test_call_llm_retry_exhaustion_raises_explicit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：失效桩持续失败 -> 重试耗尽抛显式异常带原因，不产任何假结果（§7.2 无静默降级）。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=UnavailableAgent)
    with pytest.raises(LLMRetryExhaustedError) as excinfo:
        await call_llm(agent, "prompt", backoff=0.0)
    err = excinfo.value
    assert agent.calls == 3  # 1 次初调 + max_attempts=2 次重试，全部失败
    assert err.attempts == 3
    assert len(err.causes) == 3
    assert all(isinstance(c, ConnectionError) for c in err.causes)
    assert "网络不可达" in str(err)
    assert isinstance(err, LLMError)


@pytest.mark.asyncio
async def test_call_llm_backoff_injectable_zero_no_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """backoff=0 注入：重试不真等 5s/10s（默认退避会等 5s+10s）。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=UnavailableAgent)
    agent.failures_left = 2
    start = time.monotonic()
    await call_llm(agent, "prompt", backoff=0.0)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_call_llm_timeout_triggers_retry_and_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：单次调用超时（wait_for 硬上限）-> 触发工序重试 -> 耗尽报错。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=SlowAgent)
    with pytest.raises(LLMRetryExhaustedError):
        await call_llm(agent, "prompt", timeout_s=0.05, backoff=0.0)
    assert agent.calls == 3


@pytest.mark.asyncio
async def test_call_llm_bad_output_triggers_reask_and_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：坏输出 -> PydanticAI 自动 re-ask（reask_limit=2 -> 3 轮）-> 耗尽抛显式异常。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=BadOutputAgent)
    with pytest.raises(LLMValidationError) as excinfo:
        await call_llm(agent, "prompt", backoff=0.0)
    assert agent.rounds == 3  # 初调 1 + re-ask 2 次（§14 reask_limit=2）
    assert "re-ask" in str(excinfo.value) or "校验" in str(excinfo.value)
    assert isinstance(excinfo.value, LLMError)


@pytest.mark.asyncio
async def test_call_llm_reask_limit_zero_single_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reask_limit=0：不允许 re-ask，单轮即耗尽。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=BadOutputAgent)
    with pytest.raises(LLMValidationError):
        await call_llm(agent, "prompt", reask_limit=0, backoff=0.0)
    assert agent.rounds == 1


@pytest.mark.asyncio
async def test_call_llm_non_retryable_error_propagates_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：非网络异常（ValueError）不重试，原样抛出（只重试网络/超时/5xx）。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=ValueErrorAgent)
    with pytest.raises(ValueError):
        await call_llm(agent, "prompt", backoff=0.0)
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_call_llm_5xx_retried_then_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：5xx（status_code 500）属于工序重试触发面（§14），耗尽报显式错。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=HttpStatusAgent)
    agent.status_code = 500
    with pytest.raises(LLMRetryExhaustedError) as excinfo:
        await call_llm(agent, "prompt", backoff=0.0)
    assert agent.calls == 3
    assert len(excinfo.value.causes) == 3


@pytest.mark.asyncio
async def test_call_llm_4xx_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：4xx（status_code 400）不在触发面，原样抛出不重试（§7.2 只列网络/超时/5xx）。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=HttpStatusAgent)
    agent.status_code = 400
    with pytest.raises(RuntimeError):
        await call_llm(agent, "prompt", backoff=0.0)
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_call_llm_extracts_usage_from_method_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """兼容旧形态：result.usage 为方法时同样提取 tokens（老版本 pydantic-ai）。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=MethodUsageAgent)
    result = await call_llm(agent, "prompt", backoff=0.0)
    assert result.input_tokens == 3
    assert result.output_tokens == 4


@pytest.mark.asyncio
async def test_call_llm_audit_gate_unwritable_rejects_before_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向（§7.3 原文「审计不可写 = 调用不允许发生」）：
    先审计后调用的顺序约束——gate 拒绝时 agent 一次都没被调。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=FakeAgent)
    gate = _UnwritableAuditGate()
    with pytest.raises(AuditGateError):
        await call_llm(agent, "prompt", backoff=0.0, audit_gate=gate)
    assert gate.checks == 1
    assert agent.calls == 0  # 审计不可写 -> 调用不允许发生（顺序约束）


@pytest.mark.asyncio
async def test_call_llm_audit_gate_writable_allows_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正向：审计可写 -> gate 通过 -> 调用正常发生。"""
    _set_env(monkeypatch)
    path = _write_yaml(tmp_path, _valid_models_yaml())
    registry = load_models(path)
    agent = agent_factory(registry, "default", str, agent_cls=FakeAgent)
    result = await call_llm(agent, "prompt", backoff=0.0, audit_gate=_WritableAuditGate())
    assert result.output == "fake-ok"
    assert agent.calls == 1
