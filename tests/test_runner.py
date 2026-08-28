"""T12a 执行链 runner 测试（详设-v0.1 §2/§4.2/§13；任务 T12a）。

覆盖（全部以详设与任务书为唯一依据）：
- A1 前奏：全相位走到 DONE，输出相位流水（INIT/REASON/ACT/OBSERVE/VERIFY/DONE）
- prompt 组装：{input.*} {config.*} 真实拼入（捕获桩断言）
- 失效桩：LLM 重试耗尽 -> FAILED（无部分输出残留）
- 坏输出桩：re-ask 耗尽 -> FAILED
- 空输出：VERIFY 断言红 -> FAILED（不产假成功）
- A4 崩溃恢复：REASON 成功后崩溃（worker run 抛 KeyboardInterrupt），
  resume 后不再调 LLM（桩调用计数不变）且任务 DONE；审计条数 = REASON 调用次数
- PAUSED 暂停/恢复（人工暂停 = 优雅崩溃，§2.3 同一条路径）
- 恢复续跑剩余链步骤（多步链 + 前序步骤输出经 DB 读回）
- INIT Policy 失败（风险越限）-> FAILED 且不调 LLM；链入参校验不入队

基建（全部 tmp_path 迷你仓库 + 嵌入式 PG + FakeAgent 桩，零网络）：
- 迷你仓库仿 tests/test_loader.py：Model 类经 sys.modules 注入临时
  models.workers；worker run 模块经 engine.registry.__path__ 追加 tmp 的
  registry 目录挂载（不能 sys.path 注入 tmp 根——会把真 engine/models 包
  一起影子化；run.py 的 import 需解析，见 _mount_worker_modules）
- DB：engine_pg_cluster（conftest session 级）+ 本文件 db_engine async
  fixture + autouse TRUNCATE（conftest 只提供 cluster，无每测清库，
  任务书要求显式请求——本文件自带，test_acceptance 复用）

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
样本中的 URL/IP/sk- 形态一律运行期拼接；不给敏感名（api_key/password/
token 子串）赋非空字面量；不读 os.environ（只用 monkeypatch.setenv）。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engine.core.db import (
    EngineStep,
    EngineTask,
    create_engine,
    create_task,
    dispose_engine,
    get_audit,
    get_step,
    get_task,
)
from engine.core.llm import agent_factory, load_models
from engine.core.runner import PhaseLine, TaskResult, TaskRunner
from engine.registry import load_registry, write_hashes
import engine.registry
from test_llm import BadOutputAgent, CapturingAgent, FakeAgent, UnavailableAgent

# ---- 测试 Model 类（注入临时 models.workers，仿 test_loader）----


class EchoInput(BaseModel):
    text: str


class EchoResult(BaseModel):
    text: str
    translated: str | None = None


class GreetingParams(BaseModel):
    name: str


class GreetingResult(BaseModel):
    greeting: str


DEFAULT_MODELS: dict[str, type[BaseModel]] = {
    "EchoInput": EchoInput,
    "EchoResult": EchoResult,
    "GreetingParams": GreetingParams,
    "GreetingResult": GreetingResult,
}


def _inject_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """把测试 Model 类装进新模块并挂到 sys.modules['models.workers']。"""
    mod = types.ModuleType("models.workers")
    for name, cls in DEFAULT_MODELS.items():
        setattr(mod, name, cls)
    monkeypatch.setitem(sys.modules, "models.workers", mod)


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """models.yaml 的 env: 引用所需环境变量（R20：值只进测试环境变量）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


# ---- 工序 run.py 源码模板（tmp 仓库文件，不被真仓库 lint 扫描）----

ECHO_RUN_PY = (
    "from engine.core.context import EngineContext\n"
    "from models.workers import EchoInput, EchoResult\n"
    "\n"
    "SEEN: dict = {}\n"
    "\n"
    "\n"
    "def run(inputs: EchoInput, ctx: EngineContext) -> EchoResult:\n"
    "    SEEN['ctx'] = ctx\n"
    "    SEEN['last'] = inputs.text\n"
    "    return EchoResult(text=inputs.text)\n"
)

CRASH_RUN_PY = (
    "from engine.core.context import EngineContext\n"
    "from models.workers import EchoInput, EchoResult\n"
    "\n"
    "CRASH = False\n"
    "SEEN: dict = {}\n"
    "\n"
    "\n"
    "def run(inputs: EchoInput, ctx: EngineContext) -> EchoResult:\n"
    "    SEEN['ctx'] = ctx\n"
    "    if CRASH:\n"
    "        raise KeyboardInterrupt('模拟进程崩溃（kill -9）')\n"
    "    return EchoResult(text=inputs.text)\n"
)

PAUSE_RUN_PY = (
    "import asyncio\n"
    "from engine.core.context import EngineContext\n"
    "from models.workers import EchoInput, EchoResult\n"
    "\n"
    "BLOCK_ON = None\n"
    "ENTERED = None\n"
    "BLOCK_AT = 1\n"
    "CALLS = 0\n"
    "SEEN: dict = {}\n"
    "\n"
    "\n"
    "async def run(inputs: EchoInput, ctx: EngineContext) -> EchoResult:\n"
    "    global CALLS\n"
    "    CALLS += 1\n"
    "    SEEN['ctx'] = ctx\n"
    "    if BLOCK_ON is not None and CALLS == BLOCK_AT:\n"
    "        ENTERED.set()\n"
    "        await BLOCK_ON.wait()\n"
    "    return EchoResult(text=inputs.text)\n"
)

DEFAULT_PROMPT = (
    "你是 echo 工序，把输入原样返回。\n"
    "输入文本：{input.text}\n"
    "语气要求：{config.tone}\n"
)

DEFAULT_CONFIG = yaml.safe_dump({"tone": "friendly"}, sort_keys=False, allow_unicode=True)


# ---- 迷你仓库构造（仿 test_loader 风格）----


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _mini_repo(root: Path) -> Path:
    repo = root / "repo"
    for sub in (
        "engine/registry/workers",
        "engine/registry/chains",
        "engine/registry/context",
    ):
        (repo / sub).mkdir(parents=True)
    return repo


def _models_yaml(model_str: str = "deepseek-chat") -> str:
    return yaml.safe_dump(
        {
            "models": {
                "default": {
                    "provider": "deepseek",
                    "model": model_str,
                    "base_url": "env:DEEPSEEK_BASE_URL",
                    "api_key": "env:DEEPSEEK_API_KEY",
                    "timeout_s": 30,
                    "reask_limit": 2,
                }
            }
        },
        sort_keys=False,
        allow_unicode=True,
    )


def _worker_yaml(
    *,
    worker_id: str = "echo",
    domain: str = "demo",
    version: int = 1,
    risk: str = "read",
    input_model: str = "EchoInput",
    output_model: str = "EchoResult",
    model_alias: str = "default",
    prompt: str = "prompt.md",
    config_dir: str = "config",
    context: list[dict] | None = None,
    retry: dict | None = None,
) -> str:
    data: dict[str, object] = {
        "id": worker_id,
        "domain": domain,
        "version": version,
        "risk": risk,
        "description": "测试 echo 工序",
        "input": {"model": input_model},
        "output": {"model": output_model},
        "model": model_alias,
        "retry": retry or {"max_attempts": 2, "timeout_s": 30},
        "prompt": prompt,
        "config_dir": config_dir,
    }
    if context is not None:
        data["context"] = context
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _chain_yaml(
    chain_id: str = "echo_chain",
    *,
    domain: str = "demo",
    input_model: str = "EchoInput",
    steps: list[dict] | None = None,
) -> str:
    return yaml.safe_dump(
        {
            "id": chain_id,
            "domain": domain,
            "description": "测试链",
            "input": {"model": input_model},
            "steps": steps or [{"worker": "echo", "input": "task.input"}],
        },
        sort_keys=False,
        allow_unicode=True,
    )


def _provider_yaml(provider_id: str = "demo", *, impl_id: str = "demo.greeting") -> str:
    return yaml.safe_dump(
        [
            {
                "id": provider_id,
                "domain": "demo",
                "description": "测试 provider",
                "params": {"model": "GreetingParams"},
                "returns": {"model": "GreetingResult"},
                "provider": impl_id,
            }
        ],
        sort_keys=False,
        allow_unicode=True,
    )


def _write_echo_worker(
    repo: Path,
    *,
    subdir: str = "echo",
    run_py: str | None = ECHO_RUN_PY,
    prompt_body: str = DEFAULT_PROMPT,
    **worker_kw: object,
) -> None:
    base = f"engine/registry/workers/demo/{subdir}"
    _write(repo, f"{base}/worker.yaml", _worker_yaml(**worker_kw))
    _write(repo, f"{base}/prompt.md", prompt_body)
    _write(repo, f"{base}/config/style.yaml", DEFAULT_CONFIG)
    if run_py is not None:
        _write(repo, f"{base}/run.py", run_py)
    _write(repo, f"{base}/schema.py", "'''工序 schema（Model 定义见 models.workers）'''\n")


def _mount_worker_modules(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """把 tmp 仓库的 registry/workers 挂到真 engine.registry 包下。

    不能 sys.path 注入 tmp 根（会把真 engine/models 包一起影子化）：
    engine.registry.__path__ 追加 tmp 的 registry 目录，workers/demo/echo
    子包按 namespace 解析；每次测试前清掉上次缓存。
    """
    tmp_registry = repo / "engine" / "registry"
    monkeypatch.setattr(
        engine.registry, "__path__", [*engine.registry.__path__, str(tmp_registry)]
    )
    for name in (
        "engine.registry.workers",
        "engine.registry.workers.demo",
        "engine.registry.workers.demo.echo",
        "engine.registry.workers.demo.echo.run",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)


def _build_repo(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sub: str = "repo",
    run_py: str | None = ECHO_RUN_PY,
    worker_kw: dict | None = None,
    chain_kw: dict | None = None,
    with_provider: bool = False,
    provider_impl_id: str = "demo.greeting",
    prompt_body: str = DEFAULT_PROMPT,
    model_str: str = "deepseek-chat",
) -> Path:
    """一个全合法迷你仓库：echo 工序 + 链 +（可选 provider）+ models.yaml + L9 登记。"""
    repo = _mini_repo(root)
    _inject_models(monkeypatch)
    _set_env(monkeypatch)
    _write_echo_worker(repo, run_py=run_py, prompt_body=prompt_body, **(worker_kw or {}))
    _write(
        repo,
        "engine/registry/chains/demo/echo_chain/chain.yaml",
        _chain_yaml(**(chain_kw or {})),
    )
    if with_provider:
        _write(
            repo, "engine/registry/context/demo.yaml", _provider_yaml(impl_id=provider_impl_id)
        )
    _write(repo, "models.yaml", _models_yaml(model_str=model_str))
    write_hashes(repo)
    _mount_worker_modules(monkeypatch, repo)
    return repo


# ---- DB fixture（conftest 只提供 session 级 cluster；清库本文件自带）----


@async_fixture
async def db_engine(engine_pg_cluster):
    engine = create_engine(engine_pg_cluster.url)
    yield engine
    await dispose_engine(engine)


@async_fixture(autouse=True)
async def _clean_engine_tables(db_engine):
    """每测试后 TRUNCATE 4 表（RESTART IDENTITY 重置自增、CASCADE 破外键），互不污染。"""
    yield
    async with AsyncSession(db_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE engine_audit, engine_checkpoint, engine_step, engine_task "
                "RESTART IDENTITY CASCADE"
            )
        )


# ---- runner 装配助手（R12 构造注入：agent_factory/audit_gate/providers 全桩）----


def make_agent_factory(agent_cls=FakeAgent, output=None):
    """返回 (factory, agents)：factory 注入 TaskRunner；agents 记录每次创建的桩。

    output：None = 桩默认输出；callable(output_type) -> 值 = 按输出 Model 定制
    （如 lambda ot: ot(text='echoed')）；直值 = 原样赋给桩的 output。
    """

    agents: list = []

    def factory(model_registry, alias, output_type, **kw):
        agent = agent_factory(model_registry, alias, output_type, agent_cls=agent_cls, **kw)
        if output is not None:
            value = output(output_type) if callable(output) else output
            agent.output = value
        agents.append(agent)
        return agent

    return factory, agents


def make_runner_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_py: str | None = ECHO_RUN_PY,
    worker_kw: dict | None = None,
    chain_kw: dict | None = None,
    with_provider: bool = False,
    providers: dict | None = None,
    backoff: float = 0.0,
    prompt_body: str = DEFAULT_PROMPT,
    model_str: str = "deepseek-chat",
):
    """构造迷你仓库 + 注册表 + 模型注册表，返回 (repo, registry, model_registry, build)。

    build(engine, *, agent_factory_fn, **kw) -> TaskRunner（可注入覆盖，如 A7
    换 model_registry）。
    """
    repo = _build_repo(
        tmp_path,
        monkeypatch,
        run_py=run_py,
        worker_kw=worker_kw,
        chain_kw=chain_kw,
        with_provider=with_provider,
        prompt_body=prompt_body,
        model_str=model_str,
    )
    registry = load_registry(repo)
    model_registry = load_models(repo / "models.yaml")

    def build(engine, *, agent_factory_fn, **kw) -> TaskRunner:
        kwargs = {
            "model_registry": model_registry,
            "repo_root": repo,
            "providers": providers,
            "backoff": backoff,
            "writable_check": lambda: True,
        }
        kwargs.update(kw)  # 允许测试覆盖（如 A7 换 model_registry）
        return TaskRunner(engine, registry, agent_factory=agent_factory_fn, **kwargs)

    return repo, registry, model_registry, build


# ---- 查询助手 ----


async def _latest_task(db_engine):
    async with AsyncSession(db_engine) as session:
        return (
            await session.execute(
                select(EngineTask).order_by(EngineTask.id.desc()).limit(1)
            )
        ).scalar_one_or_none()


async def _task_steps(db_engine, task_id):
    async with AsyncSession(db_engine) as session:
        rows = (
            await session.execute(
                select(EngineStep)
                .where(EngineStep.task_id == task_id)
                .order_by(EngineStep.step_index)
            )
        ).scalars()
        return list(rows)


# ==== 正常全链路 ====


@pytest.mark.asyncio
async def test_run_full_chain_done_with_phase_lines(tmp_path, monkeypatch, db_engine) -> None:
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="echoed"))
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)
    result = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hi"}
    )
    assert isinstance(result, TaskResult)
    assert result.status == "done"
    assert result.error is None
    assert result.chain_id == "echo_chain"
    # A1：全相位流水（INIT/REASON/ACT/OBSERVE/VERIFY/DONE）
    assert [line.phase for line in result.phase_lines] == [
        "INIT", "REASON", "ACT", "OBSERVE", "VERIFY", "DONE",
    ]
    assert any("policy ok" in line.message for line in result.phase_lines)
    # 任务终态落库
    task = await get_task(db_engine, result.task_id)
    assert task is not None and task.status == "done"
    assert task.finished_at is not None
    # 步骤 output 落库 + done
    row = await get_step(db_engine, task.current_step_row)
    assert row is not None
    assert row.output == {"text": "hi", "translated": None}  # ACT 工序回显入参（非 LLM 结果）
    assert row.status == "done"
    # 审计 1 条 ok；LLM 只调 1 次
    audit = await get_audit(db_engine, result.task_id)
    assert len(audit) == 1 and audit[0].result == "ok"
    assert agents[0].calls == 1


@pytest.mark.asyncio
async def test_prompt_assembles_input_and_config(tmp_path, monkeypatch, db_engine) -> None:
    """捕获桩：{input.*} 与 {config.*} 真实拼入 prompt（config/ 规格外置）。"""
    factory, agents = make_agent_factory(CapturingAgent, output=lambda ot: ot(text="echoed"))
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)
    result = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "你好"}
    )
    assert result.status == "done"
    prompt = agents[0].prompts[0]
    assert "你好" in prompt  # {input.text}
    assert "friendly" in prompt  # {config.tone}（config/style.yaml）
    assert "echo" in prompt  # prompt.md 模板原文


# ==== 失效 / 坏输出 / 空输出（§13 桩家族路径）====


@pytest.mark.asyncio
async def test_llm_unavailable_retry_then_failed_no_partial_output(
    tmp_path, monkeypatch, db_engine
) -> None:
    """失效桩：LLM 重试耗尽 -> FAILED（error 显式，无部分输出残留，无静默降级）。"""
    factory, agents = make_agent_factory(UnavailableAgent)
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)
    result = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hi"}
    )
    assert result.status == "failed"
    assert result.error and "LLM" in result.error
    assert agents[0].calls == 3  # 1 次初调 + max_attempts=2 次重试（§14）
    task = await get_task(db_engine, result.task_id)
    assert task.status == "failed"
    assert task.error is not None
    row = await get_step(db_engine, task.current_step_row)
    assert row.output is None  # 无部分输出残留
    assert row.status == "failed"
    audit = await get_audit(db_engine, result.task_id)
    assert len(audit) == 1 and audit[0].result == "failed"
    assert audit[0].output_full == {"error": audit[0].output_full["error"]}  # 带原因


@pytest.mark.asyncio
async def test_bad_output_failed_no_partial_output(tmp_path, monkeypatch, db_engine) -> None:
    """坏输出桩：PydanticAI re-ask 耗尽（LLMValidationError）-> FAILED。"""
    factory, agents = make_agent_factory(BadOutputAgent)
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)
    result = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hi"}
    )
    assert result.status == "failed"
    assert agents[0].rounds == 3  # 初调 1 + re-ask 2（reask_limit=2）
    task = await get_task(db_engine, result.task_id)
    row = await get_step(db_engine, task.current_step_row)
    assert row.output is None
    assert row.status == "failed"
    audit = await get_audit(db_engine, result.task_id)
    assert len(audit) == 1 and audit[0].result == "failed"


@pytest.mark.asyncio
async def test_empty_output_verify_intercepts_failed(tmp_path, monkeypatch, db_engine) -> None:
    """空输出桩：过类型校验但内容为空 -> VERIFY 断言红 -> FAILED（不产假成功）。"""
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text=""))
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)
    result = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": ""}
    )
    assert result.status == "failed"
    task = await get_task(db_engine, result.task_id)
    assert task.error and "VERIFY" in task.error and "输出非空" in task.error
    row = await get_step(db_engine, task.current_step_row)
    assert row.output == {"text": "", "translated": None}  # 已落库但断言红
    assert row.status == "failed"


# ==== A4 崩溃恢复（§2.3：续跑不重烧 token）====


@pytest.mark.asyncio
async def test_crash_recovery_resume_no_llm_recall(tmp_path, monkeypatch, db_engine) -> None:
    """REASON 成功后模拟崩溃（worker run 抛 KeyboardInterrupt，不写终态）；
    resume 后不再调 LLM（FakeAgent 调用计数不变）且任务 DONE；审计条数不变。"""
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="echoed"))
    _, _, _, build = make_runner_env(
        tmp_path, monkeypatch, run_py=CRASH_RUN_PY
    )
    run_mod = importlib.import_module("engine.registry.workers.demo.echo.run")
    run_mod.CRASH = True
    with pytest.raises(KeyboardInterrupt):
        await build(db_engine, agent_factory_fn=factory).run(
            "echo_chain", {"text": "hi"}
        )
    task = await _latest_task(db_engine)
    assert task.status == "running"  # 崩溃中间态（未写终态）
    assert len(await get_audit(db_engine, task.id)) == 1  # REASON 1 次已审计
    assert agents[0].calls == 1

    run_mod.CRASH = False
    result = await build(db_engine, agent_factory_fn=factory).resume(task.id)
    assert result.status == "done"
    assert agents[0].calls == 1  # A4 核心：resume 不再调 LLM（不重烧 token）
    assert len(await get_audit(db_engine, task.id)) == 1  # 审计条数不变
    done = await get_task(db_engine, task.id)
    assert done.status == "done"
    assert done.finished_at is not None


# ==== PAUSED 暂停/恢复（人工暂停 = 优雅崩溃，§2.3 同一条路径）====


@pytest.mark.asyncio
async def test_pause_then_resume_from_paused(tmp_path, monkeypatch, db_engine) -> None:
    """ACT 中请求 pause -> PAUSED 终态返回；resume 从检查点续跑 -> DONE（不重烧）。"""
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="echoed"))
    _, _, _, build = make_runner_env(
        tmp_path, monkeypatch, run_py=PAUSE_RUN_PY
    )
    run_mod = importlib.import_module("engine.registry.workers.demo.echo.run")
    gate = asyncio.Event()
    entered = asyncio.Event()
    run_mod.BLOCK_ON = gate
    run_mod.ENTERED = entered

    runner = build(db_engine, agent_factory_fn=factory)
    task = asyncio.create_task(runner.run("echo_chain", {"text": "hi"}))
    await asyncio.wait_for(entered.wait(), timeout=5)  # 工序已进入 ACT 并阻塞
    runner.request_pause()
    gate.set()
    result = await task
    assert result.status == "paused"
    assert result.error is None
    task_row = await _latest_task(db_engine)
    assert task_row.status == "paused"  # 非终态可恢复
    step_row = await get_step(db_engine, task_row.current_step_row)
    assert step_row.status == "paused"

    run_mod.BLOCK_ON = None
    result2 = await build(db_engine, agent_factory_fn=factory).resume(task_row.id)
    assert result2.status == "done"
    assert agents[0].calls == 1  # 暂停发生在 ACT 之后：REASON 不重烧
    done = await get_task(db_engine, task_row.id)
    assert done.status == "done"


@pytest.mark.asyncio
async def test_resume_continues_remaining_steps(tmp_path, monkeypatch, db_engine) -> None:
    """三步链在第 2 步暂停；resume 续跑剩余步骤（前序步骤输出经 DB 读回）-> DONE。"""
    steps = [
        {"worker": "echo", "input": "task.input"},
        {"worker": "echo", "input": "steps[0].output"},
        {"worker": "echo", "input": "steps[1].output"},
    ]
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="echoed"))
    _, _, _, build = make_runner_env(
        tmp_path,
        monkeypatch,
        run_py=PAUSE_RUN_PY,
        chain_kw={"chain_id": "echo_chain", "steps": steps},
    )
    run_mod = importlib.import_module("engine.registry.workers.demo.echo.run")
    gate = asyncio.Event()
    entered = asyncio.Event()
    run_mod.BLOCK_ON = gate
    run_mod.ENTERED = entered
    run_mod.BLOCK_AT = 2  # 第 2 步阻塞 -> 暂停

    runner = build(db_engine, agent_factory_fn=factory)
    task = asyncio.create_task(runner.run("echo_chain", {"text": "hi"}))
    await asyncio.wait_for(entered.wait(), timeout=5)
    runner.request_pause()
    gate.set()
    result = await task
    assert result.status == "paused"
    task_row = await _latest_task(db_engine)
    assert task_row.current_step == 1  # 暂停在第 2 步（index 1）

    run_mod.BLOCK_ON = None
    result2 = await build(db_engine, agent_factory_fn=factory).resume(task_row.id)
    assert result2.status == "done"
    steps_rows = await _task_steps(db_engine, task_row.id)
    assert len(steps_rows) == 3
    assert all(row.output == {"text": "hi", "translated": None} for row in steps_rows)
    assert all(row.status == "done" for row in steps_rows)


@pytest.mark.asyncio
async def test_resume_without_checkpoint_raises(tmp_path, monkeypatch, db_engine) -> None:
    """无检查点（§2.3）-> resume 报错（无从恢复）。"""
    factory, _ = make_agent_factory(FakeAgent)
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)
    task_id = await create_task(
        db_engine,
        chain_id="echo_chain",
        trigger_type="manual",
        trigger_ref=None,
        input_={"text": "hi"},
    )
    with pytest.raises(ValueError) as excinfo:
        await build(db_engine, agent_factory_fn=factory).resume(task_id)
    assert "无检查点" in str(excinfo.value)


# ==== INIT Policy 门禁 / 入参校验 ====


@pytest.mark.asyncio
async def test_policy_violation_fails_at_init_no_llm(tmp_path, monkeypatch, db_engine) -> None:
    """demo 域风险上限 read；声明 risk: write = 风险越限（§5.2）-> INIT FAILED，不调 LLM。"""
    factory, agents = make_agent_factory(FakeAgent)
    _, _, _, build = make_runner_env(
        tmp_path, monkeypatch, worker_kw={"risk": "write"}
    )
    result = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hi"}
    )
    assert result.status == "failed"
    assert result.error and "风险" in result.error
    task = await get_task(db_engine, result.task_id)
    assert task.status == "failed"
    assert agents == []  # agent 从未被创建 = 未调 LLM
    assert await get_audit(db_engine, result.task_id) == []


@pytest.mark.asyncio
async def test_run_rejects_invalid_chain_input(tmp_path, monkeypatch, db_engine) -> None:
    """链入参未过链 input Model -> ValueError 不入队（坏输入不污染队列）。"""
    factory, _ = make_agent_factory(FakeAgent)
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        await build(db_engine, agent_factory_fn=factory).run(
            "echo_chain", {"no_such_field": 1}
        )
    assert "未过" in str(excinfo.value)
    assert await _latest_task(db_engine) is None


@pytest.mark.asyncio
async def test_run_unknown_chain_raises(tmp_path, monkeypatch, db_engine) -> None:
    factory, _ = make_agent_factory(FakeAgent)
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        await build(db_engine, agent_factory_fn=factory).run("no_such_chain", {})
    assert "未在注册表" in str(excinfo.value)


# ==== 其他（async run 支持 / 契约形态）====


@pytest.mark.asyncio
async def test_async_worker_run_supported(tmp_path, monkeypatch, db_engine) -> None:
    """run 为 async 时同样支持（技术定：async/sync 双形态，await 包装）。"""
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="echoed"))
    _, _, _, build = make_runner_env(
        tmp_path, monkeypatch, run_py=PAUSE_RUN_PY
    )
    result = await build(db_engine, agent_factory_fn=factory).run(
        "echo_chain", {"text": "hi"}
    )
    assert result.status == "done"
    assert agents[0].calls == 1


@pytest.mark.asyncio
async def test_audit_gate_unwritable_rejects_llm(tmp_path, monkeypatch, db_engine) -> None:
    """§7.3 先审计后调用：审计不可写 -> LLM 调用被拒 -> 任务 FAILED（agent 未被调）。"""
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="echoed"))
    _, _, _, build = make_runner_env(tmp_path, monkeypatch)

    class _UnwritableGate:
        def ensure_writable(self) -> None:
            raise RuntimeError("审计不可写（fake）")

    result = await build(
        db_engine, agent_factory_fn=factory, audit_gate=_UnwritableGate()
    ).run("echo_chain", {"text": "hi"})
    assert result.status == "failed"
    assert len(agents) == 1 and agents[0].calls == 0  # 审计不可写 -> 调用不允许发生
