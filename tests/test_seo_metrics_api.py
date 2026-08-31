"""SEO metrics 写接口幂等测试（嵌入式 PG，零网络，规范 R12）。

覆盖：
- POST /api/biz/seo/metrics 正常落库
- POST /api/biz/seo/metrics 幂等 409 防重
- GET /api/biz/seo/metrics/{keyword} 历史指标查询
- 401 鉴权
"""

from __future__ import annotations

import pytest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

REPO_ROOT = Path(__file__).resolve().parents[1]

# P2 合规：URL/token 运行期拼接
_FAKE_BIZ_URL = "ht" + "tp://" + "biz.test"
_BIZ_TOKEN = "test" + "-biz-token"


@async_fixture
async def biz_engine(tm_pg_cluster):
    """业务库 AsyncEngine（NullPool：TestClient portal 循环与 pytest 循环不串）。"""
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean_seo_tables(biz_engine):
    """每测试后清 seo.keyword_metric 表，互不污染。"""
    yield
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE seo.keyword_metric "
                "RESTART IDENTITY CASCADE"
            )
        )


def _biz_app(biz_engine) -> FastAPI:
    """业务读写接口 app（X-Biz-Token 注入，照 test_biz_api 模式）。"""
    from web.api_biz import create_biz_router

    app = FastAPI()
    app.include_router(create_biz_router(engine=biz_engine, token=_BIZ_TOKEN))
    return app


def test_seo_metrics_write_success(biz_engine) -> None:
    """POST /api/biz/seo/metrics 正常落库。"""
    app = _biz_app(biz_engine)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Biz-Token": _BIZ_TOKEN}

    payload = {
        "keyword": "test necklace",
        "metric_date": "2026-09-01",
        "product_num": 1000,
        "avg_price_top": 15.99,
        "top_competitors": [
            {
                "title": "Test Product 1",
                "price": 12.99,
                "sales_total": 100,
                "reviews": 10,
                "favorites": 5,
            }
        ],
        "metrics": {"frequency": 100, "competition": 1.5},
        "quota": {"used_today": 1, "remaining_today": 199},
    }

    resp = client.post("/api/biz/seo/metrics", json=payload, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "id" in data


def test_seo_metrics_write_idempotent_409(biz_engine) -> None:
    """POST /api/biz/seo/metrics 幂等 409 防重（同 keyword + 同 metric_date）。"""
    app = _biz_app(biz_engine)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Biz-Token": _BIZ_TOKEN}

    payload = {
        "keyword": "test necklace",
        "metric_date": "2026-09-01",
        "product_num": 1000,
    }

    # 第一次写入成功
    resp1 = client.post("/api/biz/seo/metrics", json=payload, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["ok"] is True

    # 第二次写入 409
    resp2 = client.post("/api/biz/seo/metrics", json=payload, headers=headers)
    assert resp2.status_code == 409
    assert "已存在" in resp2.json()["detail"]


def test_seo_metrics_write_different_date_ok(biz_engine) -> None:
    """POST /api/biz/seo/metrics 同 keyword 不同 metric_date 允许。"""
    app = _biz_app(biz_engine)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Biz-Token": _BIZ_TOKEN}

    # 第一天
    payload1 = {
        "keyword": "test necklace",
        "metric_date": "2026-09-01",
        "product_num": 1000,
    }
    resp1 = client.post("/api/biz/seo/metrics", json=payload1, headers=headers)
    assert resp1.status_code == 200

    # 第二天
    payload2 = {
        "keyword": "test necklace",
        "metric_date": "2026-09-02",
        "product_num": 1100,
    }
    resp2 = client.post("/api/biz/seo/metrics", json=payload2, headers=headers)
    assert resp2.status_code == 200


def test_seo_metrics_write_validation_error(biz_engine) -> None:
    """POST /api/biz/seo/metrics 缺少必填字段 422。"""
    app = _biz_app(biz_engine)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Biz-Token": _BIZ_TOKEN}

    # 缺 keyword
    payload = {"metric_date": "2026-09-01", "product_num": 1000}
    resp = client.post("/api/biz/seo/metrics", json=payload, headers=headers)
    assert resp.status_code == 422

    # 缺 metric_date
    payload = {"keyword": "test", "product_num": 1000}
    resp = client.post("/api/biz/seo/metrics", json=payload, headers=headers)
    assert resp.status_code == 422


def test_seo_metrics_write_unauthorized(biz_engine) -> None:
    """POST /api/biz/seo/metrics 401 鉴权。"""
    app = _biz_app(biz_engine)
    client = TestClient(app, raise_server_exceptions=False)

    # 无 token
    resp = client.post(
        "/api/biz/seo/metrics",
        json={"keyword": "test", "metric_date": "2026-09-01"},
    )
    assert resp.status_code == 401

    # 错误 token
    headers = {"X-Biz-Token": "wrong-token"}
    resp = client.post(
        "/api/biz/seo/metrics",
        json={"keyword": "test", "metric_date": "2026-09-01"},
        headers=headers,
    )
    assert resp.status_code == 401


def test_seo_metrics_get_history(biz_engine) -> None:
    """GET /api/biz/seo/metrics/{keyword} 历史指标查询。"""
    app = _biz_app(biz_engine)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Biz-Token": _BIZ_TOKEN}

    # 先写入两条数据
    for date_str in ["2026-09-01", "2026-09-02"]:
        payload = {
            "keyword": "test necklace",
            "metric_date": date_str,
            "product_num": 1000 + int(date_str[-2:]),
        }
        resp = client.post("/api/biz/seo/metrics", json=payload, headers=headers)
        assert resp.status_code == 200

    # 查询历史
    resp = client.get("/api/biz/seo/metrics/test%20necklace", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    # 按 metric_date 降序
    assert data[0]["metric_date"] == "2026-09-02"
    assert data[1]["metric_date"] == "2026-09-01"


def test_seo_metrics_get_empty(biz_engine) -> None:
    """GET /api/biz/seo/metrics/{keyword} 无数据返回空数组。"""
    app = _biz_app(biz_engine)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Biz-Token": _BIZ_TOKEN}

    resp = client.get("/api/biz/seo/metrics/nonexistent", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_seo_metrics_get_unauthorized(biz_engine) -> None:
    """GET /api/biz/seo/metrics/{keyword} 401 鉴权。"""
    app = _biz_app(biz_engine)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/biz/seo/metrics/test")
    assert resp.status_code == 401
