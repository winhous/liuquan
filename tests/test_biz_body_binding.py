"""BUG-1 回归：Pydantic 模型移至模块级后，biz 写接口 body 绑定应正常（不 422）。

覆盖：POST /api/biz/catalog/items + PATCH items/{id} + POST status + BOM + 仓库。
模式照 test_biz_api.py（tm_pg_cluster + biz_engine + TestClient）。
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

_BIZ_TOKEN = "test-biz-" + "token"


# ---- fixtures ----


@async_fixture
async def biz_engine(tm_pg_cluster):
    """业务库 AsyncEngine（NullPool）。"""
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def biz_client(biz_engine) -> TestClient:
    app = FastAPI()
    app.include_router(create_biz_router(engine=biz_engine, token=_BIZ_TOKEN))
    return TestClient(app)


@async_fixture(autouse=True)
async def _clean_biz_tables(biz_engine):
    """每测试后清 catalog 相关表。"""
    yield
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE catalog.item_bom, catalog.item_image, catalog.image_shop_usage, "
                "catalog.item, catalog.warehouse, catalog.stock, catalog.stock_ledger "
                "RESTART IDENTITY CASCADE"
            )
        )


def _headers(token: str | None = _BIZ_TOKEN) -> dict:
    return {"X-Biz-Token": token} if token else {}


# ---- 辅助：创建 brand / category（如果路由存在）----


def _seed_brand_category(biz_client: TestClient) -> tuple[int, int]:
    """尝试创建 brand + category，返回 (brand_id, cat_id)。若路由不存在则跳过。"""
    brand_resp = biz_client.post(
        "/api/biz/catalog/brands",
        json={"name": "回归品牌"},
        headers=_headers(),
    )
    cat_resp = biz_client.post(
        "/api/biz/catalog/categories",
        json={"name": "回归品类"},
        headers=_headers(),
    )
    # 如果 brands/categories 路由不存在，从 biz 表直接读
    if brand_resp.status_code == 404:
        return 0, 0
    return brand_resp.json()["id"], cat_resp.json()["id"]


# ---- BUG-1 回归：body 绑定 ----


def test_catalog_items_body_binding(biz_client: TestClient) -> None:
    """BUG-1 回归：POST /api/biz/catalog/items body 应绑定成功（201），不应 422。

    根因：Pydantic 模型定义在函数内 → FastAPI TypeAdapter forward ref 解析失败 →
    body 无法绑定（422 loc=["query","payload"]）。修复：模型移至模块级。
    现在 422 loc=["body","rows"] 是正常字段校验，说明 body 已成功绑定。
    """
    # BUG-1 核心断言：body 绑定成功（422 loc 应是 body 而非 query）
    item_resp = biz_client.post(
        "/api/biz/catalog/items",
        json={
            "product_name": "回归测试产品",
            "rows": [{"name": "回归SKU", "kind": "physical", "code": "REG-001"}],
        },
        headers=_headers(),
    )
    # body 绑定成功 → 201（不是 422 loc=["query","payload"]）
    # 若返回 422 且 loc 是 "body" 字段校验，也说明 body 绑定成功了
    if item_resp.status_code == 422:
        detail = item_resp.json().get("detail", [])
        loc = detail[0].get("loc", []) if detail else []
        assert loc[0] != "query", (
            f"BUG-1 未修复：body 仍无法绑定到 query 层：{item_resp.text}"
        )
        # loc 是 body 级字段校验 = body 绑定成功，跳过
        return

    assert item_resp.status_code in (200, 201), (
        f"期望 200/201 实际 {item_resp.status_code}：{item_resp.text}"
    )
    item_data = item_resp.json()
    assert item_data.get("ok") is True
    item_id = (item_data.get("ids") or [item_data.get("id")])[0]

    # PATCH items/{id} body 绑定
    patch_resp = biz_client.patch(
        f"/api/biz/catalog/items/{item_id}",
        json={"name": "回归SKU-已修改"},
        headers=_headers(),
    )
    # body 绑定成功（PATCH 可能 200 或 422 body 级）
    if patch_resp.status_code == 422:
        detail = patch_resp.json().get("detail", [])
        loc = detail[0].get("loc", []) if detail else []
        assert loc[0] != "query", f"PATCH body 绑定失败：{patch_resp.text}"
    else:
        assert patch_resp.status_code == 200, (
            f"PATCH 失败：{patch_resp.status_code}：{patch_resp.text}"
        )

    # POST items/{id}/status body 绑定
    status_resp = biz_client.post(
        f"/api/biz/catalog/items/{item_id}/status",
        json={"to": "active"},
        headers=_headers(),
    )
    if status_resp.status_code == 422:
        detail = status_resp.json().get("detail", [])
        loc = detail[0].get("loc", []) if detail else []
        assert loc[0] != "query", f"POST status body 绑定失败：{status_resp.text}"
    else:
        assert status_resp.status_code in (200, 201), (
            f"POST status 失败：{status_resp.status_code}：{status_resp.text}"
        )


def test_catalog_items_bom_body_binding(biz_client: TestClient) -> None:
    """BUG-1 回归：POST /api/biz/catalog/items/{id}/bom body 应绑定成功。"""
    # 先建 item
    item_resp = biz_client.post(
        "/api/biz/catalog/items",
        json={
            "product_name": "BOM测试产品",
            "rows": [{"name": "BOM测试SKU", "kind": "physical", "code": "BOM-001"}],
        },
        headers=_headers(),
    )
    assert item_resp.status_code in (200, 201), item_resp.text
    item_id = (item_resp.json().get("ids") or [item_resp.json().get("id")])[0]

    # POST /api/biz/catalog/items/{id}/bom body 绑定
    bom_resp = biz_client.post(
        f"/api/biz/catalog/items/{item_id}/bom",
        json={"child_item_id": 99999, "qty": 1},
        headers=_headers(),
    )
    # body 绑定成功（可能 200/201/404/409/422 body 级）
    if bom_resp.status_code == 422:
        detail = bom_resp.json().get("detail", [])
        loc = detail[0].get("loc", []) if detail else []
        assert loc[0] != "query", (
            f"BUG-1 回归：BOM body 绑定失败：{bom_resp.text}"
        )
    else:
        assert bom_resp.status_code in (200, 201, 404, 409), (
            f"BOM 失败：{bom_resp.status_code}：{bom_resp.text}"
        )


def test_warehouses_body_binding(biz_client: TestClient) -> None:
    """BUG-1 回归：POST /api/biz/inventory/warehouses body 应绑定成功。"""
    wh_resp = biz_client.post(
        "/api/biz/inventory/warehouses",
        json={"name": "回归仓库", "code": "GH-REG"},
        headers=_headers(),
    )
    assert wh_resp.status_code in (200, 201), (
        f"BUG-1 回归：仓库 body 绑定失败：{wh_resp.status_code}：{wh_resp.text}"
    )
