"""T4b 工序注册表 schema + loader 测试（详设-v0.1 §4/§6.1；T4b 任务）。

覆盖（R10 守门层：每规则一正一反，防校验器改坏）：
- L1 id 全局唯一、snake_case（工序/链/provider 各自 + 跨表；事件/Action 各自表内）
- L2 domain 在枚举内；工序 context 引用的 provider 必须同域
- L3 Model 名必须在 models 包可 import
- L4 model 别名必须注册在 models.yaml（模型串内联拒载；models.yaml 缺失即拒载）
- L5 链内 worker 必须已注册；链域与工序域一致
- L6 链步骤 input 表达式静态可解析、只指向前序、字段存在
- L7 risk: transaction 拒载（工序与 Action 双面）
- L8 prompt 文件、config_dir 目录必须存在
- L9 Model 结构 hash 与工序 version 联动（write_hashes 往返 / 改 Model 不 bump 拒载）
- L10（补充规则，详设 §6.3）Action target 锁死 tm.proposal
- 公共 API：load_registry / validate / write_hashes / RegistryLoadError 聚合

全部用 tmp_path 迷你仓库树（仿 tests/test_lint_p2.py 风格），不污染真仓库；
Model 类经 sys.modules 注入临时 ``models.workers`` 模块（本任务不依赖真仓库
models/workers，T10 才落模型）。注入用 sys.modules 而非 sys.path 影子包：
sys.path 前置 tmp 包会连真 ``models.contract`` 一起被影子化，而 loader 要用
真契约类解析 actions yaml（技术决策，见任务报告）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
不出现完整 URL scheme / IPv4 / sk- 前缀字面量；不给敏感名（api_key/password/
token）赋非空字面量；不直接读 os.environ（只用 monkeypatch.setenv）。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from engine.registry import (
    Registry,
    RegistryLoadError,
    load_registry,
    validate,
    write_hashes,
)
from engine.registry.loader import model_structure_hash

# ---- 测试 Model 类（注入 models.workers 的临时模块，见模块 docstring）----


class EchoInput(BaseModel):
    text: str


class EchoResult(BaseModel):
    text: str
    translated: str | None = None


class InboxInput(BaseModel):
    customer_id: str
    conversation: str


class SnapshotInput(BaseModel):
    conversation: str
    customer_id: str


class SnapshotResult(BaseModel):
    snapshot: str
    customer_id: str


class TodoInput(BaseModel):
    snapshot: str


class TodoResult(BaseModel):
    done: bool


class GreetingParams(BaseModel):
    name: str


class GreetingResult(BaseModel):
    greeting: str


DEFAULT_MODELS: dict[str, type[BaseModel]] = {
    "EchoInput": EchoInput,
    "EchoResult": EchoResult,
    "InboxInput": InboxInput,
    "SnapshotInput": SnapshotInput,
    "SnapshotResult": SnapshotResult,
    "TodoInput": TodoInput,
    "TodoResult": TodoResult,
    "GreetingParams": GreetingParams,
    "GreetingResult": GreetingResult,
}


def _inject(monkeypatch: pytest.MonkeyPatch, classes: dict[str, type]) -> None:
    """把测试 Model 类装进新模块并挂到 sys.modules['models.workers']。"""
    mod = types.ModuleType("models.workers")
    for name, cls in classes.items():
        setattr(mod, name, cls)
    monkeypatch.setitem(sys.modules, "models.workers", mod)


def _inject_default_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    _inject(monkeypatch, dict(DEFAULT_MODELS))


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """models.yaml 的 env: 引用所需环境变量（R20：值只进测试环境变量）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


# ---- 迷你仓库构造（仿 test_lint_p2 风格）----


def _dump(data: object) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _mini_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for sub in (
        "engine/registry/workers",
        "engine/registry/chains",
        "engine/registry/context",
        "engine/registry/events",
        "engine/registry/actions",
    ):
        (root / sub).mkdir(parents=True)
    return root


def _write(repo: Path, rel: str, content: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _models_yaml(aliases: tuple[str, ...] = ("default",)) -> str:
    models: dict[str, object] = {}
    for alias in aliases:
        models[alias] = {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url": "env:DEEPSEEK_BASE_URL",
            "api_key": "env:DEEPSEEK_API_KEY",
        }
    return _dump({"models": models})


def _worker_yaml(
    worker_id: str = "echo",
    domain: str = "demo",
    version: int = 1,
    risk: str = "read",
    input_model: str = "EchoInput",
    output_model: str = "EchoResult",
    model: str = "default",
    prompt: str = "prompt.md",
    config_dir: str | None = None,
    context: list[dict] | None = None,
) -> str:
    data: dict[str, object] = {
        "id": worker_id,
        "domain": domain,
        "version": version,
        "risk": risk,
        "description": "测试工序",
        "input": {"model": input_model},
        "output": {"model": output_model},
        "model": model,
        "retry": {"max_attempts": 2, "timeout_s": 30},
        "prompt": prompt,
    }
    if config_dir is not None:
        data["config_dir"] = config_dir
    if context is not None:
        data["context"] = context
    return _dump(data)


def _add_worker(
    repo: Path,
    worker_id: str = "echo",
    domain: str = "demo",
    subdir: str | None = None,
    **kw: object,
) -> Path:
    sub = subdir or worker_id
    rel = f"engine/registry/workers/{domain}/{sub}/worker.yaml"
    _write(repo, rel, _worker_yaml(worker_id=worker_id, domain=domain, **kw))
    _write(repo, f"engine/registry/workers/{domain}/{sub}/prompt.md", "模板")
    return repo / "engine" / "registry" / "workers" / domain / sub


def _chain_yaml(
    chain_id: str = "echo_chain",
    domain: str = "demo",
    input_model: str = "InboxInput",
    steps: list[dict] | None = None,
) -> str:
    data: dict[str, object] = {
        "id": chain_id,
        "domain": domain,
        "description": "测试链",
        "input": {"model": input_model},
        "steps": steps or [{"worker": "echo", "input": "task.input"}],
    }
    return _dump(data)


def _provider_yaml(
    provider_id: str = "greeting",
    domain: str = "demo",
    params: str = "GreetingParams",
    returns: str = "GreetingResult",
) -> str:
    return _dump(
        [
            {
                "id": provider_id,
                "domain": domain,
                "description": "测试 provider",
                "params": {"model": params},
                "returns": {"model": returns},
                "provider": "demo.greeting",
            }
        ]
    )


def _event_yaml(
    event_type: str = "demo.message_received",
    domain: str = "demo",
    payload_model: str = "EchoInput",
    dedup_window_min: int = 10,
    trigger_chain: str | None = None,
) -> str:
    item: dict[str, object] = {
        "event_type": event_type,
        "domain": domain,
        "payload_model": payload_model,
        "dedup_window_min": dedup_window_min,
    }
    if trigger_chain is not None:
        item["trigger_chain"] = trigger_chain
    return _dump([item])


def _action_yaml(
    action_id: str = "demo.suggest_followup",
    domain: str = "demo",
    risk: str = "suggest",
    output_model: str = "EchoResult",
    target: str = "tm.proposal",
) -> str:
    return _dump(
        [
            {
                "action_id": action_id,
                "domain": domain,
                "risk": risk,
                "output_model": output_model,
                "target": target,
            }
        ]
    )


def _valid_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个全合法的迷你仓库：echo 工序 + 链 + provider + 事件 + Action + models.yaml。"""
    repo = _mini_repo(tmp_path)
    _inject_default_workers(monkeypatch)
    _set_env(monkeypatch)
    _add_worker(repo, "echo", "demo", context=[{"id": "greeting", "params": {}}])
    _write(
        repo,
        "engine/registry/chains/demo/echo_chain/chain.yaml",
        _chain_yaml(),
    )
    _write(repo, "engine/registry/context/demo.yaml", _provider_yaml())
    _write(
        repo,
        "engine/registry/events/demo.yaml",
        _event_yaml(trigger_chain="echo_chain"),
    )
    _write(repo, "engine/registry/actions/demo.yaml", _action_yaml())
    _write(repo, "models.yaml", _models_yaml())
    write_hashes(repo)
    return repo


# ==== 公共 API ====


def test_load_registry_full_valid_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    reg = load_registry(repo)
    assert isinstance(reg, Registry)
    assert reg.valid
    assert list(reg.workers) == ["echo"]
    assert list(reg.chains) == ["echo_chain"]
    assert list(reg.context_providers) == ["greeting"]
    assert list(reg.events) == ["demo.message_received"]
    assert list(reg.actions) == ["demo.suggest_followup"]
    assert reg.workers["echo"].risk == "read"
    assert reg.workers["echo"].domain.value == "demo"
    assert reg.workers["echo"].context[0].id == "greeting"
    assert reg.actions["demo.suggest_followup"].target == "tm.proposal"


def test_validate_clean_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    assert validate(repo) == []


def test_validate_broken_returns_lines_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "echo", "demo", subdir="dup")
    lines = validate(repo)
    assert lines
    assert any(line.startswith("[L1]") for line in lines)


def test_load_registry_raises_aggregated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """校验失败抛 RegistryLoadError，聚合全部违规（每条一行 [L#] ...）。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "echo", "demo", subdir="dup")  # L1 重复
    _add_worker(repo, "inline", "demo", subdir="im", model="deepseek-chat")  # L4 内联
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert len(lines) >= 2
    assert any(line.startswith("[L1]") for line in lines)
    assert any(line.startswith("[L4]") for line in lines)


def test_empty_registry_with_models_yaml_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空全册（无任何声明）+ models.yaml 在位 = 合法空注册表。"""
    repo = _mini_repo(tmp_path)
    _set_env(monkeypatch)
    _write(repo, "models.yaml", _models_yaml())
    reg = load_registry(repo)
    assert reg.workers == {}
    assert reg.chains == {}
    assert reg.context_providers == {}
    assert reg.events == {}
    assert reg.actions == {}
    assert reg.valid


def test_validate_missing_models_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空全册 + models.yaml 缺失 = 拒载（缺失即拒载，任务语义）。"""
    repo = _mini_repo(tmp_path)
    _inject_default_workers(monkeypatch)
    lines = validate(repo)
    assert any(line.startswith("[L4]") and "缺失" in line for line in lines)
    with pytest.raises(RegistryLoadError):
        load_registry(repo)


def test_load_registry_custom_models_yaml_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    (repo / "models.yaml").unlink()
    alt = tmp_path / "models_alt.yaml"
    alt.write_text(_models_yaml(), encoding="utf-8")
    reg = load_registry(repo, models_yaml_path=alt)
    assert reg.valid


# ==== L1 id 全局唯一、snake_case ====


def test_l1_distinct_snake_ids_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "second_worker", "demo", subdir="sw")
    reg = load_registry(repo)
    assert set(reg.workers) == {"echo", "second_worker"}


def test_l1_duplicate_worker_id_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "echo", "demo", subdir="dup")
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L1]") and "重复" in line for line in lines)


def test_l1_cross_table_duplicate_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工序 id 与链 id 相同 = 跨表重复拒载。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(repo, "engine/registry/chains/demo/echo/chain.yaml", _chain_yaml(chain_id="echo"))
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L1]") and "跨表" in line for line in lines)


def test_l1_duplicate_provider_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(repo, "engine/registry/context/demo2.yaml", _provider_yaml(provider_id="greeting"))
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L1]") and "重复" in line for line in lines)


def test_l1_duplicate_event_type_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(
        repo,
        "engine/registry/events/demo2.yaml",
        _event_yaml(event_type="demo.message_received"),
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L1]") and "重复" in line for line in lines)


def test_l1_non_snake_id_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "bad-id", "demo", subdir="bad")
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L1]") and "snake_case" in line for line in lines)


# ==== L2 domain 枚举内 + context provider 同域 ====


def test_l2_same_domain_context_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正向（_valid_repo 的 echo 已引用同域 provider greeting）。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    reg = load_registry(repo)
    assert reg.workers["echo"].context[0].id == "greeting"


def test_l2_cross_domain_context_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """crm 工序引用 demo provider = 跨域拒载（L2 反向）。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(
        repo,
        "crm_worker",
        "crm",
        subdir="crmw",
        context=[{"id": "greeting", "params": {}}],
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L2]") and "跨域" in line for line in lines)


def test_l2_unregistered_provider_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(
        repo,
        "ghost_ref",
        "demo",
        subdir="ghost",
        context=[{"id": "ghost_provider", "params": {}}],
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L2]") and "未登记" in line for line in lines)


def test_l2_domain_not_in_enum_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "bad_domain", "finance", subdir="bd")
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L2]") for line in lines)


# ==== L3 Model 可 import ====


def test_l3_missing_model_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(
        repo,
        "ghost_model",
        "demo",
        subdir="gm",
        input_model="NoSuchModel",
        output_model="EchoResult",
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L3]") and "NoSuchModel" in line for line in lines)


def test_l3_model_resolvable_from_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """链 input model 用契约类（models.contract 包内同名类，L3 回退搜索）。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(
        repo,
        "engine/registry/chains/demo/alt/chain.yaml",
        _chain_yaml(
            chain_id="alt_chain",
            input_model="ActionDeclaration",
            steps=[{"worker": "echo", "input": "task.input"}],
        ),
    )
    reg = load_registry(repo)
    assert "alt_chain" in reg.chains


def test_l3_not_a_pydantic_model_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NotAModel:  # 非 Pydantic Model（普通类）
        pass

    repo = _valid_repo(tmp_path, monkeypatch)
    _inject(monkeypatch, {**DEFAULT_MODELS, "NotAModel": NotAModel})
    _add_worker(
        repo,
        "bad_model_type",
        "demo",
        subdir="bmt",
        input_model="NotAModel",
        output_model="EchoResult",
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L3]") and "Pydantic" in line for line in lines)


# ==== L4 model 别名必须注册（模型串内联拒载）====


def test_l4_inline_model_string_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向（A2）：model: deepseek-chat 内联模型串拒载。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "inline_model", "demo", subdir="im", model="deepseek-chat")
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L4]") and "未注册" in line for line in lines)


def test_l4_unknown_alias_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "alias_worker", "demo", subdir="aw", model="fast")
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L4]") for line in lines)


def test_l4_missing_models_yaml_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """models.yaml 缺失 = 拒载「未配置模型注册」（任务语义：缺失即拒载）。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    (repo / "models.yaml").unlink()
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L4]") and "缺失" in line for line in lines)


def test_l4_bad_models_yaml_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(repo, "models.yaml", "models:\n  default: [unclosed\n")
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L4]") for line in lines)


# ==== L5 链内 worker 已注册 + 链域一致 ====


def test_l5_unregistered_worker_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(
        repo,
        "engine/registry/chains/demo/ghost/chain.yaml",
        _chain_yaml(chain_id="ghost_chain", steps=[{"worker": "ghost", "input": "task.input"}]),
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L5]") and "未登记" in line for line in lines)


def test_l5_domain_mismatch_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "crm_worker", "crm", subdir="crmw")
    _write(
        repo,
        "engine/registry/chains/demo/mixed/chain.yaml",
        _chain_yaml(
            chain_id="mixed_chain",
            domain="demo",
            steps=[{"worker": "crm_worker", "input": "task.input"}],
        ),
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L5]") and "域" in line for line in lines)


# ==== L6 链步骤 input 表达式 ====


def _l6_chain(steps: list[dict]) -> str:
    return _chain_yaml(chain_id="l6_chain", input_model="InboxInput", steps=steps)


def _add_l6_workers(repo: Path) -> None:
    _add_worker(
        repo, "snapshot", "demo", subdir="snap",
        input_model="SnapshotInput", output_model="SnapshotResult",
    )
    _add_worker(
        repo, "todo", "demo", subdir="todo",
        input_model="TodoInput", output_model="TodoResult",
    )
    write_hashes(repo)  # 新 Model 的 hash 记录随新工序登记


def test_l6_valid_references_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """task.input 直传 + steps[n].output(.字段) + task.input.字段 正向。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_l6_workers(repo)
    _write(
        repo,
        "engine/registry/chains/demo/l6/chain.yaml",
        _l6_chain(
            steps=[
                {"worker": "echo", "input": "task.input"},
                {
                    "worker": "snapshot",
                    "input": {
                        "conversation": "steps[0].output.text",
                        "customer_id": "task.input.customer_id",
                    },
                },
            ]
        ),
    )
    reg = load_registry(repo)
    assert "l6_chain" in reg.chains


def test_l6_forward_reference_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """反向：step 1 引用 steps[2] = 引用后序步拒载。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_l6_workers(repo)
    _write(
        repo,
        "engine/registry/chains/demo/l6/chain.yaml",
        _l6_chain(
            steps=[
                {"worker": "echo", "input": "task.input"},
                {"worker": "snapshot", "input": {"conversation": "steps[2].output.snapshot"}},
                {"worker": "todo", "input": "task.input"},
            ]
        ),
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L6]") and "非前序" in line for line in lines)


def test_l6_self_reference_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """反向：step 0 引用 steps[0]（自身）= 非前序拒载。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(
        repo,
        "engine/registry/chains/demo/l6/chain.yaml",
        _l6_chain(steps=[{"worker": "echo", "input": {"text": "steps[0].output.text"}}]),
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L6]") and "非前序" in line for line in lines)


def test_l6_missing_source_field_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：steps[0].output 引用不存在的字段。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_l6_workers(repo)
    _write(
        repo,
        "engine/registry/chains/demo/l6/chain.yaml",
        _l6_chain(
            steps=[
                {"worker": "echo", "input": "task.input"},
                {"worker": "snapshot", "input": {"conversation": "steps[0].output.no_such"}},
            ]
        ),
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L6]") and "不存在" in line for line in lines)


def test_l6_missing_task_input_field_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：task.input 引用链 input Model 不存在的字段。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(
        repo,
        "engine/registry/chains/demo/l6/chain.yaml",
        _l6_chain(steps=[{"worker": "echo", "input": {"text": "task.input.no_such"}}]),
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L6]") and "不存在" in line for line in lines)


def test_l6_dict_key_not_in_target_model_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：dict 键不是目标工序 input Model 的字段。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(
        repo,
        "engine/registry/chains/demo/l6/chain.yaml",
        _l6_chain(steps=[{"worker": "echo", "input": {"no_such_key": "task.input"}}]),
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L6]") and "不存在" in line for line in lines)


def test_l6_unparseable_expression_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：表达式形态不合法（steps[n].input 不存在）。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(
        repo,
        "engine/registry/chains/demo/l6/chain.yaml",
        _l6_chain(steps=[{"worker": "echo", "input": "steps[0].input.foo"}]),
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L6]") and "不可静态解析" in line for line in lines)


# ==== L7 risk: transaction 拒载 ====


def test_l7_transaction_worker_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "txn", "demo", subdir="txn", risk="transaction")
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L7]") and "transaction" in line for line in lines)


def test_l7_allowed_risks_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    for risk in ("read", "suggest", "write"):
        _add_worker(repo, f"risk_{risk}", "demo", subdir=f"r{risk}", risk=risk)
    reg = load_registry(repo)
    got = {reg.workers[w].risk for w in reg.workers if w.startswith("risk_")}
    assert got == {"read", "suggest", "write"}


def test_l7_action_transaction_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ActionDeclaration.risk 同样 Literal，transaction 不可登记（§6.3）。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(repo, "engine/registry/actions/demo.yaml", _action_yaml(risk="transaction"))
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L7]") for line in lines)


# ==== L8 prompt / config_dir 存在 ====


def test_l8_missing_prompt_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "no_prompt", "demo", subdir="np")
    (repo / "engine/registry/workers/demo/np/prompt.md").unlink()
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L8]") and "prompt" in line for line in lines)


def test_l8_missing_config_dir_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "no_cfg", "demo", subdir="nc", config_dir="config/")
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L8]") and "config_dir" in line for line in lines)


def test_l8_prompt_escaping_repo_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向加固：prompt 解析后越出仓库目录（文件存在也拒）。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "escape", "demo", subdir="esc", prompt="../../../../../../outside.md")
    (tmp_path / "outside.md").write_text("x", encoding="utf-8")
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L8]") and "越出" in line for line in lines)


def test_l8_present_prompt_and_config_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(repo, "with_cfg", "demo", subdir="wc", config_dir="config/")
    (repo / "engine/registry/workers/demo/wc/config").mkdir(parents=True)
    reg = load_registry(repo)
    assert "with_cfg" in reg.workers


# ==== L9 Model 结构 hash 与 version 联动 ====


def test_write_hashes_writes_stable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    p = repo / "engine/registry/model_hashes.yaml"
    assert p.is_file()
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert set(data["models"]) == {"EchoInput", "EchoResult"}
    rec = data["models"]["EchoInput"]
    assert rec["version"] == 1
    assert isinstance(rec["hash"], str) and len(rec["hash"]) == 64
    # 幂等：重跑 write_hashes 内容不变
    assert p.read_text(encoding="utf-8") == write_hashes(repo).read_text(encoding="utf-8")


def test_l9_roundtrip_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """write_hashes 后 load：hash 匹配 + version 一致 = 通过（正向）。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    reg = load_registry(repo)
    assert reg.valid


def test_l9_missing_hash_record_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：model_hashes.yaml 缺失 = 拒载提示先跑 write-hashes。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    (repo / "engine/registry/model_hashes.yaml").unlink()
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L9]") and "write-hashes" in line for line in lines)


def test_l9_model_changed_without_bump_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向（详设 L9 原文）：改 Model 字段不 bump 工序 version = 拒载。"""
    repo = _valid_repo(tmp_path, monkeypatch)  # 先按旧结构登记 hash

    class EchoResultV2(BaseModel):
        text: str
        translated: str | None = None
        extra: str  # 新增字段 = 结构变更

    _inject(monkeypatch, {**DEFAULT_MODELS, "EchoResult": EchoResultV2})
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L9]") and "已变更" in line for line in lines)


def test_l9_version_mismatch_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """反向：bump version 但未重跑 write-hashes = version 与记录不一致拒载。"""
    repo = _valid_repo(tmp_path, monkeypatch)
    _add_worker(
        repo,
        "echo_v2",
        "demo",
        subdir="v2",
        version=2,
        input_model="EchoInput",
        output_model="EchoResult",
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L9]") and "不一致" in line for line in lines)


# ==== L10（§6.3）Action target 锁死 tm.proposal ====


def test_l10_action_target_locked_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(repo, "engine/registry/actions/demo.yaml", _action_yaml(target="other.target"))
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L10]") for line in lines)


def test_l10_action_target_tm_proposal_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    reg = load_registry(repo)
    assert reg.actions["demo.suggest_followup"].target == "tm.proposal"


# ==== 事件 trigger_chain 引用一致性（补充，R22 断裂=拒载）====


def test_event_dangling_trigger_chain_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    _write(repo, "engine/registry/events/demo.yaml", _event_yaml(trigger_chain="ghost_chain"))
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("[L0]") and "trigger_chain" in line for line in lines)


def test_event_trigger_chain_registered_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _valid_repo(tmp_path, monkeypatch)
    reg = load_registry(repo)
    assert reg.events["demo.message_received"].trigger_chain == "echo_chain"


def test_l9_equivalent_annotations_same_hash() -> None:
    """review 回归（2026-08-28）：等价类型写法（Optional / str|None / Union /
    None|str / Literal 顺序）必须产出同一结构 hash——纯语法重构不得触发
    「结构已变更」误报（原实现三写法两 hash）。"""
    from typing import Literal, Optional, Union

    class A(BaseModel):
        x: str | None = None
        y: Literal["a", "b"] = "a"

    class B(BaseModel):
        x: Optional[str] = None
        y: Literal["b", "a"] = "a"

    class C(BaseModel):
        x: Union[str, None] = None
        y: Literal["a", "b"] = "a"

    class D(BaseModel):
        x: None | str = None
        y: Literal["a", "b"] = "a"

    hashes = {model_structure_hash(m) for m in (A, B, C, D)}
    assert len(hashes) == 1, f"等价注解 hash 不一致：{hashes}"
