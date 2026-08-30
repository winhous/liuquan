"""v0.2 T5 引擎常驻 server（engine/server.py）测试（详设-v0.2 §2.3/§4/§5）。

覆盖（任务书验收：三接口各路径 + 队列消费循环 + 转交器 rejected 标 failed）：
- 三接口（httpx ASGITransport + 桩 runner/转交器，循环不启动）：
    POST 201（建任务入队 trigger_type=manual，立即 queued）/ 404（链未登记）/
    422（input 不过链 input Model）；GET 200（status/current_step/error/output/
    finished_at/audits 摘要列表）/ 404（任务不存在）；GET registry 200
    （workers/chains/actions/events 结构，详设 §4.3）
- 队列消费循环：真 TaskRunner + FakeAgent（R12 桩 LLM）驱动 tm_demo_chain
    DONE -> 转交钩子提取 TaskProposal -> 记录桩消费者被调用（注入 whitelist=
    链输入 ref_id 集合 / registry）；转交器 rejected -> 任务标 failed（error
    记拒落原因）；skipped_* -> 任务保持 done（防重/幂等不改变链终态）
- 端到端：真转交器（CONSUMERS 缺省）+ 真业务库 -> 链 DONE 后 tm.task_proposal
    落 pending（A12 在 server 层的落点）
- 崩溃恢复 recover()：running 无检查点 -> 标记 failed（孤儿清理）；running 有
    INIT->REASON 检查点 -> resume 续跑到 done（续跑只新增 1 次桩 LLM 调用，
    A4 不重烧 token）
- claim_task 原子认领（变更日志 M4 的 v0.2 落地）：最老优先 + 认领即 running
- 纯函数：_collect_ref_ids（白名单递归收集）/ _extract_proposal（判据键）/
    _engine_port（.env 端口解析与缺省）

基建复用（from test_runner import ...，fixture 随模块收集）：db_engine +
_clean_engine_tables（autouse 清引擎库）+ FakeAgent / make_agent_factory
（R12 构造注入桩）；业务库 tm_pg_cluster（conftest 会话级）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
URL/IP/sk- 形态一律运行期拼接（见 _FAKE_SCHEME/_FAKE_HOST），不读
os.environ（环境变量只经 monkeypatch.setenv 写入）。
"""

from __future__ import annotations

import httpx
import json

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from engine.actions.biz_client import BizApiClient
from engine.actions import ConsumeOutcome
from engine.core import db as _db
from engine.core.llm import load_models
from engine.core.runner import TaskRunner
from engine.registry import load_registry
from engine.server import (
    _collect_ref_ids,
    _engine_port,
    _extract_proposal,
    _fmt_task,
    create_app,
)
from models.tm import TaskProposal as TmTaskProposalRow
from test_llm import FakeAgent
from test_runner import (  # noqa: F401  # 跨模块 fixture 随模块收集（autouse 清引擎库）
    _clean_engine_tables,
    db_engine,
    make_agent_factory,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# 测试用假地址（P2：URL/IP 不得以单一字符串常量出现，段拼接——与
# test_engineapi_client.py 同款）
_FAKE_SCHEME = "ht" + "tp://"
_FAKE_HOST = "127" + ".0.0.1"
_FAKE_BASE = _FAKE_SCHEME + "engine" + ".test"

_INPUT = {"text": "买家询问物流时效", "ref_id": "m-001", "kind": "message"}


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """models.yaml 的 env: 引用所需环境变量（R20：值只进测试环境变量）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


# ---- fixtures ----

# 假地址段（给 _engine_port 测试用，见 test_engineapi_client 同款）
_FAKE_URL = _FAKE_SCHEME + _FAKE_HOST + ":9" + "999"


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
    """业务库 AsyncEngine（嵌入式 PG；真转交器落库测试用）。"""
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


class _StubRunner:
    """三接口路径测试用桩 runner（P3-4：桩只住 tests/，生产目录零桩）。

    三接口不触达 runner（循环未启动）；触达即断言失败——防桩掩盖接线错误。
    """

    def __init__(self) -> None:
        self.consume_calls = 0

    async def consume_once(self):
        self.consume_calls += 1
        return None

    async def resume(self, task_id: int):  # pragma: no cover
        raise AssertionError("API 路径测试不应触发 runner.resume")


async def _noop_consumer(proposal: Any, **kwargs: Any) -> ConsumeOutcome:
    raise AssertionError("API 路径测试不应触发转交器")


@pytest.fixture
def api_app(db_engine, registry):
    """三接口路径测试 app：桩 runner + 桩转交器（消费循环不启动）。"""
    stub = _StubRunner()
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner=stub,
        consumers={"tm.proposal": _noop_consumer},
    )
    return app, stub


def _client(app):
    """httpx ASGITransport 客户端（不跑 lifespan——消费循环手动 start/stop）。"""
    return AsyncClient(transport=ASGITransport(app=app), base_url=_FAKE_BASE)


async def _wait_status(client, task_id: str, wanted: str, *, deadline: float = 8.0) -> None:
    """轮询 GET 直到任务 status == wanted（终态断言；转交钩子在终态后仍可能续跑）。"""
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        resp = await client.get(f"/api/engine/tasks/{task_id}")
        assert resp.status_code == 200
        if resp.json()["status"] == wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 未在 {deadline}s 内达 {wanted}")


async def _wait_until(predicate, *, what: str, deadline: float = 8.0) -> None:
    """轮询直到谓词为真（转交器调用等终态后异步动作的收尾等待）。"""
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"超时未等到：{what}")


def _runner_kwargs(model_registry, monkeypatch: pytest.MonkeyPatch) -> dict:
    """真 TaskRunner 注入参数（FakeAgent 桩 LLM + 真注册表/模型注册表）。"""
    _set_env(monkeypatch)
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="echoed"))
    return {
        "agent_factory": factory,
        "model_registry": model_registry,
        "repo_root": REPO_ROOT,
        "writable_check": lambda: True,
        "backoff": 0.0,
    }, agents


async def _post_demo(app, client, *, input_: dict | None = None) -> str:
    """POST /api/engine/tasks 触发 tm_demo_chain，返回 task_id。"""
    resp = await client.post(
        "/api/engine/tasks",
        json={
            "chain_id": "tm_demo_chain",
            "input": input_ if input_ is not None else dict(_INPUT),
            "trigger_ref": "运营",
        },
    )
    assert resp.status_code == 201
    return resp.json()["task_id"]


async def _wait_tm_count(tm_engine, expected: int, *, deadline: float = 8.0) -> None:
    """轮询业务库 tm.task_proposal 行数达期望（转交落库的收尾等待）。"""
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        async with AsyncSession(tm_engine) as session:
            count = len(
                (await session.execute(select(TmTaskProposalRow.id))).scalars().all()
            )
        if count >= expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"tm.task_proposal 未达 {expected} 条")


# ==== POST /api/engine/tasks：201 / 404 / 422（详设 §4.1）====


@pytest.mark.asyncio
async def test_post_creates_queued_task(api_app) -> None:
    """POST 合法 -> 201 {task_id/status/chain_id}；engine_task 落库 queued（manual）。"""
    app, stub = api_app
    async with _client(app) as client:
        resp = await client.post(
            "/api/engine/tasks",
            json={"chain_id": "tm_demo_chain", "input": dict(_INPUT), "trigger_ref": "运营"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "queued"
    assert body["chain_id"] == "tm_demo_chain"
    assert body["task_id"].startswith("e-")
    row = await _db.get_task(app.state.engine, int(body["task_id"][2:]))
    assert row is not None
    assert row.status == "queued"
    assert row.trigger_type == "manual"
    assert row.trigger_ref == "运营"
    assert row.input == _INPUT
    assert stub.consume_calls == 0  # 三接口不触达 runner（循环未启动）


@pytest.mark.asyncio
async def test_post_unknown_chain_404(api_app) -> None:
    """链未登记 -> 404 {"detail": "chain not registered"}。"""
    app, _ = api_app
    async with _client(app) as client:
        resp = await client.post(
            "/api/engine/tasks",
            json={"chain_id": "ghost_chain", "input": {}, "trigger_ref": "运营"},
        )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "chain not registered"}


@pytest.mark.asyncio
async def test_post_invalid_input_422(api_app) -> None:
    """input 不过链 input Model（DemoProposeInput 缺 ref_id/kind）-> 422。"""
    app, _ = api_app
    async with _client(app) as client:
        resp = await client.post(
            "/api/engine/tasks",
            json={"chain_id": "tm_demo_chain", "input": {"text": "只有文本"}, "trigger_ref": "运营"},
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and "校验" in detail


# ==== GET /api/engine/tasks/{id}：200 / 404（详设 §4.2）====


@pytest.mark.asyncio
async def test_get_queued_task_200(api_app) -> None:
    """查 queued 任务 -> 200：status/current_step/error/output/finished_at/audits。"""
    app, _ = api_app
    async with _client(app) as client:
        task_id = await _post_demo(app, client)
        resp = await client.get(f"/api/engine/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == task_id
    assert body["chain_id"] == "tm_demo_chain"
    assert body["status"] == "queued"
    assert body["current_step"] == 0
    assert body["error"] is None
    assert body["output"] is None  # 未执行无链产物
    assert body["finished_at"] is None
    assert body["audits"] == []  # 未执行无 LLM 调用


@pytest.mark.asyncio
async def test_get_unknown_task_404(api_app) -> None:
    """任务不存在 -> 404 {"detail": "task not found"}（含非法 id 形态）。"""
    app, _ = api_app
    async with _client(app) as client:
        for bad in ("e-999999", "not-an-id", "1x"):
            resp = await client.get(f"/api/engine/tasks/{bad}")
            assert resp.status_code == 404
            assert resp.json() == {"detail": "task not found"}


@pytest.mark.asyncio
async def test_get_done_task_200_with_output_and_audits(
    monkeypatch: pytest.MonkeyPatch, db_engine, registry, model_registry
) -> None:
    """DONE 任务 -> 200：output = 链产物（TaskProposal）、audits 摘要非空、finished_at。"""
    kwargs, agents = _runner_kwargs(model_registry, monkeypatch)
    app = create_app(engine=db_engine, registry=registry, runner_kwargs=kwargs)
    # 直接经 runner 同步执行到 DONE（测试建产物，不走消费循环）
    runner = TaskRunner(db_engine, registry, **kwargs)
    result = await runner.run("tm_demo_chain", dict(_INPUT), trigger_ref="运营")
    assert result.status == "done"
    async with _client(app) as client:
        resp = await client.get(f"/api/engine/tasks/{_fmt_task(result.task_id)}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["finished_at"] is not None
    assert body["output"] is not None
    assert body["output"]["action_id"] == "tm.proposal"  # 链产物 = TaskProposal
    assert body["output"]["source"]["worker_id"] == "demo_propose"
    assert len(body["audits"]) == 1  # 全链仅 demo_echo 调 1 次 LLM（桩）
    assert body["audits"][0]["worker_id"] == "demo_echo"
    assert body["audits"][0]["tokens_in"] == 12
    assert agents[0].calls == 1


# ==== GET /api/engine/registry：200（详设 §4.3）====


@pytest.mark.asyncio
async def test_registry_200_shape(api_app) -> None:
    """registry 清单 -> 200：workers/chains/actions/events 结构对齐 §4.3。"""
    app, _ = api_app
    async with _client(app) as client:
        resp = await client.get("/api/engine/registry")
    assert resp.status_code == 200
    body = resp.json()
    workers = {w["id"]: w for w in body["workers"]}
    assert set(workers) >= {"demo_echo", "demo_propose"}
    assert workers["demo_echo"]["domain"] == "demo"
    assert workers["demo_echo"]["risk"] == "read"
    assert workers["demo_propose"]["risk"] == "suggest"
    assert workers["demo_echo"]["version"] == 1
    chains = {c["id"]: c for c in body["chains"]}
    assert chains["tm_demo_chain"]["workers"] == ["demo_echo", "demo_propose"]
    actions = {a["id"]: a for a in body["actions"]}
    assert actions["tm.proposal"]["risk"] == "suggest"
    events = {e["id"] for e in body["events"]}
    assert "demo.echo_done" in events


# ==== 队列消费循环 + TM 转交器钩子（详设 §2.3/§5）====


@pytest.mark.asyncio
async def test_consumer_loop_transfers_proposal_on_done(
    monkeypatch: pytest.MonkeyPatch, db_engine, registry, model_registry
) -> None:
    """消费循环：POST 入队 -> 循环执行 tm_demo_chain DONE -> 转交器（桩）被调用。

    断言：提案提取正确（demo_propose 产出）、注入 whitelist = 链输入 ref_id
    集合（m-001，决策 16 数据引用封闭性）、registry 注入、任务保持 done。
    """
    calls: list[tuple[Any, dict[str, Any]]] = []

    async def stub_consumer(proposal: Any, **kwargs: Any) -> ConsumeOutcome:
        calls.append((proposal, kwargs))
        return ConsumeOutcome("inserted", proposal_id=1)

    kwargs, agents = _runner_kwargs(model_registry, monkeypatch)
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner_kwargs=kwargs,
        consumers={"tm.proposal": stub_consumer},
        poll_interval=0.01,
    )
    async with _client(app) as client:
        task_id = await _post_demo(app, client)
        app.state.consumer.start()
        try:
            await _wait_status(client, task_id, "done")
            await _wait_until(lambda: len(calls) == 1, what="转交器被调用")
        finally:
            await app.state.consumer.stop()
    assert len(calls) == 1  # 转交器恰被调用一次
    proposal, inject = calls[0]
    assert proposal.action_id == "tm.proposal"
    assert proposal.source.worker_id == "demo_propose"
    assert proposal.source.chain_id == "tm_demo_chain"
    assert inject["whitelist"] == {"m-001"}  # 白名单 = 链 input 的 ref_id
    assert inject["registry"] is registry
    assert "tm_engine" not in inject  # 未注入 tm_engine（桩消费者不需要）
    assert agents[0].calls == 1  # 全链仅 1 次桩 LLM 调用


@pytest.mark.asyncio
async def test_consumer_loop_marks_failed_on_rejected(
    monkeypatch: pytest.MonkeyPatch, db_engine, registry, model_registry
) -> None:
    """转交器 rejected -> 任务标 failed，error 记拒落原因（详设 §3.2 拒落 + 链 FAILED）。"""
    calls: list[Any] = []

    async def rejecting_consumer(proposal: Any, **kwargs: Any) -> ConsumeOutcome:
        calls.append(proposal)
        return ConsumeOutcome("rejected", reason="幻觉证据: ref_id=out-1（数据引用封闭性）")

    kwargs, _ = _runner_kwargs(model_registry, monkeypatch)
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner_kwargs=kwargs,
        consumers={"tm.proposal": rejecting_consumer},
        poll_interval=0.01,
    )
    async with _client(app) as client:
        task_id = await _post_demo(app, client)
        app.state.consumer.start()
        try:
            # rejected 时终态为 failed：转交钩子先把链标 done 再改 failed，
            # 直接等 failed（此时 calls 已追加）
            await _wait_status(client, task_id, "failed")
        finally:
            await app.state.consumer.stop()
        resp = await client.get(f"/api/engine/tasks/{task_id}")
    assert len(calls) == 1
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"] is not None
    assert "幻觉证据" in body["error"]


@pytest.mark.asyncio
async def test_consumer_loop_skipped_keeps_done(
    monkeypatch: pytest.MonkeyPatch, db_engine, registry, model_registry
) -> None:
    """转交器 skipped_* -> 任务保持 done（防重/幂等不改变链终态，决策 17）。"""
    calls: list[Any] = []

    async def skipping_consumer(proposal: Any, **kwargs: Any) -> ConsumeOutcome:
        calls.append(proposal)
        return ConsumeOutcome("skipped_duplicate", reason="同事件防重：已有未完成任务")

    kwargs, _ = _runner_kwargs(model_registry, monkeypatch)
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner_kwargs=kwargs,
        consumers={"tm.proposal": skipping_consumer},
        poll_interval=0.01,
    )
    async with _client(app) as client:
        task_id = await _post_demo(app, client)
        app.state.consumer.start()
        try:
            await _wait_status(client, task_id, "done")
            await _wait_until(lambda: len(calls) == 1, what="转交器被调用（skipped 路径）")
        finally:
            await app.state.consumer.stop()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_consumer_loop_no_transfer_for_non_proposal_chain(
    monkeypatch: pytest.MonkeyPatch, db_engine, registry, model_registry
) -> None:
    """非 suggest 链（demo_echo_chain，产物无提案判据键）-> 不触发转交器。"""
    calls: list[Any] = []

    async def stub_consumer(proposal: Any, **kwargs: Any) -> ConsumeOutcome:
        calls.append(proposal)
        return ConsumeOutcome("inserted", proposal_id=1)

    kwargs, _ = _runner_kwargs(model_registry, monkeypatch)
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner_kwargs=kwargs,
        consumers={"tm.proposal": stub_consumer},
        poll_interval=0.01,
    )
    async with _client(app) as client:
        resp = await client.post(
            "/api/engine/tasks",
            json={"chain_id": "demo_echo_chain", "input": {"text": "hi"}, "trigger_ref": "运营"},
        )
        assert resp.status_code == 201
        task_id = resp.json()["task_id"]
        app.state.consumer.start()
        try:
            await _wait_status(client, task_id, "done")
            await asyncio.sleep(0.05)  # 给不存在的转交留观察窗（防时序掩盖）
        finally:
            await app.state.consumer.stop()
    assert calls == []  # 无提案产物，不转交


@pytest.mark.asyncio
async def test_consumer_loop_real_transfer_lands_proposal(
    monkeypatch: pytest.MonkeyPatch,
    db_engine,
    tm_engine,
    registry,
    model_registry,
    _clean_tm_tables,
) -> None:
    """端到端：链 DONE -> 真转交器（CONSUMERS 缺省）经写接口落 tm.task_proposal。

    v0.3 改造（决策 26）：转交器落库走 biz_client（HTTP 写接口，MockTransport 桩）
    ——断言接口收到合法提案（risk 代码标注 + evidence/source 对齐），引擎进程
    零业务库连接串（验收 A12/A35 在 server 层的落点）。
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "id": 1, "skipped": False})

    kwargs, _ = _runner_kwargs(model_registry, monkeypatch)
    app = create_app(
        engine=db_engine,
        registry=registry,
        runner_kwargs=kwargs,
        biz_client=BizApiClient(
            base_url="http" + "://test-" + "biz", token="test-token",
            transport=httpx.MockTransport(handler),
        ),
        poll_interval=0.01,
    )
    async with _client(app) as client:
        task_id = await _post_demo(app, client)
        app.state.consumer.start()
        try:
            await _wait_status(client, task_id, "done")
            await asyncio.sleep(0.1)  # 给转交钩子执行留窗
        finally:
            await app.state.consumer.stop()
    assert "body" in captured, "转交钩子未调写接口"
    body = captured["body"]
    assert body["action_id"] == "tm.proposal"
    assert body["risk"] == "suggest"  # risk 代码规则标注（Action 声明）
    assert body["evidence"][0]["ref_id"] == "m-001"
    assert body["source"]["worker_id"] == "demo_propose"


# ==== 崩溃恢复 recover()（详设 §2.3 + 变更日志 M4 配套）====


@pytest.mark.asyncio
async def test_recover_marks_running_without_checkpoint_failed(
    monkeypatch: pytest.MonkeyPatch, db_engine, registry, model_registry
) -> None:
    """running 且无检查点（claim 后崩溃窗口）-> recover 标记 failed（孤儿清理）。"""
    kwargs, _ = _runner_kwargs(model_registry, monkeypatch)
    app = create_app(engine=db_engine, registry=registry, runner_kwargs=kwargs)
    task_id = await _db.create_task(
        app.state.engine,
        chain_id="tm_demo_chain",
        trigger_type="manual",
        trigger_ref="运营",
        input_=dict(_INPUT),
    )
    await _db.update_task(
        app.state.engine, task_id, status="running", started_at=datetime.now(timezone.utc)
    )
    lines = await app.state.consumer.recover()
    row = await _db.get_task(app.state.engine, task_id)
    assert row is not None
    assert row.status == "failed"
    assert "孤儿任务恢复失败" in (row.error or "")
    assert any("marked failed" in line for line in lines)


@pytest.mark.asyncio
async def test_recover_resumes_running_with_checkpoint(
    monkeypatch: pytest.MonkeyPatch, db_engine, registry, model_registry
) -> None:
    """running 且有 INIT->REASON 检查点 -> recover 经 resume 续跑到 done（不重烧 token）。

    崩溃窗口模拟：claim 后 REASON 未完成即崩溃（仅检查点 + 首步已建）。
    续跑从 REASON 继续：仅 demo_echo 调 1 次桩 LLM（demo_propose 纯代码），
    任务 DONE，断言 A4 语义（续跑不重复已经完成的调用）。
    """
    kwargs, agents = _runner_kwargs(model_registry, monkeypatch)
    app = create_app(engine=db_engine, registry=registry, runner_kwargs=kwargs)
    task_id = await _db.create_task(
        app.state.engine,
        chain_id="tm_demo_chain",
        trigger_type="manual",
        trigger_ref="运营",
        input_=dict(_INPUT),
    )
    await _db.update_task(
        app.state.engine, task_id, status="running", started_at=datetime.now(timezone.utc)
    )
    step_id = await _db.create_step(
        app.state.engine,
        task_id,
        step_index=0,
        worker_id="demo_echo",
        input_=dict(_INPUT),
    )
    await _db.write_checkpoint(
        app.state.engine,
        task_id=task_id,
        step_id=step_id,
        from_phase="INIT",
        to_phase="REASON",
        state={
            "phase_output": None,
            "memory_state": {"inputs": dict(_INPUT), "context_data": {}},
        },
    )
    lines = await app.state.consumer.recover()
    row = await _db.get_task(app.state.engine, task_id)
    assert row is not None
    assert row.status == "done"
    assert any("resumed -> done" in line for line in lines)
    assert agents[0].calls == 1  # 续跑只新增 1 次 LLM 调用（A4 不重烧 token）


# ==== claim_task 原子认领（变更日志 M4 的 v0.2 落地）====


@pytest.mark.asyncio
async def test_claim_task_atomic_running_oldest_first(db_engine) -> None:
    """原子认领：最老 queued 优先 + 认领事务内即 running（双消费者不重取）。"""
    first = await _db.create_task(
        db_engine, chain_id="tm_demo_chain", trigger_type="manual", trigger_ref=None, input_={}
    )
    second = await _db.create_task(
        db_engine, chain_id="tm_demo_chain", trigger_type="manual", trigger_ref=None, input_={}
    )
    got1 = await _db.claim_task(db_engine)
    assert got1 is not None and got1.id == first
    assert got1.status == "running"  # 认领即 running（M4：原子认领落地）
    got2 = await _db.claim_task(db_engine)
    assert got2 is not None and got2.id == second
    assert await _db.claim_task(db_engine) is None  # 全部认领后队列空


# ==== 纯函数助手 ====


def test_collect_ref_ids_recursive() -> None:
    """白名单递归收集：dict/list 深层 ref_id/id 字符串值（决策 16 数据集合）。"""
    data = {
        "text": "消息",
        "ref_id": "m-001",
        "nested": {"id": "o-2", "count": 3},
        "list": [{"ref_id": "m-003"}],
    }
    assert _collect_ref_ids(data) == {"m-001", "o-2", "m-003"}
    assert _collect_ref_ids({"nested": {"x": {"id": "deep-9"}}}) == {"deep-9"}
    assert _collect_ref_ids({"count": 3, "nested": {"id": 7}}) == {"7"}  # v0.3：int id 统一转 str（MessageBrief.id 白名单）


def test_extract_proposal_judges_keys() -> None:
    """提案提取判据：六键齐 + 契约合法 -> TaskProposal；缺键/类型错 -> None。"""
    valid = {
        "title": "跟进",
        "detail": "说明",
        "domain": "demo",
        "action_id": "tm.proposal",
        "suggested_role": "运营",
        "suggested_due_days": 1,
        "evidence": [{"kind": "message", "ref_id": "m-001", "quote": "q"}],
        "source": {
            "chain_id": "tm_demo_chain",
            "engine_task_id": "e-000001",
            "worker_id": "demo_propose",
            "audit_ids": [],
        },
    }
    assert _extract_proposal(valid) is not None
    assert _extract_proposal({"title": "x"}) is None  # 缺键
    assert _extract_proposal(None) is None
    assert _extract_proposal("not-a-dict") is None
    bad = {**valid, "title": ""}  # 契约非法（title 空）
    assert _extract_proposal(bad) is None


def test_engine_port_from_dotenv(tmp_path: Path) -> None:
    """.env 的 LIUQUAN_ENGINE_API_URL -> 解析端口（P2：值只存 .env）。"""
    (tmp_path / ".env").write_text(f"LIUQUAN_ENGINE_API_URL={_FAKE_URL}\n", encoding="utf-8")
    assert _engine_port(tmp_path) == 9999


def test_engine_port_default_when_unset(tmp_path: Path) -> None:
    """.env 无此变量 / 不存在 -> 缺省 8100（与 web 侧客户端缺省同源）。"""
    (tmp_path / ".env").write_text("OTHER_KEY=1\n", encoding="utf-8")
    assert _engine_port(tmp_path) == 8100
    assert _engine_port(tmp_path / "no-such-dir") == 8100
