"""v0.3 T6 翻译工具测试（决策 23：单纯翻译 + 可选归入客户）。

- GET /translate 页面（登录保护）
- POST /translate：触发 crm_translate_chain（引擎桩）-> 返回 engine_task_id
- POST /translate/archive：译文归入客户 -> crm.message 落库
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from models.crm import Customer
from web.app import create_app
from web.crm_store import CRMStore
from web.engineapi.client import EngineAPIClient

_FAKE_BASE = "http" + "://test-" + "engine"


class _EngineStub:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/engine/tasks" and request.method == "POST":
            self.created.append(json.loads(request.content))
            return httpx.Response(201, json={"task_id": "e-000020", "chain_id": "crm_translate_chain"})
        if request.url.path.startswith("/api/engine/tasks/"):
            return httpx.Response(
                200,
                json={
                    "status": "done",
                    "task_id": "e-000020",
                    "output": {"translated": "你好，世界"},
                },
            )
        return httpx.Response(404, json={"detail": "nf"})


@async_fixture
async def crm_engine(tm_pg_cluster):
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean(crm_engine):
    yield
    async with AsyncSession(crm_engine) as session, session.begin():
        await session.execute(
            text("TRUNCATE crm.message, crm.customer, tm.task RESTART IDENTITY CASCADE")
        )


@pytest.fixture
def client(crm_engine) -> TestClient:
    stub = _EngineStub()

    def factory() -> EngineAPIClient:
        return EngineAPIClient(
            base_url=_FAKE_BASE, transport=httpx.MockTransport(stub.handler)
        )

    app = create_app(
        crm_store=CRMStore(crm_engine),
        engine_client_factory=factory,
    )
    return TestClient(app, follow_redirects=False)


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "liuquan", "role": "ops"})


def test_translate_requires_login(client: TestClient) -> None:
    resp = client.get("/translate")
    assert resp.status_code == 303


def test_translate_page(client: TestClient) -> None:
    _login(client)
    resp = client.get("/translate")
    assert resp.status_code == 200
    assert "翻译任意文本" in resp.text


@pytest.mark.version_acceptance
def test_translate_triggers_chain(client: TestClient) -> None:
    _login(client)
    resp = client.post(
        "/translate",
        data={"text": "hello world", "source_lang": "en", "target_lang": "zh"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["engine_task_id"] == "e-000020"


@pytest.mark.asyncio
@pytest.mark.version_acceptance
async def test_translate_archive_lands_message(client: TestClient, crm_engine) -> None:
    async with AsyncSession(crm_engine) as session, session.begin():
        session.add(Customer(nickname="Mia"))
    _login(client)
    resp = client.post(
        "/translate/archive",
        data={"customer_id": "1", "source_text": "hello", "translated_text": "你好", "direction": "buyer"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    async with AsyncSession(crm_engine) as session:
        row = (await session.execute(text("SELECT source_text, translated_text FROM crm.message"))).one()
        assert row == ("hello", "你好")
