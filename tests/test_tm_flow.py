"""v0.3 T8 任务「下一步」区测试（决策 25/27/28；验收 A33/A34 落点）。

- A33 标签：AI 建议标签随确认落库（test_web_crm 已覆盖）+ next tag 改标签 +
  列表展示标签徽章
- A34 流转：next assign/tag/note/block/ignore + transferred 事件；自然语言入口
  （next-intent 触发 tm_intent_chain 引擎桩 + next-confirm 执行）；transfer 未接入
  域（erp/seo）明确提示不可执行 + disagreed 事件留痕；忽略建议清 ai_suggestion
"""

from __future__ import annotations

from datetime import date
from urllib.parse import unquote_plus

import httpx
import pytest
from fastapi.testclient import TestClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from models.tm import Task, TaskEvent
from web.app import create_app
from web.engineapi.client import EngineAPIClient
from web.tm_store import TMStore

_FAKE_BASE = "http" + "://test-" + "engine"


class _EngineStub:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/engine/tasks" and request.method == "POST":
            import json as _json

            self.created.append(_json.loads(request.content))
            return httpx.Response(201, json={"task_id": "e-000030", "chain_id": "tm_intent_chain"})
        if request.url.path.startswith("/api/engine/tasks/"):
            return httpx.Response(
                200,
                json={
                    "status": "done",
                    "task_id": "e-000030",
                    "output": {
                        "action": "assign",
                        "target_role": "采购",
                        "clarity": "clear",
                        "note": "交给采购处理",
                    },
                },
            )
        return httpx.Response(404, json={"detail": "nf"})


@async_fixture
async def tm_engine(tm_pg_cluster):
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean(tm_engine):
    """每测试前 TRUNCATE（文件间独立：前序文件如 test_engine_server 端到端落库
    未清，测试前清理保证本文件断言不受残留影响）。"""
    async with AsyncSession(tm_engine) as session, session.begin():
        await session.execute(
            text("TRUNCATE tm.task_event, tm.task_proposal, tm.task RESTART IDENTITY CASCADE")
        )
    yield


@pytest.fixture
def store(tm_engine) -> TMStore:
    return TMStore(tm_engine)


@pytest.fixture
def client(store: TMStore) -> TestClient:
    stub = _EngineStub()

    def factory() -> EngineAPIClient:
        return EngineAPIClient(
            base_url=_FAKE_BASE, transport=httpx.MockTransport(stub.handler)
        )

    app = create_app(tm_store=store, engine_client_factory=factory)
    app.state.engine_stub = stub
    return TestClient(app, follow_redirects=False)


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "liuquan", "role": "ops"})


@async_fixture
async def _seed_task(tm_engine) -> int:
    async with AsyncSession(tm_engine) as session, session.begin():
        task = Task(
            title="确认花材组合",
            detail="",
            domain="crm",
            role="运营",
            due=date(2026, 8, 31),
            source_type="ai",
            source={"customer_id": 1},
            tags=["报价"],
            ai_suggestion={"action": "transfer", "target_domain": "erp", "note": "建议流转到 ERP"},
            created_by="运营",
        )
        session.add(task)
        await session.flush()
        session.add(TaskEvent(task_id=task.id, event_type="suggested", actor="运营"))
        return task.id


# ---- A34 流转：改派 / 标签 / 挂起 / 忽略 ----


@pytest.mark.asyncio
async def test_next_assign_changes_role(client: TestClient, _seed_task, tm_engine) -> None:
    _login(client)
    resp = client.post(
        f"/tasks/{_seed_task}/next",
        data={"action": "assign", "target_role": "采购"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with AsyncSession(tm_engine) as session:
        task = await session.get(Task, _seed_task)
        assert task.role == "采购"
        assert task.ai_suggestion is None  # 执行建议后清空
        events = (
            (await session.execute(text("SELECT event_type FROM tm.task_event ORDER BY id")))
            .scalars()
            .all()
        )
        assert list(events) == ["suggested", "transferred"]  # 流转留痕（决策 27）


@pytest.mark.asyncio
async def test_next_tag_updates_tags(client: TestClient, _seed_task, tm_engine) -> None:
    _login(client)
    client.post(f"/tasks/{_seed_task}/next", data={"action": "tag", "tags": "售后,物流"}, follow_redirects=False)
    async with AsyncSession(tm_engine) as session:
        task = await session.get(Task, _seed_task)
        assert task.tags == ["售后", "物流"]  # A33：改标签


@pytest.mark.asyncio
async def test_next_block_requires_valid_reason(client: TestClient, _seed_task, tm_engine) -> None:
    _login(client)
    resp = client.post(
        f"/tasks/{_seed_task}/next",
        data={"action": "block", "note": "非法原因"},
        follow_redirects=False,
    )
    assert "blocked_reason" in unquote_plus(resp.headers["location"])
    resp = client.post(
        f"/tasks/{_seed_task}/next",
        data={"action": "block", "note": "等物料"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with AsyncSession(tm_engine) as session:
        task = await session.get(Task, _seed_task)
        assert task.status == "blocked"
        assert task.blocked_reason == "等物料"


@pytest.mark.asyncio
async def test_next_ignore_clears_suggestion(
    client: TestClient, _seed_task, tm_engine
) -> None:
    _login(client)
    client.post(f"/tasks/{_seed_task}/next", data={"action": "ignore"}, follow_redirects=False)
    async with AsyncSession(tm_engine) as session:
        task = await session.get(Task, _seed_task)
        assert task.ai_suggestion is None
        events = (
            (await session.execute(text("SELECT event_type FROM tm.task_event ORDER BY id")))
            .scalars()
            .all()
        )
        assert list(events) == ["suggested"]  # ignore 不落 transferred


# ---- A34 自然语言入口（决策 28）----


def test_next_intent_triggers_chain(client: TestClient, _seed_task) -> None:
    _login(client)
    resp = client.post(
        f"/tasks/{_seed_task}/next-intent",
        data={"instruction": "流转到 ERP 库存"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["engine_task_id"] == "e-000030"
    stub = client.app.state.engine_stub
    assert stub.created[-1]["chain_id"] == "tm_intent_chain"
    assert stub.created[-1]["input"]["task_id"] == _seed_task


@pytest.mark.asyncio
async def test_next_confirm_transfer_unavailable(
    client: TestClient, _seed_task, tm_engine
) -> None:
    """决策 28：transfer 目标域未接入（erp）明确提示不可执行并记录意图。"""
    _login(client)
    resp = client.post(
        f"/tasks/{_seed_task}/next-confirm",
        data={"action": "transfer", "clarity": "clear", "target_domain": "erp"},
        follow_redirects=False,
    )
    assert "暂不可执行" in unquote_plus(resp.headers["location"])
    async with AsyncSession(tm_engine) as session:
        events = (
            (await session.execute(text("SELECT event_type FROM tm.task_event ORDER BY id")))
            .scalars()
            .all()
        )
        assert list(events) == ["suggested", "disagreed"]  # 分歧留痕（决策 27）


@pytest.mark.asyncio
async def test_next_confirm_assign_executes(
    client: TestClient, _seed_task, tm_engine
) -> None:
    """自然语言解析 -> 确认执行（assign 改派采购）。"""
    _login(client)
    resp = client.post(
        f"/tasks/{_seed_task}/next-confirm",
        data={"action": "assign", "clarity": "clear", "target_role": "采购"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with AsyncSession(tm_engine) as session:
        task = await session.get(Task, _seed_task)
        assert task.role == "采购"


# ---- A33 标签展示 ----

@pytest.mark.asyncio
async def test_task_list_shows_tags(client: TestClient, _seed_task) -> None:
    _login(client)
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "报价" in resp.text  # 标签徽章展示
