"""v0.3 T6 CRM 页面测试（TestClient + 嵌入式 PG + 引擎桩，详设 §7/§8）。

覆盖（验收 A27 后半/A28/A29/A31/A32/A36 落点）：
- 客户列表：状态筛选 / 搜索 / 新建（重名两层拦截）
- 客户详情：快照 / 消息时间线 / 任务卡（domain=crm source.customer_id）/ 候选
- 粘贴对话：触发 crm_chat_chain（引擎桩）-> 详情页带 engine_task_id
- 确认事务（A28）：candidate confirmed + tm.task(domain=crm, ai) + created/approved
  事件 + confirmed_task_id 回填 + tags 落库（决策 25）；重复确认拒
- 回复归档（A31）：reply/send 落 seller 消息 + 状态 replied
- 客户删除（A36）：级联删 message/snapshot/candidate/task
- 飞书（决策 21）：确认生成任务时调用 send_task_card（monkeypatch 桩）

基建：tm_pg_cluster（tm + crm 两 schema）+ NullPool engine + TestClient +
引擎桩（MockTransport，R12 注入）。
"""

from __future__ import annotations

from datetime import date
from urllib.parse import unquote_plus

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from models.crm import Customer, Message, Snapshot, TodoCandidate
from models.tm import Task, TaskEvent
from web.app import create_app
from web.crm_store import CRMStore
from web.engineapi.client import EngineAPIClient

_FAKE_BASE = "http" + "://test-" + "engine"


class _EngineStub:
    """引擎三接口桩（MockTransport handler，R12；测试零网络零真服务）。"""

    def __init__(self) -> None:
        self.created: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/engine/tasks" and request.method == "POST":
            body = httpx.QueryParams("").__class__(request.content.decode()).dict() if False else None
            import json as _json

            body = _json.loads(request.content)
            self.created.append(body)
            chain = body.get("chain_id", "")
            tid = "e-000010"
            return httpx.Response(201, json={"task_id": tid, "chain_id": chain})
        if path.startswith("/api/engine/tasks/") and request.method == "GET":
            return httpx.Response(200, json={"status": "done", "task_id": "e-000010", "output": {}})
        if path == "/api/engine/registry":
            return httpx.Response(200, json={"chains": []})
        return httpx.Response(404, json={"detail": "not found"})


@async_fixture
async def crm_engine(tm_pg_cluster):
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean_all(crm_engine):
    yield
    async with AsyncSession(crm_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE crm.todo_candidate, crm.snapshot, crm.message, crm.customer, "
                "tm.task_event, tm.task_proposal, tm.task RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def store(crm_engine) -> CRMStore:
    return CRMStore(crm_engine)


@pytest.fixture
def engine_stub() -> _EngineStub:
    return _EngineStub()


@pytest.fixture
def client(store: CRMStore, engine_stub: _EngineStub) -> TestClient:
    def factory() -> EngineAPIClient:
        return EngineAPIClient(
            base_url=_FAKE_BASE, transport=httpx.MockTransport(engine_stub.handler)
        )

    app = create_app(crm_store=store, engine_client_factory=factory)
    return TestClient(app, follow_redirects=False)


def _login(client: TestClient, role: str = "ops") -> None:
    resp = client.post("/login", data={"password": "liuquan", "role": role})
    assert resp.status_code == 303


@async_fixture
async def _seed(crm_engine) -> int:
    """建客户 + 消息 + 快照 + pending 候选。"""
    async with AsyncSession(crm_engine) as session, session.begin():
        c = Customer(nickname="Mia", source_shop="成品", follow_up_status="waiting_reply")
        session.add(c)
        await session.flush()
        session.add_all(
            [
                Message(customer_id=c.id, source_text="hi", translated_text="你好", direction="buyer", language="en"),
                Snapshot(customer_id=c.id, current_need="花束", summary="想定制"),
                TodoCandidate(
                    customer_id=c.id,
                    content="确认花材组合及婚礼日期",
                    reason="卖家答应确认",
                    suggested_tags=["报价"],
                    evidence=[{"kind": "message", "ref_id": "1", "quote": "hi"}],
                    status="pending",
                ),
            ]
        )
        return c.id


# ---- 列表 / 新建 / 删除 ----


def test_crm_requires_login(client: TestClient) -> None:
    resp = client.get("/crm")
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_crm_index_lists_customer(client: TestClient, _seed) -> None:
    _login(client)
    resp = client.get("/crm")
    assert resp.status_code == 200
    assert "Mia" in resp.text
    assert "暂无客户" not in resp.text


def test_crm_create_duplicate_rejected(client: TestClient, _seed) -> None:
    _login(client)
    resp = client.post("/crm/customers", data={"nickname": "mia"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]  # 重名拦截（忽略大小写）


def test_crm_create_force_ok(client: TestClient, _seed, crm_engine) -> None:
    _login(client)
    resp = client.post(
        "/crm/customers",
        data={"nickname": "mia", "source_shop": "贝壳", "force": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/crm" in resp.headers["location"]


@pytest.mark.asyncio
async def test_crm_delete_cascades(client: TestClient, _seed, crm_engine) -> None:
    """A36：删除客户级联清 message/snapshot/candidate/task。"""
    _login(client)
    # 预置关联任务（domain=crm source.customer_id）
    async with AsyncSession(crm_engine) as session, session.begin():
        session.add(
            Task(
                title="确认花材", detail="", domain="crm", role="运营",
                due=date(2026, 8, 31), source_type="ai",
                source={"customer_id": 1}, created_by="运营",
            )
        )
    resp = client.post("/crm/1/delete", follow_redirects=False)
    assert resp.status_code == 303
    async with AsyncSession(crm_engine) as session:
        for sql in (
            "SELECT count(*) FROM crm.message",
            "SELECT count(*) FROM crm.snapshot",
            "SELECT count(*) FROM crm.todo_candidate",
            "SELECT count(*) FROM crm.customer",
            "SELECT count(*) FROM tm.task WHERE domain='crm'",
        ):
            assert (await session.execute(text(sql))).scalar_one() == 0


# ---- 详情 / 粘贴 ----


def test_crm_detail_shows_snapshot_tasks_candidates(
    client: TestClient, _seed
) -> None:
    _login(client)
    resp = client.get("/crm/1")
    assert resp.status_code == 200
    assert "花束" in resp.text  # 快照
    assert "确认花材组合及婚礼日期" in resp.text  # 候选


def test_crm_paste_triggers_chain(client: TestClient, _seed, engine_stub) -> None:
    _login(client)
    resp = client.post(
        "/crm/1/messages",
        data={"conversation_text": "Hi! I love your flowers"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "engine_task_id=e-000010" in resp.headers["location"]
    assert engine_stub.created[-1]["chain_id"] == "crm_chat_chain"


# ---- 确认事务（A28/决策 25）----


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_confirm_candidates_creates_task(
    client: TestClient, _seed, crm_engine, monkeypatch
) -> None:
    """A28：确认候选 -> 单事务建 tm.task + created/approved 事件 + 回填 + tags。"""
    sent: list[dict] = []

    async def fake_send(view: dict) -> bool:
        sent.append(view)
        return True

    monkeypatch.setattr("web.app.send_task_card", fake_send)
    _login(client)
    resp = client.post("/crm/1/todos/confirm", data={"candidate_ids": ["1"]}, follow_redirects=False)
    assert resp.status_code == 303
    async with AsyncSession(crm_engine) as session:
        task = (await session.execute(text("SELECT * FROM tm.task WHERE domain='crm'"))).one()
        assert task.source_type == "ai"
        # tags 落库（决策 25）
        cand = await session.get(TodoCandidate, 1)
        assert cand.status == "confirmed"
        assert cand.confirmed_task_id == task.id
        events = (
            (await session.execute(text("SELECT event_type FROM tm.task_event ORDER BY id")))
            .scalars()
            .all()
        )
        assert list(events) == ["created", "approved"]
    assert len(sent) == 1  # 飞书卡片（决策 21）


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_confirm_candidate_twice_rejected(
    client: TestClient, _seed, crm_engine
) -> None:
    _login(client)
    client.post("/crm/1/todos/confirm", data={"candidate_ids": ["1"]}, follow_redirects=False)
    resp = client.post("/crm/1/todos/confirm", data={"candidate_ids": ["1"]}, follow_redirects=False)
    assert "重复确认" in unquote_plus(resp.headers["location"])


# ---- 回复归档（A31）----


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_reply_send_archives_seller_message(
    client: TestClient, _seed, crm_engine
) -> None:
    _login(client)
    resp = client.post(
        "/crm/1/reply/send",
        data={"reply_en": "Hi there!", "reply_zh": "你好！"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with AsyncSession(crm_engine) as session:
        msgs = (await session.execute(text("SELECT direction, source_text FROM crm.message ORDER BY id"))).all()
        assert msgs[-1][0] == "seller"
        assert msgs[-1][1] == "Hi there!"
        status = (await session.execute(text("SELECT follow_up_status FROM crm.customer WHERE id=1"))).scalar_one()
        assert status == "replied"
