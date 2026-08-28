"""T12a 验收断言（详设-v0.1 §12 A1-A10；@version_acceptance 标记，开发流程⑤）。

除 A11（真模型冒烟，发版前 + 用户 key 单独跑）外 A1-A10 全部落地：
- A1  runner + FakeAgent 全相位到 DONE + 相位流水非空
- A2  registry 缺字段/重复 id/模型串内联 -> load_registry 拒载（RegistryLoadError）
- A3  audit 可查：每次调用 input_full/output_full/tokens/耗时非空（get_audit）
- A4  崩溃恢复不重烧（resume 后审计条数不变）
- A5  本套件跑完零网络（断言用 FakeAgent 桩，无真 PydanticAI Agent）
- A6  lint 三类样本（业务词字面量/内联 URL/裸 dict 返回）各被拦
- A7  换模型串（models.yaml deepseek-chat -> deepseek-reasoner）零代码改动，
      审计 model 字段是新串
- A8  risk: transaction 工序声明被 loader 拒载
- A9  五桩（标准/捕获/失效/坏输出/空输出）各走通预期路径；
      生产目录 Fake 前缀类被 P3-4 拦
- A10 桩 provider 经 providers 注册注入生效（Context 通道）；worker A
      import worker B 被 P3-2 拦（低耦合 R21/R24）

复用 test_runner 的迷你仓库 / FakeAgent / DB fixture 基建（from test_runner import ...，
pytest 对测试模块命名空间内的 fixture 注册同样生效——fixture 随模块收集）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
A6 样本的 URL 形态运行期拼接；不给敏感名赋非空字面量；不读 os.environ。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pytest_asyncio import fixture as async_fixture

from engine.core.db import (
    create_engine,
    dispose_engine,
    get_audit,
    get_step,
    get_task,
)
from engine.core.llm import load_models
from engine.lint.p1 import P1BusinessTermsRule
from engine.lint.p2 import P2CredentialsRule
from engine.lint.p3 import (
    P3Rule1WorkerSignature,
    P3Rule2CouplingImports,
    P3Rule4StubLeak,
)
from engine.registry import RegistryLoadError, load_registry
from test_runner import (
    BadOutputAgent,
    CapturingAgent,
    EchoResult,
    FakeAgent,
    GreetingParams,
    GreetingResult,
    UnavailableAgent,
    _build_repo,
    _clean_engine_tables,
    _write,
    _worker_yaml,
    db_engine,
    make_agent_factory,
    make_runner_env,
)


class FakeGreetingProvider:
    """A10 桩 provider（住 tests/，R12；生产目录出现即 P3-4 拦）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.params_seen: list[GreetingParams] = []

    async def provide(self, params: GreetingParams) -> GreetingResult:
        self.calls += 1
        self.params_seen.append(params)
        return GreetingResult(greeting="hello " + params.name)


# ==== A1：全相位走完到 DONE，输出相位流水 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a1_full_chain_done_with_phase_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="ok"))
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)
    result = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hello"}
    )
    assert result.status == "done"
    assert result.error is None
    assert result.phase_lines  # 相位流水非空
    assert [line.phase for line in result.phase_lines] == [
        "INIT", "REASON", "ACT", "OBSERVE", "VERIFY", "DONE",
    ]
    assert agents[0].calls == 1
    task = await get_task(db_engine, result.task_id)
    assert task is not None and task.status == "done"
    row = await get_step(db_engine, task.current_step_row)
    assert row.output is not None


# ==== A2：注册表缺字段/重复 id/模型串内联 -> 拒载 ====


@pytest.mark.version_acceptance
def test_a2_registry_rejects_broken_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (a) 缺必填字段 risk（§4.1 必填）
    repo_a = _build_repo(tmp_path / "a", monkeypatch)
    body = _worker_yaml()
    body = "\n".join(
        line for line in body.splitlines() if not line.startswith("risk:")
    ) + "\n"
    _write(
        repo_a,
        "engine/registry/workers/demo/echo/worker.yaml",
        body,
    )
    with pytest.raises(RegistryLoadError):
        load_registry(repo_a)

    # (b) 重复 id（L1 全局唯一）
    repo_b = _build_repo(tmp_path / "b", monkeypatch)
    from test_runner import _write_echo_worker

    _write_echo_worker(repo_b, subdir="echo2", worker_id="echo")
    with pytest.raises(RegistryLoadError) as excinfo_b:
        load_registry(repo_b)
    assert any("[L1]" in line for line in str(excinfo_b.value).splitlines())

    # (c) 模型串内联（L4：只允许 models.yaml 已注册别名）
    repo_c = _build_repo(tmp_path / "c", monkeypatch)
    _write(
        repo_c,
        "engine/registry/workers/demo/echo/worker.yaml",
        _worker_yaml(model_alias="deepseek-chat"),
    )
    with pytest.raises(RegistryLoadError) as excinfo_c:
        load_registry(repo_c)
    assert any("[L4]" in line for line in str(excinfo_c.value).splitlines())


# ==== A3：审计可查（每次调用输入/输出/token/耗时非空）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a3_audit_queryable_full_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    factory, _ = make_agent_factory(FakeAgent, output=lambda ot: ot(text="ok"))
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)
    result = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hello"}
    )
    assert result.status == "done"
    rows = await get_audit(db_engine, result.task_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.result == "ok"
    assert row.input_full  # prompt 全文非空（受控，§8）
    assert row.output_full  # 模型输出全文非空
    assert row.input_tokens is not None  # token/耗时为真值（§7.3）
    assert row.output_tokens is not None
    assert row.duration_ms is not None


# ==== A4：崩溃恢复不重烧（审计条数不变）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a4_crash_recovery_audit_count_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    import importlib

    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="echoed"))
    _, _, _, build = make_runner_env(
        tmp_path, monkeypatch, run_py=(
            "from engine.core.context import EngineContext\n"
            "from models.workers import EchoInput, EchoResult\n"
            "\n"
            "CRASH = False\n"
            "\n"
            "\n"
            "def run(inputs: EchoInput, ctx: EngineContext) -> EchoResult:\n"
            "    if CRASH:\n"
            "        raise KeyboardInterrupt('kill -9 模拟')\n"
            "    return EchoResult(text=inputs.text)\n"
        )
    )
    run_mod = importlib.import_module("engine.registry.workers.demo.echo.run")
    run_mod.CRASH = True
    with pytest.raises(KeyboardInterrupt):
        await build(db_engine, agent_factory_fn=factory).run(
            "echo_chain", {"text": "hello"}
        )

    from test_runner import _latest_task

    task = await _latest_task(db_engine)
    assert len(await get_audit(db_engine, task.id)) == 1  # REASON 1 次已审计
    assert agents[0].calls == 1

    run_mod.CRASH = False
    result = await build(db_engine, agent_factory_fn=factory).resume(task.id)
    assert result.status == "done"
    assert agents[0].calls == 1  # 不重烧 token
    assert len(await get_audit(db_engine, task.id)) == 1  # 审计条数不变（A4）


# ==== A5：本套件跑完零网络（断言用 FakeAgent，无真 API）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a5_suite_zero_network_all_stubs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="ok"))
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)
    result = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hello"}
    )
    assert result.status == "done"
    assert agents  # 确实创建了桩
    for agent in agents:
        # 无真 PydanticAI Agent（真 Agent 模块为 pydantic_ai.*）=> 零网络
        assert not type(agent).__module__.startswith("pydantic_ai")


# ==== A6：lint 三类违规样本各被拦 ====


@pytest.mark.version_acceptance
def test_a6_lint_blocks_three_sample_kinds(tmp_path: Path) -> None:
    # (a) 业务词字面量（P1）：config/ 词表词出现在 run.py（R10 机器层）
    repo_a = tmp_path / "a"
    _write(
        repo_a,
        "engine/registry/workers/demo/w1/config/spec.yaml",
        yaml.safe_dump({"shop": "甲铺"}, sort_keys=False, allow_unicode=True),
    )
    _write(repo_a, "engine/registry/workers/demo/w1/worker.yaml", "id: w1\n")
    _write(repo_a, "engine/registry/workers/demo/w1/run.py", 'name = "甲铺"\n')
    vios_a = P1BusinessTermsRule().check(repo_a)
    assert any(v.rule_id == "P1" and "甲铺" in v.message for v in vios_a)

    # (b) 内联 URL（P2 凭据端点零容忍）
    repo_b = tmp_path / "b"
    _write(repo_b, "engine/leak.py", 'url = "ht' + 'tps://example.invalid/v1"\n')
    vios_b = P2CredentialsRule().check(repo_b)
    assert any(v.rule_id == "P2" and "URL" in v.message for v in vios_b)

    # (c) 裸 dict 返回（P3-1 工序签名：run 必须返回 RegisteredModel）
    repo_c = tmp_path / "c"
    _write(
        repo_c,
        "engine/registry/workers/demo/w2/run.py",
        "from engine.core.context import EngineContext\n"
        "def run(inputs, ctx) -> dict:\n"
        "    return {}\n",
    )
    vios_c = P3Rule1WorkerSignature().check(repo_c)
    assert any(v.rule_id == "P3-1" and "dict" in v.message for v in vios_c)


# ==== A7：换模型串零代码改动，审计 model 字段是新串 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a7_model_string_switch_zero_code_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    from test_runner import _models_yaml

    factory, _ = make_agent_factory(FakeAgent, output=lambda ot: ot(text="ok"))
    repo, _, _, build = make_runner_env(tmp_path, monkeypatch)

    r1 = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hello"}
    )
    assert r1.status == "done"
    audit1 = await get_audit(db_engine, r1.task_id)
    assert audit1[0].model == "deepseek-chat"

    # 只改 models.yaml 的 model 串（零代码改动，R11）
    (repo / "models.yaml").write_text(
        _models_yaml(model_str="deepseek-reasoner"), encoding="utf-8"
    )
    mr2 = load_models(repo / "models.yaml")
    r2 = await build(
        db_engine, agent_factory_fn=factory, model_registry=mr2
    ).run("echo_chain", {"text": "hello"})
    assert r2.status == "done"
    audit2 = await get_audit(db_engine, r2.task_id)
    assert audit2[0].model == "deepseek-reasoner"  # 审计 model 字段是新串


# ==== A8：risk: transaction 工序声明被拒载 ====


@pytest.mark.version_acceptance
def test_a8_transaction_risk_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _build_repo(tmp_path, monkeypatch)
    _write(
        repo,
        "engine/registry/workers/demo/echo/worker.yaml",
        _worker_yaml(risk="transaction"),
    )
    with pytest.raises(RegistryLoadError) as excinfo:
        load_registry(repo)
    assert any(
        "[L7]" in line and "transaction" in line
        for line in str(excinfo.value).splitlines()
    )


# ==== A9：五桩各走通预期路径 + 生产目录桩类被 P3-4 拦 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a9_five_stubs_and_production_stub_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)

    # 标准桩：正常全链路到 DONE
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="ok"))
    r = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hello"}
    )
    assert r.status == "done"

    # 捕获桩：prompt 组装可查
    factory, agents = make_agent_factory(CapturingAgent, output=lambda ot: ot(text="ok"))
    r = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hello"}
    )
    assert r.status == "done"
    assert agents[0].prompts and "hello" in agents[0].prompts[0]

    # 失效桩：重试耗尽 -> FAILED（error 显式）
    factory, agents = make_agent_factory(UnavailableAgent)
    r = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hello"}
    )
    assert r.status == "failed"
    assert agents[0].calls == 3

    # 坏输出桩：re-ask 耗尽 -> FAILED
    factory, agents = make_agent_factory(BadOutputAgent)
    r = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hello"}
    )
    assert r.status == "failed"
    assert agents[0].rounds == 3

    # 空输出桩：VERIFY 拦截 -> FAILED（不产假成功）
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text=""))
    r = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": ""}
    )
    assert r.status == "failed"
    task = await get_task(db_engine, r.task_id)
    assert "VERIFY" in (task.error or "") and "输出非空" in (task.error or "")

    # 生产目录 Fake 前缀类被 P3-4 拦（桩只许住 tests/，R12）
    prod = tmp_path / "prod"
    _write(prod, "engine/leak.py", "class FakeAgent:\n    pass\n")
    vios = P3Rule4StubLeak().check(prod)
    assert any(v.rule_id == "P3-4" and "FakeAgent" in v.message for v in vios)


# ==== A10：桩 provider 注入生效 + worker import worker 被 P3-2 拦 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a10_provider_injection_and_worker_import_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    import importlib

    provider = FakeGreetingProvider()
    prompt_body = "你是 echo 工序。\n输入：{input.text}\n问候语：{context.demo.greeting}\n"
    _, _, _, build = make_runner_env(
        tmp_path,
        monkeypatch,
        with_provider=True,
        providers={"demo.greeting": provider.provide},
        worker_kw={"context": [{"id": "demo", "params": {"name": "input.text"}}]},
        prompt_body=prompt_body,
    )
    factory, agents = make_agent_factory(CapturingAgent, output=lambda ot: ot(text="ok"))
    r = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hi"}
    )
    assert r.status == "done"
    assert provider.calls == 1
    # Context 通道生效（数据三通道的 Context 通道）：问候语真实拼入 prompt
    assert "hello hi" in agents[0].prompts[0]
    # 工序侧可见 context_data（EngineContext 契约：{provider_id: BaseModel}）
    run_mod = importlib.import_module("engine.registry.workers.demo.echo.run")
    ctx = run_mod.SEEN["ctx"]
    assert ctx.context_data["demo"].greeting == "hello hi"

    # worker A import worker B 被 P3-2 拦（低耦合 R21：工序间只许走链 YAML）
    repo2 = tmp_path / "couple"
    _write(
        repo2,
        "engine/workers/demo/a/run.py",
        "from engine.workers.demo.b import run\n",
    )
    vios = P3Rule2CouplingImports().check(repo2)
    assert any(v.rule_id == "P3-2" for v in vios)
