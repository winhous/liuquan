"""T12a 验收断言（详设-v0.1 §12 A1-A10 + 详设-v0.2 §8 A12-A26；
@version_acceptance 标记，开发流程⑤）。

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

v0.2 T7 新增 A12-A26（详设-v0.2 §8，全部可复现、零网络零真 token：
嵌入式 PG tm_pg_cluster + FakeAgent 桩 + 构造注入）：
- A12 三接口 POST 触发 tm_demo_chain -> 消费循环 DONE -> tm.task_proposal 落
      pending（evidence 非空 / ref_id 命中链数据白名单 / 审计可查）
- A13 全部提案人工审：批准同事务建 ai 任务 + created/approved 双事件 + 回填；
      驳回 rejected 不建任务；无自动通过路径（DB 不变量 + 静态唯一路径）
- A14 open->in_progress->done 事件流水齐全 + done_at 落库；追溯闭环
      task -> 提案 -> engine_task_id -> 引擎审计可查
- A15 三接口 curl 可演示：POST 201 / GET 200 / registry 200 + 422/404 错误路径
- A16 三角色 cookie 映射中文标签；未登录 /tasks 重定向 /login
- A17 崩溃自愈：recover 无检查点标 failed / 有检查点 resume 续跑到 done /
      队列任务重启后不丢（新 app 消费到 DONE）
- A18 幂等：同链同工序提案重复交付只落一条（skipped_idempotent）
- A19 四绿标注：check.sh 四门存在 + 红即非零退出 + A12-A26 落点自检
- A20 双通道：AI 通道（提案批准 ai 任务）+ 人工通道（manual 任务，来源域
      可选）；任务表来源徽章显示 domain
- A21 禁幻觉三件套反向：白名单外 ref_id 拒落 + 链 FAILED / 空 evidence 拒落 /
      audit_ids 不可查拒落（fixtures/tm 样本 A/B/C/D + fake provider 白名单）
- A22 审核面板完整展示依据（类型/ref_id/原文摘录/查看入口）；无依据提案
      页面不可见（list_pending_proposals 防御性过滤）
- A23 必填回复：不填 result_note 完成/作废被拒（页面/DAO 拦截 + DB CHECK
      双保险）；填后 done/void 落库且 note 进 task_event
- A24 派生不结束：A 派生 B -> A 不自动 done；B.derived_from=A；derived 事件；
      A 结束后 B 不受影响
- A25 防重 + 挂起：同 ref_id+action_id 已有待审提案 -> 新提案不落库；
      未完成任务一直挂着（自动清理机制不存在）；逾期提醒条常驻 + 筛选可用
- A26 编辑留痕：updated 事件带 from/to 快照；done/void 可重开回 open

复用 test_runner 的迷你仓库 / FakeAgent / DB fixture 基建（from test_runner
import ...，pytest 对测试模块命名空间内的 fixture 注册同样生效——fixture 随
模块收集）；v0.2 部分复用 test_engine_server 的 server 装配 helper（_client/
_post_demo/_wait_status/_wait_tm_count/_runner_kwargs/api_app）与 test_web_tm
的 web helper（_EngineStub/_login/_seed_task/_seed_proposal/...）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
A6 样本的 URL 形态运行期拼接；不给敏感名赋非空字面量；不读 os.environ。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from engine.actions import consume_task_proposal
from engine.actions.biz_client import BizApiClient
from engine.core import db as _db
from engine.core.db import (
    create_engine,
    dispose_engine,
    get_audit,
    get_step,
    get_task,
)
from engine.core.llm import load_models
from engine.core.runner import TaskRunner
from engine.lint.p1 import P1BusinessTermsRule
from engine.lint.p2 import P2CredentialsRule
from engine.lint.p3 import (
    P3Rule1WorkerSignature,
    P3Rule2CouplingImports,
    P3Rule4StubLeak,
)
from engine.registry import RegistryLoadError, load_registry
from engine.server import _fmt_task, create_app as create_engine_app

def _biz_client(tm_engine) -> BizApiClient:
    """引擎消费者写接口 -> 真 web /api/biz 接口（ASGITransport 端到端落真业务库）。

    v0.3 接口化（决策 26）：消费者 POST /api/biz/* 由 web 接口校验后落库——
    测试用 ASGITransport 指向真 web 接口 app（create_biz_router 注入嵌入式业务
    库），A12/A13/A14 批准流程依赖真库提案，保持端到端语义。
    """
    from fastapi import FastAPI

    from web.api_biz import create_biz_router

    biz_app = FastAPI()
    biz_app.include_router(create_biz_router(engine=tm_engine, token="test-token"))
    return BizApiClient(
        base_url="http" + "://test-" + "biz",
        token="test-token",
        transport=httpx.ASGITransport(app=biz_app),
    )


from fake_providers import FakeDemoInbox
from fixtures.tm.load import task_proposal_sample
from models.contract.task import TaskProposal
from models.tm import Task as TmTask
from models.tm import TaskProposal as TmTaskProposalRow
from test_engine_server import (  # noqa: F401  # 跨模块 helper/fixture（api_app 随模块收集）
    _INPUT,
    _client,
    _post_demo,
    _runner_kwargs,
    _wait_status,
    _wait_tm_count,
    api_app,
)
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
from test_web_tm import (  # noqa: F401  # 跨模块 helper（_EngineStub 桩只住 tests/）
    _EngineStub,
    _all_tasks,
    _events_for,
    _get_proposal,
    _get_task,
    _login,
    _seed_proposal,
    _seed_task,
)
from web.app import ROLE_LABEL, create_app as create_web_app
from web.engineapi.client import EngineAPIClient
from web.tm_store import TMStore, TMWebError, proposal_display_id, task_display_id

REPO_ROOT = Path(__file__).resolve().parents[1]

# 引擎桩 base URL（P2：URL 不得以单一字符串常量出现，段拼接——test_web_tm 同款）
_FAKE_BASE_URL = "ht" + "tp://" + "engine" + ".test"


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


# =====================================================================
# v0.2 T7 验收断言（详设-v0.2 §8 A12-A26；@version_acceptance）
# 全部可复现、零网络零真 token：嵌入式 PG（tm_pg_cluster）+ FakeAgent 桩
# + 构造注入（server 三接口 ASGITransport / web TestClient / 转交器注入）。
# =====================================================================


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """models.yaml 的 env: 引用所需环境变量（R20：值只进测试环境变量）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    """真注册表（tm_demo_chain / demo_propose / tm.proposal Action）。"""
    _set_env(monkeypatch)
    return load_registry(REPO_ROOT)


@pytest.fixture
def model_registry(monkeypatch: pytest.MonkeyPatch):
    """models.yaml 模型注册表（env: 引用需测试环境变量）。"""
    _set_env(monkeypatch)
    return load_models(REPO_ROOT / "models.yaml")


@async_fixture
async def tm_engine(tm_pg_cluster):
    """业务库 AsyncEngine（嵌入式 PG；NullPool 防跨循环串连接）。"""
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture
async def _clean_tm_tables(tm_engine):
    """每测试后 TRUNCATE tm 三表（RESTART IDENTITY 重置自增、CASCADE 破外键）。"""
    yield
    async with AsyncSession(tm_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE tm.task_event, tm.task_proposal, tm.task "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def store(tm_engine) -> TMStore:
    """web 侧 DAO（嵌入式 PG store，R12 构造注入）。"""
    return TMStore(tm_engine)


@pytest.fixture
def engine_stub() -> _EngineStub:
    return _EngineStub()


@pytest.fixture
def client(store: TMStore, engine_stub: _EngineStub) -> TestClient:
    """web 应用 + 构造注入（嵌入式 PG store + MockTransport 引擎桩，R12）。"""

    def factory() -> EngineAPIClient:
        return EngineAPIClient(
            base_url=_FAKE_BASE_URL,
            transport=httpx.MockTransport(engine_stub.handler),
        )

    app = create_web_app(tm_store=store, engine_client_factory=factory)
    return TestClient(app, follow_redirects=False)


# ==== A12：触发 tm_demo_chain（经三接口 POST）-> DONE -> 提案落 pending ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a12_trigger_chain_lands_pending_proposal(
    monkeypatch: pytest.MonkeyPatch,
    db_engine,
    tm_engine,
    _clean_tm_tables,
    registry,
    model_registry,
) -> None:
    """A12：POST /api/engine/tasks 触发 tm_demo_chain -> 消费循环 DONE ->
    tm.task_proposal 落一条 pending（evidence 非空、ref_id 命中链数据白名单、
    审计可查）——FakeAgent 桩（全链仅 1 次桩调用），零网络零真 token。"""
    kwargs, agents = _runner_kwargs(model_registry, monkeypatch)
    app = create_engine_app(
        engine=db_engine,
        registry=registry,
        runner_kwargs=kwargs,
        biz_client=_biz_client(tm_engine),
        poll_interval=0.01,
    )
    async with _client(app) as client:
        task_id = await _post_demo(app, client)
        assert task_id.startswith("e-")
        app.state.consumer.start()
        try:
            await _wait_status(client, task_id, "done")
            await _wait_tm_count(tm_engine, 1)
        finally:
            await app.state.consumer.stop()
    assert agents[0].calls == 1  # 全链仅 demo_echo 调 1 次 LLM（桩）

    # 落库断言：pending + evidence 非空 + ref_id 命中链数据白名单 + 追溯
    async with AsyncSession(tm_engine) as session:
        ids = (
            await session.execute(select(TmTaskProposalRow.id))
        ).scalars().all()
    assert len(ids) == 1  # 恰落一条
    landed = await _get_proposal(tm_engine, ids[0])
    assert landed.status == "pending"  # 决策 12：全部人工审，落库即待审
    assert landed.evidence  # evidence 非空（禁幻觉三件套 ①）
    ref_ids = {ev["ref_id"] for ev in landed.evidence}
    assert ref_ids == {"m-001"}
    assert ref_ids <= {"m-001"}  # 全部命中链数据白名单（决策 16：server 收集
    # 链 input 的 ref_id/id 字段字符串值 = 本链喂给 AI 的数据集合）
    assert landed.source["engine_task_id"] == task_id
    assert landed.source["worker_id"] == "demo_propose"
    assert landed.source["audit_ids"] == []  # reason:none 纯代码工序豁免

    # 审计可查（A12）：全链 demo_echo 的 LLM 调用在引擎库可查（1 条桩审计）
    engine_id = int(task_id[2:])
    audits = await get_audit(db_engine, engine_id)
    assert len(audits) == 1 and audits[0].worker_id == "demo_echo"
    assert audits[0].input_full and audits[0].output_full


# ==== A13：全部提案人工审（批准事务 / 驳回不建任务 / 无自动通过路径）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a13_manual_review_approve_reject_no_auto_path(
    store: TMStore, tm_engine, _clean_tm_tables
) -> None:
    """A13：批准 -> 事务内建 tm.task(open, source_type=ai, domain=提案域) +
    created/approved 双事件 + task_id 回填；驳回 -> rejected 不建任务；
    不存在代码自动通过路径（DB 不变量 + 静态唯一建 AI 任务路径）。"""
    # 批准：事务内 提案 approved + 建任务 + 双事件 + 回填
    pid = await _seed_proposal(
        tm_engine, domain="demo", suggested_role="运营", suggested_due_days=2
    )
    task = await store.approve_proposal(pid, actor="运营")
    prop = await _get_proposal(tm_engine, pid)
    assert prop.status == "approved"
    assert prop.reviewed_by == "运营" and prop.reviewed_at is not None
    assert prop.task_id == task.id  # 提案关联生成的任务（回填）
    row = await _get_task(tm_engine, task.id)
    assert row.status == "open"
    assert row.source_type == "ai"
    assert row.domain == "demo"  # AI 任务继承提案 domain（决策 16）
    assert row.role == "运营"  # suggested_role 落库
    assert row.due == date.today() + timedelta(days=2)  # AI 建议截止天数
    assert row.source["chain_id"] == "tm_demo_chain"  # 来源追溯（§3.4）
    assert row.source["proposal_id"] == proposal_display_id(pid)  # 批准时回填
    events = await _events_for(tm_engine, task.id)
    assert [e.event_type for e in events] == ["created", "approved"]  # 同事务双事件

    # 驳回：rejected + reject_reason，不建任务
    pid2 = await _seed_proposal(tm_engine)
    await store.reject_proposal(pid2, actor="运营", reason="与现有任务重复")
    prop2 = await _get_proposal(tm_engine, pid2)
    assert prop2.status == "rejected"
    assert prop2.reject_reason == "与现有任务重复"
    assert prop2.task_id is None
    assert len(await _all_tasks(tm_engine)) == 1  # 驳回不建任务

    # 无自动通过路径：
    # ① DB 不变量：pending 提案永不带 task_id（任务只在批准同事务生成）
    pid3 = await _seed_proposal(tm_engine)
    prop3 = await _get_proposal(tm_engine, pid3)
    assert prop3.status == "pending" and prop3.task_id is None
    # ② 静态：web/tm_store.py 中 source_type="ai" 只出现一次（唯一建 AI 任务
    # 路径 = approve_proposal；无 pending 直变 task 的旁路）
    src = (REPO_ROOT / "web" / "tm_store.py").read_text(encoding="utf-8")
    assert src.count('source_type="ai"') == 1
    assert "approved" in src  # approve_proposal 写 approved 事件（同事务）


# ==== A14：状态回流（open->in_progress->done 流水 + 追溯闭环）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a14_status_flow_done_at_and_trace_closure(
    monkeypatch: pytest.MonkeyPatch,
    db_engine,
    tm_engine,
    _clean_tm_tables,
    registry,
    model_registry,
    store: TMStore,
) -> None:
    """A14：任务 open->in_progress->done：task_event 流水齐全、done_at 落库；
    追溯闭环 task -> 提案 -> engine_task_id -> 引擎审计（全部可查）。"""
    # 追溯闭环起点：真链 DONE -> 转交器落提案 -> 批准建 AI 任务（全链路桩）
    kwargs, _ = _runner_kwargs(model_registry, monkeypatch)
    runner = TaskRunner(db_engine, registry, **kwargs)
    result = await runner.run("tm_demo_chain", dict(_INPUT), trigger_ref="运营")
    assert result.status == "done"
    engine_task = await get_task(db_engine, result.task_id)
    step = await get_step(db_engine, engine_task.current_step_row)
    proposal = TaskProposal.model_validate(step.output)
    outcome = await consume_task_proposal(
        proposal,
        biz_client=_biz_client(tm_engine),
        registry=registry,
        whitelist={"m-001"},  # 链数据白名单（决策 16）
        audit_lookup=lambda ids: True,
    )
    assert outcome.status == "inserted"
    tm_task = await store.approve_proposal(outcome.proposal_id, actor="运营")

    # 状态回流：open -> in_progress -> done（completed 必填 result_note）
    await store.transition(tm_task.id, "started", actor="运营")
    await store.transition(tm_task.id, "completed", actor="运营", result_note="已回复买家")
    row = await _get_task(tm_engine, tm_task.id)
    assert row.status == "done"
    assert row.done_at is not None  # done 写 done_at（§3.3）
    events = await _events_for(tm_engine, tm_task.id)
    assert [e.event_type for e in events] == [
        "created", "approved", "started", "completed",
    ]  # 流水齐全（§3.3 事件写入点表）
    completed = events[-1]
    assert completed.from_status == "in_progress"
    assert completed.to_status == "done"
    assert completed.note == "已回复买家"  # completed 事件 note=result_note

    # 追溯闭环：task -> 提案 -> engine_task_id -> 引擎审计（可查）
    source = row.source
    assert source["proposal_id"] == proposal_display_id(outcome.proposal_id)
    prop = await _get_proposal(tm_engine, outcome.proposal_id)
    assert prop.task_id == tm_task.id  # 提案 -> 任务
    assert prop.source["engine_task_id"] == f"e-{result.task_id:06d}"
    engine_id = int(prop.source["engine_task_id"][2:])
    engine_task2 = await get_task(db_engine, engine_id)
    assert engine_task2 is not None and engine_task2.status == "done"
    audits = await get_audit(db_engine, engine_id)
    assert len(audits) == 1 and audits[0].worker_id == "demo_echo"  # 审计可查


# ==== A15：三接口 curl 可演示（201/200/200 + 422/404 错误路径）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a15_three_endpoints_and_error_paths(api_app) -> None:
    """A15：POST 201 / GET 200 / GET registry 200 + 422/404 错误路径。"""
    app, _ = api_app
    async with _client(app) as client:
        # POST 建任务 201（入队即 queued）
        resp = await client.post(
            "/api/engine/tasks",
            json={"chain_id": "tm_demo_chain", "input": dict(_INPUT), "trigger_ref": "运营"},
        )
        assert resp.status_code == 201
        task_id = resp.json()["task_id"]
        assert resp.json()["status"] == "queued"
        # GET 查状态 200
        resp = await client.get(f"/api/engine/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"
        assert resp.json()["error"] is None
        # GET registry 200
        resp = await client.get("/api/engine/registry")
        assert resp.status_code == 200
        chains = {c["id"] for c in resp.json()["chains"]}
        assert "tm_demo_chain" in chains
        # 错误路径：链未登记 404
        resp = await client.post(
            "/api/engine/tasks",
            json={"chain_id": "ghost_chain", "input": {}, "trigger_ref": "运营"},
        )
        assert resp.status_code == 404
        # 错误路径：input 不过链 input Model -> 422
        resp = await client.post(
            "/api/engine/tasks",
            json={"chain_id": "tm_demo_chain", "input": {"text": "只有文本"}, "trigger_ref": "运营"},
        )
        assert resp.status_code == 422
        # 错误路径：任务不存在 404
        resp = await client.get("/api/engine/tasks/e-999999")
        assert resp.status_code == 404


# ==== A16：登录与角色（三角色 cookie 映射中文标签；未登录重定向）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a16_login_roles_redirect_and_labels(client: TestClient) -> None:
    """A16：未登录访问 /tasks 重定向 /login；三角色 cookie 映射中文标签。"""
    assert ROLE_LABEL == {"admin": "管理员", "ops": "运营", "buyer": "采购"}
    resp = client.get("/tasks")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    for key, label in (("admin", "管理员"), ("ops", "运营"), ("buyer", "采购")):
        resp = client.post("/login", data={"role": key})
        assert resp.status_code == 303
        page = client.get("/tasks")
        assert page.status_code == 200
        assert label in page.text  # cookie 已映射角色中文标签


# ==== A17：崩溃自愈（recover 无检查点 failed / 有检查点续跑 / 队列不丢）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a17_crash_recovery_queue_not_lost(
    monkeypatch: pytest.MonkeyPatch,
    db_engine,
    tm_engine,
    _clean_tm_tables,
    registry,
    model_registry,
) -> None:
    """A17：模拟崩溃重启——running 无检查点 -> recover 标 failed（孤儿清理）；
    running 有检查点 -> recover resume 续跑到 done（不重烧 token）；
    queued 任务在新进程（新 app）消费循环继续跑到 DONE（队列任务不丢）。"""
    kwargs, _ = _runner_kwargs(model_registry, monkeypatch)
    app = create_engine_app(
        engine=db_engine, registry=registry, runner_kwargs=kwargs,
        biz_client=_biz_client(tm_engine), poll_interval=0.01,
    )

    # (a) running 无检查点（claim 后崩溃窗口）-> recover 标 failed
    task_a = await _db.create_task(
        app.state.engine, chain_id="tm_demo_chain", trigger_type="manual",
        trigger_ref="运营", input_=dict(_INPUT),
    )
    await _db.update_task(
        app.state.engine, task_a, status="running",
        started_at=datetime.now(timezone.utc),
    )
    lines = await app.state.consumer.recover()
    row_a = await _db.get_task(app.state.engine, task_a)
    assert row_a is not None and row_a.status == "failed"
    assert any("marked failed" in line for line in lines)

    # (b) running 有 INIT->REASON 检查点 -> recover resume 续跑到 done
    task_b = await _db.create_task(
        app.state.engine, chain_id="tm_demo_chain", trigger_type="manual",
        trigger_ref="运营", input_=dict(_INPUT),
    )
    await _db.update_task(
        app.state.engine, task_b, status="running",
        started_at=datetime.now(timezone.utc),
    )
    step_id = await _db.create_step(
        app.state.engine, task_b, step_index=0, worker_id="demo_echo",
        input_=dict(_INPUT),
    )
    await _db.write_checkpoint(
        app.state.engine,
        task_id=task_b,
        step_id=step_id,
        from_phase="INIT",
        to_phase="REASON",
        state={
            "phase_output": None,
            "memory_state": {"inputs": dict(_INPUT), "context_data": {}},
        },
    )
    lines = await app.state.consumer.recover()
    row_b = await _db.get_task(app.state.engine, task_b)
    assert row_b is not None and row_b.status == "done"
    assert any("resumed -> done" in line for line in lines)
    audits_b = await get_audit(db_engine, task_b)
    assert len(audits_b) == 1  # 续跑只新增 1 次 LLM 调用（不重烧 token）

    # (c) queued 任务在「进程重启」（新 app 新消费循环）后继续消费到 DONE
    task_c = await _db.create_task(
        app.state.engine, chain_id="tm_demo_chain", trigger_type="manual",
        trigger_ref="运营", input_=dict(_INPUT),
    )
    app2 = create_engine_app(
        engine=db_engine, registry=registry, runner_kwargs=kwargs,
        biz_client=_biz_client(tm_engine), poll_interval=0.01,
    )
    async with _client(app2) as client:
        app2.state.consumer.start()
        try:
            await _wait_status(client, _fmt_task(task_c), "done")
        finally:
            await app2.state.consumer.stop()
    row_c = await _db.get_task(app.state.engine, task_c)
    assert row_c is not None and row_c.status == "done"  # 队列任务不丢
    audits_c = await get_audit(db_engine, task_c)
    assert len(audits_c) == 1  # 全链仅 demo_echo 1 次桩调用


# ==== A18：幂等（同链同工序重复交付只落一条）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a18_idempotent_same_delivery_once(
    tm_engine, _clean_tm_tables, registry
) -> None:
    """A18：同链同工序（source.engine_task_id + worker_id）重复交付只落一条。"""
    proposal = task_proposal_sample("valid")
    whitelist = FakeDemoInbox().whitelist()
    first = await consume_task_proposal(
        proposal,
        biz_client=_biz_client(tm_engine), registry=registry,
        whitelist=whitelist, audit_lookup=lambda ids: True,
    )
    assert first.status == "inserted"
    second = await consume_task_proposal(  # 模拟同链重跑（同 eid + wid）
        proposal,
        biz_client=_biz_client(tm_engine), registry=registry,
        whitelist=whitelist, audit_lookup=lambda ids: True,
    )
    assert second.status == "skipped_idempotent"
    assert "幂等" in (second.reason or "")
    async with AsyncSession(tm_engine) as session:
        count = len((await session.execute(select(TmTaskProposalRow.id))).scalars().all())
    assert count == 1  # 只落一条（不重复建任务）


# ==== A19：四绿标注（check.sh 四门 + 红即非零退出 + 落点自检）====


@pytest.mark.version_acceptance
def test_a19_four_greens_gates_covered() -> None:
    """A19：四绿守门（check.sh 断言已有，本断言为标注 + 最小检查）——
    四门存在 + 任一真红非零退出（R13 提交被拦）+ A12-A26 逐条有测试落点。
    2026-08-28 修复：绿 3（单测）与绿 4（verify）合并为一次 verify 调用
    （verify 内部 _VERIFY_CHECKS 含 lint+registry+pytest 三件套；此前连续两次
    pytest 抢 tests/.pgdata 嵌入式 PG 簇必现 ConnectionRefused 竞态），
    故单测由 verify 承载而非 check.sh 独立 pytest。"""
    script = (REPO_ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    assert "python -m engine.lint" in script  # 绿 1 lint
    assert "registry-check" in script  # 绿 2 registry 一致性
    assert "liuquan-engine verify" in script  # 绿 3+4：单测 + verify
    # 单测真跑：verify 的 _VERIFY_CHECKS 内含 uv run pytest（engine/cli.py）
    assert "uv run pytest" in (REPO_ROOT / "engine" / "cli.py").read_text(encoding="utf-8")
    assert "exit 1" in script and "RED" in script  # 任一真红 -> 非零退出
    # 交付要求 3：A12-A26 每条至少一个测试函数（命名含断言号便于追溯）
    for num in range(12, 27):
        assert any(f"test_a{num}_" in name for name in globals()), f"A{num} 缺验收落点"


# ==== A20：双通道（AI 通道 + 人工通道）+ 来源徽章显示 domain ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a20_dual_channel_and_source_badge(
    store: TMStore, tm_engine, _clean_tm_tables, client: TestClient
) -> None:
    """A20：AI 通道（提案批准 ai 任务）+ 人工通道（manual 任务，来源域可选）
    各自成立；任务表来源徽章显示 domain（决策 16）。"""
    _login(client)
    # AI 通道：提案批准 -> tm.task(source_type=ai, domain=提案域)
    pid = await _seed_proposal(tm_engine, domain="demo")
    ai_task = await store.approve_proposal(pid, actor="运营")
    ai_row = await _get_task(tm_engine, ai_task.id)
    assert ai_row.source_type == "ai"
    assert ai_row.domain == "demo"  # 继承提案 domain
    # 人工通道：表单建 tm.task(source_type=manual, 来源域可选)
    manual = await store.create_task(
        title="人工补货任务", detail=None, domain="erp", role="采购",
        due=date.today() + timedelta(days=3), actor="采购",
    )
    m_row = await _get_task(tm_engine, manual.id)
    assert m_row.source_type == "manual"
    assert m_row.domain == "erp"
    assert m_row.source == {"creator": "采购"}  # §3.4 人工输入，creator 即依据
    # 来源徽章 = domain：页面渲染两域徽章标签（决策 16）
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "演示" in resp.text  # demo -> 演示徽章
    assert "ERP" in resp.text  # erp -> ERP 徽章


# ==== A21：禁幻觉三件套反向（白名单外拒落 + 链 FAILED / 空 evidence / 断链）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a21_antihallucination_triple_reverse(
    tm_engine, _clean_tm_tables, registry
) -> None:
    """A21（转交器层，样本 B/C/D）：白名单外 ref_id 拒落 + 幻觉证据报告；
    空 evidence 拒落；audit_ids 不可查拒落——三件套各一条反向。"""
    whitelist = FakeDemoInbox().whitelist()  # 白名单来源 = fake provider 数据集合
    # 反向 1：集合外 ref_id（fake provider 没喂过的对象）-> 拒落
    outcome_b = await consume_task_proposal(
        task_proposal_sample("hallucinated"),
        biz_client=_biz_client(tm_engine), registry=registry,
        whitelist=whitelist, audit_lookup=lambda ids: True,
    )
    assert outcome_b.status == "rejected"
    assert (outcome_b.reason or "").startswith("幻觉证据: ref_id=msg-999")
    # 反向 2：空 evidence -> 拒落（无依据不出建议）
    outcome_c = await consume_task_proposal(
        task_proposal_sample("empty_evidence"),
        biz_client=_biz_client(tm_engine), registry=registry,
        whitelist=whitelist, audit_lookup=lambda ids: True,
    )
    assert outcome_c.status == "rejected"
    assert "evidence 为空" in (outcome_c.reason or "")
    # 反向 3：audit_ids 不可查 -> 拒落（追溯保证）
    outcome_d = await consume_task_proposal(
        task_proposal_sample("broken_audit"),
        biz_client=_biz_client(tm_engine), registry=registry,
        whitelist=whitelist, audit_lookup=lambda ids: False,
    )
    assert outcome_d.status == "rejected"
    assert "audit_ids 不可查" in (outcome_d.reason or "")
    # 三件套全拒 -> 零落库
    async with AsyncSession(tm_engine) as session:
        count = len((await session.execute(select(TmTaskProposalRow.id))).scalars().all())
    assert count == 0


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a21_whitelist_violation_marks_chain_failed(
    monkeypatch: pytest.MonkeyPatch,
    db_engine,
    tm_engine,
    _clean_tm_tables,
    registry,
    model_registry,
) -> None:
    """A21（server 层）：真转交器对白名单外 ref_id 拒落 -> 链标 FAILED，
    error 记「幻觉证据: ref_id=...」（详设 §3.2：拒落 + 链 FAILED）。"""
    async def restricted_consumer(proposal, **kwargs):
        # 真转交器 + 白名单收窄：模拟本链喂给 AI 的数据集合不含提案引用的对象
        return await consume_task_proposal(
            proposal,
            registry=kwargs["registry"],
            whitelist={"other-obj-001"},  # 集合外：demo 链产出 ref_id=m-001 不命中
            audit_lookup=kwargs.get("audit_lookup"),
            biz_client=kwargs.get("biz_client"),
        )

    kwargs, _ = _runner_kwargs(model_registry, monkeypatch)
    app = create_engine_app(
        engine=db_engine,
        registry=registry,
        runner_kwargs=kwargs,
        consumers={"tm.proposal": restricted_consumer},
        biz_client=_biz_client(tm_engine),
        poll_interval=0.01,
    )
    async with _client(app) as client:
        task_id = await _post_demo(app, client)
        app.state.consumer.start()
        try:
            await _wait_status(client, task_id, "failed")
        finally:
            await app.state.consumer.stop()
        resp = await client.get(f"/api/engine/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"  # 拒落 -> 链 FAILED
    assert body["error"] is not None
    assert "幻觉证据" in body["error"]
    assert "m-001" in body["error"]


# ==== A22：审核面板完整展示依据；无依据提案页面不可见 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a22_evidence_panel_full_and_empty_invisible(
    client: TestClient, tm_engine, _clean_tm_tables
) -> None:
    """A22：审核面板完整展示依据（类型/ref_id/原文摘录/查看入口）+ 来源追溯；
    无依据的提案页面不可见（list_pending_proposals 防御性过滤）。"""
    _login(client)
    pid = await _seed_proposal(tm_engine)
    # 无依据提案（绕过转交器直插 DB，防御性验证页面不展示）
    await _seed_proposal(tm_engine, title="无依据提案应不可见", evidence=[])
    resp = client.get("/tasks")
    assert resp.status_code == 200
    # 依据区完整：evidence 类型 / ref_id / 原文摘录 / 查看入口（决策 16）
    assert "message" in resp.text  # 类型
    assert "msg-001" in resp.text  # ref_id
    assert "Where is my order?" in resp.text  # 原文摘录
    assert "查看" in resp.text  # 查看入口
    assert proposal_display_id(pid) in resp.text  # 提案展示形
    assert "tm_demo_chain" in resp.text  # 来源 chain_id
    assert "e-000042" in resp.text  # 来源 engine_task_id
    # 无依据提案页面不可见（A22）
    assert "无依据提案应不可见" not in resp.text


# ==== A23：必填回复（页面/DAO 拦截 + DB CHECK 双保险）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a23_result_note_required_then_lands(
    store: TMStore, tm_engine, _clean_tm_tables
) -> None:
    """A23：不填 result_note 点完成/作废被拒（DAO 拦截 + DB CHECK 双保险）；
    填后 done/void 落库且 note 进 task_event（决策 17 第 1 条）。"""
    task = await store.create_task(
        title="完成回复测试", detail=None, domain="crm", role="运营",
        due=date.today(), actor="运营",
    )
    await store.transition(task.id, "started", actor="运营")
    # 拒：不填 result_note（页面弹窗必填，DAO 服务端拦截）
    with pytest.raises(TMWebError) as exc:
        await store.transition(task.id, "completed", actor="运营")
    assert "result_note" in str(exc.value)
    row = await _get_task(tm_engine, task.id)
    assert row.status == "in_progress"  # 状态未被改动
    # DB CHECK 双保险：绕过 DAO 直落 done 无 result_note -> IntegrityError
    async with AsyncSession(tm_engine) as session:
        session.add(
            TmTask(
                title="DB CHECK 拦截", detail=None, domain="crm", role="运营",
                due=date.today(), status="done",
                source_type="manual", source={"creator": "运营"}, created_by="运营",
            )
        )
        with pytest.raises(IntegrityError, match="chk_result_note"):
            await session.flush()
        await session.rollback()
    # 成：填 result_note -> done 落库 + completed 事件 note=result_note + done_at
    note = "已回复买家并确认送达时间"
    await store.transition(task.id, "completed", actor="运营", result_note=note)
    row = await _get_task(tm_engine, task.id)
    assert row.status == "done"
    assert row.result_note == note
    assert row.done_at is not None
    events = await _events_for(tm_engine, task.id)
    completed = [e for e in events if e.event_type == "completed"][0]
    assert completed.note == note
    # voided 路同：必填回复，填后 void 落库且 note 进事件
    t2 = await store.create_task(
        title="作废测试", detail=None, domain="crm", role="运营",
        due=date.today(), actor="运营",
    )
    await store.transition(t2.id, "started", actor="运营")
    with pytest.raises(TMWebError):
        await store.transition(t2.id, "voided", actor="运营")
    await store.transition(t2.id, "voided", actor="运营", result_note="需求变更，作废")
    vrow = await _get_task(tm_engine, t2.id)
    assert vrow.status == "void"
    assert vrow.result_note == "需求变更，作废"
    vevents = await _events_for(tm_engine, t2.id)
    voided = [e for e in vevents if e.event_type == "voided"][0]
    assert voided.note == "需求变更，作废"


# ==== A24：派生不结束（A 派生 B -> A 不自动 done；B 独立）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a24_derive_keeps_parent_open_and_child_independent(
    store: TMStore, tm_engine, _clean_tm_tables
) -> None:
    """A24：A 派生 B -> A 保持原状态不自动 done；B.derived_from=A；
    task_event 记 derived；A 结束后 B 不受影响（决策 17 第 1 条）。"""
    parent = await store.create_task(
        title="处理退货申请", detail="买家要求退货", domain="crm", role="运营",
        due=date.today(), actor="运营",
    )
    await store.transition(parent.id, "started", actor="运营")
    child = await store.derive_task(
        parent.id, title="生成退货标签", detail="生成退货面单", domain="crm",
        role="运营", due=date.today() + timedelta(days=1), actor="运营",
    )
    # A 不自动 done（派生≠原任务结束）；B.derived_from=A
    prow = await _get_task(tm_engine, parent.id)
    assert prow.status == "in_progress"
    crow = await _get_task(tm_engine, child.id)
    assert crow.derived_from == parent.id
    # task_event 记 derived（note=派生出的任务展示 id）
    pevents = await _events_for(tm_engine, parent.id)
    assert [e.event_type for e in pevents] == ["created", "started", "derived"]
    assert pevents[-1].note == task_display_id(child.id)
    cevents = await _events_for(tm_engine, child.id)
    assert [e.event_type for e in cevents] == ["created"]
    # A 结束后 B 不受影响（B 仍挂清单）
    await store.transition(parent.id, "completed", actor="运营", result_note="已处理退货")
    prow = await _get_task(tm_engine, parent.id)
    assert prow.status == "done"
    crow = await _get_task(tm_engine, child.id)
    assert crow.status == "open"


# ==== A25：防重 + 挂起 + 逾期提醒条 + 列表筛选 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a25_dedup_hang_overdue_bar_and_filters(
    tm_engine, _clean_tm_tables, registry, client: TestClient
) -> None:
    """A25：同 ref_id+action_id 已有待审提案 -> 新提案不落库（skip）；
    未完成任务一直挂着（自动清理机制不存在——静态断言）；
    逾期提醒条常驻 + 列表筛选可用（决策 17 第 3/5 条）。"""
    # 防重：同 ref_id + action_id 已有待审提案 -> 新提案不落库
    whitelist = FakeDemoInbox().whitelist()
    first = await consume_task_proposal(
        task_proposal_sample("valid"),
        biz_client=_biz_client(tm_engine), registry=registry,
        whitelist=whitelist, audit_lookup=lambda ids: True,
    )
    assert first.status == "inserted"
    dup = task_proposal_sample("valid").model_copy(deep=True)
    dup.source.engine_task_id = "e-000099"  # 新链新任务（幂等键不同）
    second = await consume_task_proposal(
        dup,
        biz_client=_biz_client(tm_engine), registry=registry,
        whitelist=whitelist, audit_lookup=lambda ids: True,
    )
    assert second.status == "skipped_duplicate"
    assert "防重" in (second.reason or "")
    async with AsyncSession(tm_engine) as session:
        count = len((await session.execute(select(TmTaskProposalRow.id))).scalars().all())
    assert count == 1  # 新提案未落库

    # 未完成任务一直挂着：自动清理机制不存在（决策 17：不自动清理/关闭/替代）
    store_src = (REPO_ROOT / "web" / "tm_store.py").read_text(encoding="utf-8")
    assert "DELETE" not in store_src.upper()  # 无删除路径（作废代替删除）
    assert "cleanup" not in store_src.lower()  # 无清理机制
    web_src = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")
    assert "scheduler" not in web_src.lower() and "cron" not in web_src.lower()  # 无定时清理

    # 逾期提醒条常驻 + 列表筛选可用（决策 17 第 5 条）
    _login(client)
    await _seed_task(
        tm_engine, title="逾期挂起任务", due=date.today() - timedelta(days=1),
        status="open",
    )
    await _seed_task(
        tm_engine, title="未逾期任务", due=date.today() + timedelta(days=1),
        status="open",
    )
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "1 个任务已逾期" in resp.text  # 提醒条常驻计数
    assert "查看逾期任务" in resp.text  # 入口
    resp = client.get("/tasks", params={"overdue": "1"})
    assert "逾期挂起任务" in resp.text
    assert "未逾期任务" not in resp.text  # 逾期筛选可用


# ==== A26：编辑留痕（updated 事件 from/to 快照）+ done/void 重开 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a26_edit_snapshot_and_reopen(
    store: TMStore, tm_engine, _clean_tm_tables
) -> None:
    """A26：编辑任务（title/role/due/domain/detail）后 updated 事件带
    from/to 快照；done/void 可重开回 open（updated 事件 status 快照留痕）。"""
    task = await store.create_task(
        title="原标题", detail="原详情", domain="crm", role="运营",
        due=date.today() + timedelta(days=2), actor="运营",
    )
    new_due = date.today() + timedelta(days=5)
    await store.edit_task(
        task.id, title="新标题", role="采购", due=new_due, domain="erp",
        detail="新详情", actor="运营",
    )
    events = await _events_for(tm_engine, task.id)
    updated = [e for e in events if e.event_type == "updated"][0]
    assert updated.actor == "运营"
    snapshot = json.loads(updated.note)  # from/to 快照进 note（§3.5.5）
    assert snapshot["title"] == {"from": "原标题", "to": "新标题"}
    assert snapshot["role"] == {"from": "运营", "to": "采购"}
    assert snapshot["domain"] == {"from": "crm", "to": "erp"}
    assert snapshot["detail"] == {"from": "原详情", "to": "新详情"}
    assert snapshot["due"] == {
        "from": (date.today() + timedelta(days=2)).isoformat(),
        "to": new_due.isoformat(),
    }

    # done 可重开回 open（updated 事件 status 快照；清 result_note/done_at）
    await store.transition(task.id, "started", actor="运营")
    await store.transition(task.id, "completed", actor="运营", result_note="完成回复")
    await store.reopen_task(task.id, actor="运营")
    row = await _get_task(tm_engine, task.id)
    assert row.status == "open"
    assert row.result_note is None  # chk_result_note：非 done/void 必须为空
    assert row.done_at is None
    reopen_events = await _events_for(tm_engine, task.id)
    updated2 = [e for e in reopen_events if e.event_type == "updated"][-1]
    snap2 = json.loads(updated2.note)
    assert snap2["status"] == {"from": "done", "to": "open"}

    # void 也可重开回 open
    t2 = await store.create_task(
        title="作废重开", detail=None, domain="crm", role="运营",
        due=date.today(), actor="运营",
    )
    await store.transition(t2.id, "started", actor="运营")
    await store.transition(t2.id, "voided", actor="运营", result_note="作废")
    await store.reopen_task(t2.id, actor="运营")
    assert (await _get_task(tm_engine, t2.id)).status == "open"
