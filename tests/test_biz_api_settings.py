"""v0.4 批 1b 业务读写接口 engine-params 测试（嵌入式 PG，零网络，R12 桩注入）。

覆盖（详设 §8 / A44）：
- GET /api/biz/settings/engine-params 鉴权 401
- 表有键 -> 取值
- 表无键 -> 回退默认 {2, 30.0, 30}

基建：tm_pg_cluster（含 sys schema）+ NullPool AsyncEngine + FastAPI TestClient。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from web.api_biz import create_biz_router


_BIZ_TOKEN = "test" + "-biz-" + "token-settings"


@async_fixture
async def biz_engine(tm_pg_cluster):
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean(biz_engine):
    yield
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(text("DELETE FROM sys.settings"))


@pytest.fixture
def client(biz_engine) -> TestClient:
    router = create_biz_router(engine=biz_engine, token=_BIZ_TOKEN)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, follow_redirects=False)


def _headers(token: str = _BIZ_TOKEN) -> dict:
    return {"X-Biz-Token": token}


# ---- 401 鉴权 ----


def test_engine_params_401_no_token(client: TestClient):
    resp = client.get("/api/biz/settings/engine-params")
    assert resp.status_code == 401


def test_engine_params_401_wrong_token(client: TestClient):
    resp = client.get(
        "/api/biz/settings/engine-params",
        headers=_headers("wrong-token"),
    )
    assert resp.status_code == 401


# ---- 表无键 -> 回退默认 ----


def test_engine_params_defaults_when_empty(client: TestClient):
    resp = client.get(
        "/api/biz/settings/engine-params",
        headers=_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["max_attempts"] == 2
    assert data["timeout_s"] == 30.0
    assert data["backoff_cap"] == 30


# ---- 表有键 -> 取值 ----


@pytest.mark.asyncio
async def test_engine_params_reads_from_settings(
    client: TestClient, biz_engine
):
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO sys.settings (key, value, description) "
                "VALUES (:k, :v, :d)"
            ),
            {"k": "engine.max_attempts", "v": "5", "d": "重试次数"},
        )
        await session.execute(
            text(
                "INSERT INTO sys.settings (key, value, description) "
                "VALUES (:k, :v, :d)"
            ),
            {"k": "engine.timeout_s", "v": "60.0", "d": "超时"},
        )
        await session.execute(
            text(
                "INSERT INTO sys.settings (key, value, description) "
                "VALUES (:k, :v, :d)"
            ),
            {"k": "engine.backoff_cap", "v": "45", "d": "退避封顶"},
        )
    resp = client.get(
        "/api/biz/settings/engine-params",
        headers=_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["max_attempts"] == 5
    assert data["timeout_s"] == 60.0
    assert data["backoff_cap"] == 45
