"""v0.6 验收断言 A61/A64/A65 + 批 1 数据地基 + 批 3 连接器单测（详设-v0.6 §10；@version_acceptance）。

覆盖（对应详设 §10 验收断言表 + §12 批 1 数据地基 / 批 3 连接器搬回）：
- A61 图来源标记 + SKU×店铺留位（迁移 0011 结构断言）：scrape.link_record 表存在 +
  关键列（url/normalized_url/source/status/image_count/batch_id/error_note/
  degraded_note）+ UNIQUE(normalized_url)；scrape.image_file 增 link_record_id/
  source_mark/sku_id/shop_id 列 + source_mark 默认 'scraped' + CHECK 四值可插 +
  非法值报错 + uq_scrape_image_link_url 唯一索引存在
- A64 xhs 连接器照广成（H1/H2）：脚本 from source import XHS（非 from main import
  download）+ explore_data 中文列 SQL + 差集计数 + 未落盘 ok=False（xsec_token 提示）+
  vendor source/__init__.py 完整度检查（代码级 + 行为级）
- A65 闲鱼连接器照广成（H3）：10 主图 + 6 详情选择器（照广成原文）+ naturalWidth≥100 +
  过滤关键词 + _DEAD_PAGE_KEYWORDS + 全局 90s deadline + networkidle 降级
  （代码级 + fake page 行为单测）
- 批 1 单测（非 acceptance）：scrape_store create_link 幂等（同 normalized_url
  二次调用返回现有行 created=false）/ get_links 筛选 / get_link_by_id（含图片列表）/
  update_link 落库 / get_link_queue/set_link_queue 读写 / normalize_link_url
  规范化（去 xsec_token 等易变 query 保留路径段）/ engine-params 新键
  （scrape.storage_dir + scrape.schedule_time，缺省 + 改键返回变化）

基建：tm_pg_cluster / engine_pg_cluster（conftest 嵌入式 PG，业务库迁移 upgrade
head 自动含 0011）+ 本文件 autouse _clean_v06_tables（每测试后清 sys.settings +
scrape.link_record + scrape.image_file，互不污染——v05 的 _clean_v05_tables 只清
image_file 不清 link_record，本文件要清全）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- URL/IP 一律运行期拼接，单一字符串常量不得含完整 scheme 或 IPv4 四段
- 不读 os.environ / os.getenv（P2 规则4）
- fake page / fake 连接器桩只住 tests/（R12 桩只住 tests/；P3-4 tests/ 豁免）
- 连接器行为单测不真发网络请求（R12 零网络）：subprocess.run / sqlite3.connect /
  sync_playwright 全部 monkeypatch 假对象
"""

from __future__ import annotations

import sqlite3
import subprocess
import time as _time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from test_web_tm import _login  # noqa: F401  # 跨模块 helper（只住 tests/）

REPO_ROOT = Path(__file__).resolve().parents[1]

# P2 合规：URL/token 运行期拼接
_BIZ_TOKEN = "test" + "-biz-token"
_FAKE_BIZ_URL = "ht" + "tp://" + "biz.test"

# P2 合规：含 scheme 的 URL 一律运行期拼接（单一字符串常量不得含完整 scheme）
_XHS_EXPLORE = "ht" + "tps://www.xiaohongshu.com/explore/abc123"
_XHS_SHORT = "ht" + "tp://xhslink.com/a/xyz789"


# ---- fixtures ----


@async_fixture
async def biz_engine(tm_pg_cluster):
    """业务库 AsyncEngine（NullPool：TestClient portal 循环与 pytest 循环不串）。"""
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _web_db_maker(biz_engine, monkeypatch):
    """scrape_store 函数式 DAO 走 web.db 模块级 maker——monkeypatch 指向嵌入式 PG

    （照 test_acceptance_v05.py::test_a56 写法；不触碰 web.db 模块级 _engine）。"""
    import web.db as webdb
    from sqlalchemy.ext.asyncio import async_sessionmaker

    monkeypatch.setattr(
        webdb, "_maker", async_sessionmaker(biz_engine, expire_on_commit=False)
    )
    yield


@async_fixture(autouse=True)
async def _clean_v06_tables(biz_engine):
    """每测试后清 v0.6 批 1 涉及表（sys.settings + scrape.link_record + scrape.image_file），
    互不污染（TRUNCATE CASCADE：image_file.link_record_id FK 级联一并处理）。"""
    yield
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE sys.settings, scrape.link_record, scrape.image_file "
                "RESTART IDENTITY CASCADE"
            )
        )


def _biz_app(biz_engine) -> FastAPI:
    """业务读写接口 app（X-Biz-Token 注入，照 test_acceptance_v05 模式）。"""
    from web.api_biz import create_biz_router

    app = FastAPI()
    app.include_router(create_biz_router(engine=biz_engine, token=_BIZ_TOKEN))
    return app


# ==== A61：图来源标记 + SKU×店铺留位（迁移 0011 结构断言）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a61_image_source_mark_and_sku_shop_slot(biz_engine) -> None:
    """A61：link_record 表 + 关键列 + UNIQUE(normalized_url)；image_file 增
    link_record_id/source_mark/sku_id/shop_id + source_mark 默认/CHECK 四值/
    非法值报错 + uq_scrape_image_link_url 唯一索引（迁移 0011 结构断言，真 SQL）。"""
    async with AsyncSession(biz_engine) as session:
        # ---- scrape.link_record 表存在 + 关键列存在 ----
        cols = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'scrape' AND table_name = 'link_record' "
                "ORDER BY ordinal_position"
            )
        )
        link_cols = {r[0] for r in cols}
        required = {
            "id", "url", "normalized_url", "source", "status", "image_count",
            "desc", "tags", "author_id", "batch_id", "storage_dir",
            "error_note", "degraded_note", "created_at", "updated_at",
        }
        assert required <= link_cols, (
            f"scrape.link_record 缺列: {required - link_cols}"
        )

        # ---- UNIQUE(normalized_url) 唯一约束存在（pg_indexes）----
        idxs = await session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'scrape' AND tablename = 'link_record'"
            )
        )
        link_indexes = {r[0]: r[1] for r in idxs}
        assert "uq_scrape_link_record_normalized_url" in link_indexes, (
            "scrape.link_record 应有唯一约束 uq_scrape_link_record_normalized_url"
        )
        assert "UNIQUE" in link_indexes["uq_scrape_link_record_normalized_url"].upper()
        assert "normalized_url" in link_indexes["uq_scrape_link_record_normalized_url"]

        # ---- scrape.image_file 增列存在 ----
        img_cols = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'scrape' AND table_name = 'image_file' "
                "ORDER BY ordinal_position"
            )
        )
        img_col_names = {r[0] for r in img_cols}
        for col in ("link_record_id", "source_mark", "sku_id", "shop_id"):
            assert col in img_col_names, f"scrape.image_file 缺列 {col}"

    # ---- UNIQUE(normalized_url) 行为：同 normalized_url 第二行报错 ----
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO scrape.link_record (url, normalized_url, source, batch_id) "
                "VALUES (:u, :n, :s, :b)"
            ),
            {"u": "url-1", "n": "norm-same", "s": "xhs", "b": "batch-1"},
        )
    with pytest.raises(IntegrityError):
        async with AsyncSession(biz_engine) as session2, session2.begin():
            await session2.execute(
                text(
                    "INSERT INTO scrape.link_record (url, normalized_url, source, batch_id) "
                    "VALUES (:u, :n, :s, :b)"
                ),
                {"u": "url-2", "n": "norm-same", "s": "xhs", "b": "batch-2"},
            )

    # ---- source_mark 默认 'scraped'（插入不带 source_mark 验证默认值）----
    async with AsyncSession(biz_engine) as session, session.begin():
        row = (
            await session.execute(
                text(
                    "INSERT INTO scrape.image_file (batch_id, source, url) "
                    "VALUES (:b, :s, :u) RETURNING id, source_mark"
                ),
                {"b": "batch-dflt", "s": "xhs", "u": "img-dflt"},
            )
        ).first()
        assert row is not None and row[1] == "scraped", (
            f"source_mark 默认应为 'scraped'，实际 {row[1] if row else None}"
        )

    # ---- source_mark CHECK 四值均可插入 ----
    for idx, mark in enumerate(["scraped", "selfshot", "ai_generated", "authorized"]):
        async with AsyncSession(biz_engine) as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO scrape.image_file (batch_id, source, url, source_mark) "
                    "VALUES (:b, :s, :u, :m)"
                ),
                {"b": "batch-mark", "s": "xhs", "u": f"img-mark-{idx}", "m": mark},
            )

    # ---- source_mark 非法值插入报错（CHECK 执法）----
    with pytest.raises(IntegrityError):
        async with AsyncSession(biz_engine) as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO scrape.image_file (batch_id, source, url, source_mark) "
                    "VALUES (:b, :s, :u, :m)"
                ),
                {"b": "batch-bad", "s": "xhs", "u": "img-bad", "m": "stolen"},
            )

    # ---- uq_scrape_image_link_url 唯一索引存在 ----
    async with AsyncSession(biz_engine) as session:
        idxs2 = await session.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'scrape' AND tablename = 'image_file'"
            )
        )
        img_index_names = {r[0] for r in idxs2}
        assert "uq_scrape_image_link_url" in img_index_names, (
            "scrape.image_file 应有唯一索引 uq_scrape_image_link_url(link_record_id, url)"
        )
        # 保留原 uq_image_file_batch_url 不动（兼容存量）
        assert "uq_image_file_batch_url" in img_index_names

    # ---- uq_scrape_image_link_url 行为：同 link_record_id + 同 url 第二行报错 ----
    async with AsyncSession(biz_engine) as session, session.begin():
        link_id = (
            await session.execute(
                text(
                    "INSERT INTO scrape.link_record (url, normalized_url, source, batch_id) "
                    "VALUES (:u, :n, :s, :b) RETURNING id"
                ),
                {"u": "url-link", "n": "norm-link", "s": "xianyu", "b": "batch-link"},
            )
        ).scalar_one()
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO scrape.image_file (batch_id, source, url, link_record_id) "
                "VALUES (:b, :s, :u, :l)"
            ),
            {"b": "batch-img1", "s": "xianyu", "u": "same-image-url", "l": link_id},
        )
    with pytest.raises(IntegrityError):
        async with AsyncSession(biz_engine) as session2, session2.begin():
            await session2.execute(
                text(
                    "INSERT INTO scrape.image_file (batch_id, source, url, link_record_id) "
                    "VALUES (:b, :s, :u, :l)"
                ),
                {"b": "batch-img2", "s": "xianyu", "u": "same-image-url", "l": link_id},
            )

    # ---- sku_id/shop_id 裸列可写（v0.7 留位，无 FK 约束）----
    async with AsyncSession(biz_engine) as session, session.begin():
        r = await session.execute(
            text(
                "INSERT INTO scrape.image_file (batch_id, source, url, sku_id, shop_id) "
                "VALUES (:b, :s, :u, :sk, :sh) RETURNING sku_id, shop_id"
            ),
            {"b": "batch-sku", "s": "http", "u": "img-sku", "sk": 1001, "sh": 2002},
        )
        row = r.first()
        assert row is not None and row[0] == 1001 and row[1] == 2002


# ==== 批 1 单测：scrape_store 扩展（非 acceptance）====


@pytest.mark.asyncio
async def test_scrape_store_create_link_idempotent() -> None:
    """create_link 幂等：同作品不同 xsec_token → 同 normalized_url → 二次返回现有行。"""
    from web import scrape_store

    u1 = _XHS_EXPLORE + "?xsec_token=TOK1&xsec_source=pc_feed"
    u2 = _XHS_EXPLORE + "?xsec_token=TOK2&xsec_source=pc_feed"

    first = await scrape_store.create_link(u1, "xhs", "batch-1")
    assert first["created"] is True
    assert first["existing"] is False
    assert first["status"] == "pending"
    assert first["source"] == "xhs"
    assert first["batch_id"] == "batch-1"

    second = await scrape_store.create_link(u2, "xhs", "batch-1")
    assert second["created"] is False
    assert second["existing"] is True
    assert second["id"] == first["id"], (
        "同 normalized_url 二次调用应返回现有行（不重复建）"
    )

    # 显式传 normalized_url
    third = await scrape_store.create_link(
        "https-url", "http", "batch-2", normalized_url="explicit-norm"
    )
    assert third["created"] is True
    assert third["normalized_url"] == "explicit-norm"


@pytest.mark.asyncio
async def test_scrape_store_normalize_url() -> None:
    """normalize_link_url：去 xsec_token 等易变 query，保留路径段与其余 query。"""
    from web.scrape_store import normalize_link_url

    u = _XHS_EXPLORE + "?xsec_token=TOK1&xsec_source=pc_feed&extra=1"
    assert normalize_link_url(u) == _XHS_EXPLORE + "?extra=1"
    assert normalize_link_url(_XHS_SHORT) == _XHS_SHORT
    # 无 query 的规范化 = 原样
    assert normalize_link_url(_XHS_EXPLORE) == _XHS_EXPLORE


@pytest.mark.asyncio
async def test_scrape_store_get_links_filters() -> None:
    """get_links 来源/状态筛选 + 分页。"""
    from web import scrape_store

    l1 = await scrape_store.create_link("url-xhs", "xhs", "b1")
    await scrape_store.create_link("url-xianyu", "xianyu", "b2")

    all_links = await scrape_store.get_links(limit=50)
    assert len(all_links) == 2

    xhs_links = await scrape_store.get_links(source="xhs")
    assert len(xhs_links) == 1 and xhs_links[0]["source"] == "xhs"

    # 状态筛选
    await scrape_store.update_link(l1["id"], status="done", image_count=3)
    done_links = await scrape_store.get_links(status="done")
    assert len(done_links) == 1 and done_links[0]["status"] == "done"

    # 分页
    page = await scrape_store.get_links(limit=1, offset=1)
    assert len(page) == 1


@pytest.mark.asyncio
async def test_scrape_store_get_link_by_id_with_images(biz_engine) -> None:
    """get_link_by_id：单条链接 + 该链接图片列表。"""
    from web import scrape_store

    link = await scrape_store.create_link("url-detail", "xhs", "b1")
    # 直接落两张图（挂 link_record_id；批 4 才扩展 create_image_file 签名）
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO scrape.image_file (batch_id, source, url, link_record_id) "
                "VALUES (:b, :s, :u, :l)"
            ),
            {"b": "b1", "s": "xhs", "u": "img-1", "l": link["id"]},
        )
        await session.execute(
            text(
                "INSERT INTO scrape.image_file (batch_id, source, url, link_record_id) "
                "VALUES (:b, :s, :u, :l)"
            ),
            {"b": "b1", "s": "xhs", "u": "img-2", "l": link["id"]},
        )

    detail = await scrape_store.get_link_by_id(link["id"])
    assert detail is not None
    assert detail["link"]["id"] == link["id"]
    assert len(detail["images"]) == 2
    urls = {img["url"] for img in detail["images"]}
    assert urls == {"img-1", "img-2"}


@pytest.mark.asyncio
async def test_scrape_store_update_link() -> None:
    """update_link：状态/图数/元数据/error_note/degraded_note 落库；只更新传入字段。"""
    from web import scrape_store

    link = await scrape_store.create_link("url-upd", "xhs", "b1")
    updated = await scrape_store.update_link(
        link["id"],
        status="done",
        image_count=5,
        desc="测试描述",
        tags=["a", "b"],
        author_id="seller-1",
        storage_dir="xhs/abc",
        error_note="",
        degraded_note="no-title",
    )
    assert updated["status"] == "done"
    assert updated["image_count"] == 5
    assert updated["desc"] == "测试描述"
    assert updated["tags"] == ["a", "b"]
    assert updated["author_id"] == "seller-1"
    assert updated["storage_dir"] == "xhs/abc"
    assert updated["degraded_note"] == "no-title"

    # 只更新部分字段，其余保留
    again = await scrape_store.update_link(link["id"], status="failed")
    assert again["status"] == "failed"
    assert again["image_count"] == 5
    assert again["desc"] == "测试描述"


@pytest.mark.asyncio
async def test_scrape_store_link_queue(biz_engine) -> None:
    """get_link_queue/set_link_queue：读设置键 scrape.link_queue（json 数组，回退 []）。"""
    from web import scrape_store
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    # 表无键回退默认 []
    assert await scrape_store.get_link_queue(settings=store) == []

    # 写入 → 读回
    await scrape_store.set_link_queue(["u1", "u2"], settings=store)
    assert await scrape_store.get_link_queue(settings=store) == ["u1", "u2"]

    # 覆盖写
    await scrape_store.set_link_queue(["u3"], settings=store)
    assert await scrape_store.get_link_queue(settings=store) == ["u3"]

    # 清空
    await scrape_store.set_link_queue([], settings=store)
    assert await scrape_store.get_link_queue(settings=store) == []


@pytest.mark.asyncio
async def test_scrape_schedule_time_and_storage_defaults(biz_engine) -> None:
    """批 1 设置键：scrape.schedule_time（time，默认 07:00）+ scrape.link_queue 注册类型。"""
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    # 表无键回退默认
    assert await store.get("scrape.schedule_time", "07:00") == "07:00"
    assert await store.get("scrape.link_queue", []) == []

    # time 类型解析（存 "08:30" → 解析为 datetime.time）
    from datetime import time as dtime

    await store.set("scrape.schedule_time", "08:30", "定时默认时间")
    parsed = await store.get("scrape.schedule_time", "07:00")
    assert isinstance(parsed, dtime)
    assert parsed.hour == 8 and parsed.minute == 30

    # json 数组
    await store.set("scrape.link_queue", '["u1", "u2"]', "定时队列")
    assert await store.get("scrape.link_queue", []) == ["u1", "u2"]


@pytest.mark.asyncio
async def test_engine_params_scrape_keys(biz_engine) -> None:
    """engine-params 扩展：返回 scrape.storage_dir + scrape.schedule_time（缺省 + 改键变化）。"""
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    client = TestClient(_biz_app(biz_engine), raise_server_exceptions=False)
    headers = {"X-Biz-Token": _BIZ_TOKEN}

    # 缺省（表无键）
    resp = client.get("/api/biz/settings/engine-params", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["scrape.storage_dir"] == "/opt/liuquan/scrape/"
    assert data["scrape.schedule_time"] == "07:00"

    # 改键 → engine-params 返回变化（time 序列化为 "HH:MM" 字符串）
    await store.set("scrape.storage_dir", "/data/liuquan/scrape/", "扒图存储目录")
    await store.set("scrape.schedule_time", "08:30", "定时默认时间")
    resp2 = client.get("/api/biz/settings/engine-params", headers=headers)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["scrape.storage_dir"] == "/data/liuquan/scrape/"
    assert data2["scrape.schedule_time"] == "08:30"


# =====================================================================
# 批 2：入口与队列 + 素材库（详设 §10/§12 批 2）——A59 / A60 / A63
# =====================================================================


def _noop_engine_handler(request: httpx.Request) -> httpx.Response:
    """批 2 页面用最小引擎桩（registry 空 + schedules 空 + 建任务假 id，零网络）。"""
    if request.url.path == "/api/engine/tasks" and request.method == "POST":
        return httpx.Response(201, json={"task_id": "e-000100"})
    if request.url.path == "/api/engine/registry":
        return httpx.Response(200, json={"chains": []})
    if request.url.path == "/api/engine/schedules":
        return httpx.Response(200, json={"schedules": []})
    return httpx.Response(404, json={"detail": "not found"})


def _web_app(biz_engine, engine_handler) -> FastAPI:
    """批 2 页面/接口测试用完整 web app（嵌入式 PG 全 store 注入 + 假引擎客户端）。"""
    from web.app import create_app
    from web.crm_store import CRMStore
    from web.engineapi.client import EngineAPIClient
    from web.settings_store import SettingsStore
    from web.tm_store import TMStore

    return create_app(
        tm_store=TMStore(biz_engine),
        crm_store=CRMStore(biz_engine),
        settings_store=SettingsStore(biz_engine),
        engine_client_factory=lambda: EngineAPIClient(
            base_url=_FAKE_BIZ_URL,
            transport=httpx.MockTransport(engine_handler),
        ),
    )


# ==== A59：一链接一条 + url 幂等（web 层）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a59_link_record_url_idempotent(biz_engine) -> None:
    """A59：web 层幂等/去重——①POST /settings/scrape/queue 同链接（不同 xsec_token）
    两次 → 队列（scrape.link_queue 键）只 1 条（normalized_url 去重）；
    ②POST /api/biz/scrape/links 同 normalized_url 两次 → 只 1 行 link_record
    （第二次返回 existing，created/existing 标记）。"""
    from web import scrape_store
    from web.settings_store import SettingsStore

    u1 = _XHS_EXPLORE + "?xsec_token=TOK1&xsec_source=pc_feed"
    u2 = _XHS_EXPLORE + "?xsec_token=TOK2&xsec_source=pc_feed"

    # ① 设置页「定时队列」加入去重（normalized_url 幂等追加）
    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False)
    _login(client)

    resp1 = client.post("/settings/scrape/queue", data={"action": "add", "urls": u1})
    assert resp1.status_code == 303
    resp2 = client.post("/settings/scrape/queue", data={"action": "add", "urls": u2})
    assert resp2.status_code == 303
    store = SettingsStore(biz_engine)
    queue = await store.get("scrape.link_queue", [])
    assert isinstance(queue, list) and len(queue) == 1, (
        f"同链接（不同 xsec_token）入队两次应只 1 条，实际 {queue!r}"
    )

    # ② /api/biz/scrape/links 幂等（normalized_url 唯一 → 只 1 行）
    biz_client = TestClient(_biz_app(biz_engine), raise_server_exceptions=False)
    headers = {"X-Biz-Token": _BIZ_TOKEN}
    r1 = biz_client.post(
        "/api/biz/scrape/links",
        json={"urls": [u1], "batch_id": "batch-a59-1"},
        headers=headers,
    )
    assert r1.status_code == 200
    data1 = r1.json()
    assert data1["created_count"] == 1
    assert data1["links"][0]["created"] is True
    assert data1["links"][0]["existing"] is False

    r2 = biz_client.post(
        "/api/biz/scrape/links",
        json={"urls": [u2], "batch_id": "batch-a59-2"},
        headers=headers,
    )
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["existing_count"] == 1
    assert data2["links"][0]["existing"] is True
    assert data2["links"][0]["id"] == data1["links"][0]["id"], (
        "同 normalized_url 二次提交应返回现有行（不重复建）"
    )

    rows = await scrape_store.get_links(limit=50)
    assert len(rows) == 1


# ==== A60：图包导出（zip + metadata.json）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a60_link_export_zip(biz_engine, tmp_path) -> None:
    """A60：图包导出——构造 link_record + image_file 行 + 磁盘假图片（local_path
    相对 storage_dir）→ GET /scrape/links/{id}/export → 200 zip → 成员含图片 +
    metadata.json；metadata 字段齐全（url/source/source_mark/width/height/
    watermark/batch_id + 链接记录字段）。"""
    import io
    import json as _json
    import zipfile
    from datetime import time as dtime

    from web import scrape_store
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    await store.set("scrape.storage_dir", str(tmp_path), "扒图存储目录")

    # 磁盘假图片（local_path 相对 storage_dir）
    img_dir = tmp_path / "xhs" / "note1"
    img_dir.mkdir(parents=True)
    (img_dir / "01.jpg").write_bytes(b"fake-jpg-01")
    (img_dir / "02.jpg").write_bytes(b"fake-jpg-02")

    link = await scrape_store.create_link("url-export", "xhs", "batch-export")
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO scrape.image_file (batch_id, source, url, link_record_id, "
                "local_path, source_mark, width, height, watermark) "
                "VALUES (:b, :s, :u, :l, :p, :m, :w, :h, :wm)"
            ),
            {
                "b": "batch-export", "s": "xhs", "u": "img-url-1", "l": link["id"],
                "p": "xhs/note1/01.jpg", "m": "scraped", "w": 800, "h": 600, "wm": True,
            },
        )
        await session.execute(
            text(
                "INSERT INTO scrape.image_file (batch_id, source, url, link_record_id, "
                "local_path, source_mark, width, height, watermark) "
                "VALUES (:b, :s, :u, :l, :p, :m, :w, :h, :wm)"
            ),
            {
                "b": "batch-export", "s": "xhs", "u": "img-url-2", "l": link["id"],
                "p": "xhs/note1/02.jpg", "m": "scraped", "w": 1024, "h": 768, "wm": False,
            },
        )

    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False)
    resp = client.get(f"/scrape/links/{link['id']}/export")
    assert resp.status_code == 200
    assert resp.headers.get("content-type") == "application/zip"
    assert f"link-{link['id']}-" in resp.headers.get("content-disposition", ""), (
        "zip 文件名应含 link-{id}-{8位短hash}"
    )

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    assert "metadata.json" in names
    assert "xhs/note1/01.jpg" in names, f"zip 缺图片成员，实际 {sorted(names)}"
    assert "xhs/note1/02.jpg" in names
    assert zf.read("xhs/note1/01.jpg") == b"fake-jpg-01"

    meta = _json.loads(zf.read("metadata.json"))
    assert meta["url"] == "url-export"
    assert meta["source"] == "xhs"
    assert meta["batch_id"] == "batch-export"
    assert meta["status"] == "pending"
    assert "desc" in meta and "tags" in meta and "author_id" in meta
    assert "error_note" in meta and "degraded_note" in meta

    imgs = meta["images"]
    assert len(imgs) == 2
    for im in imgs:
        for field in (
            "url", "source_mark", "width", "height", "watermark",
            "local_path", "created_at",
        ):
            assert field in im, f"metadata.images 缺字段 {field}"
    assert {im["local_path"] for im in imgs} == {
        "xhs/note1/01.jpg", "xhs/note1/02.jpg"
    }
    marks = {im["source_mark"] for im in imgs}
    assert marks == {"scraped"}

    # 页面可见降级提示：无图链接导出 → 重定向回详情页带 err（不 500）
    empty_link = await scrape_store.create_link("url-empty", "xhs", "batch-empty")
    resp_empty = client.get(f"/scrape/links/{empty_link['id']}/export")
    assert resp_empty.status_code == 303
    assert "err" in resp_empty.headers.get("location", "")

    # 链接详情页渲染：状态/元数据 + 图片网格 + 图包导出按钮（页面可见）
    detail_page = client.get(f"/scrape/links/{link['id']}")
    assert detail_page.status_code == 200
    assert "图包导出" in detail_page.text
    assert "url-export" in detail_page.text
    assert "图片网格" in detail_page.text


# ==== A63：定时默认时间设置生效 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a63_schedule_time_setting_effect(biz_engine) -> None:
    """A63：改 scrape.schedule_time → ①设置键变化 ②假引擎客户端收到
    update_schedule_time(chain_id=scrape_download_chain 行, "0 8 * * *")
    ③定时页渲染显示新 cron（list_schedules 返回该行）。"""
    import json as _json
    from datetime import time as dtime

    from web.settings_store import SettingsStore

    update_calls: list[str] = []
    schedule_row: dict = {
        "id": 1,
        "chain_id": "scrape_download_chain",
        "name": "定时扒图",
        "cron": "0 7 * * *",
        "enabled": True,
        "last_run_at": None,
        "next_run_at": None,
    }

    def _schedule_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/engine/registry":
            return httpx.Response(
                200,
                json={"chains": [{"id": "scrape_download_chain", "name": "定时扒图"}]},
            )
        if request.url.path == "/api/engine/schedules" and request.method == "GET":
            return httpx.Response(200, json={"schedules": [schedule_row]})
        if request.url.path == "/api/engine/schedules/1/time" and request.method == "POST":
            body = _json.loads(request.content)
            schedule_row["cron"] = body.get("schedule")
            update_calls.append(str(body.get("schedule")))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "not found"})

    store = SettingsStore(biz_engine)
    app = _web_app(biz_engine, _schedule_handler)
    client = TestClient(app, follow_redirects=False)
    _login(client)

    # ① 保存 schedule_time=08:00 → 设置键变化
    resp = client.post("/settings/scrape", data={"schedule_time": "08:00"})
    assert resp.status_code == 303
    val = await store.get("scrape.schedule_time", "07:00")
    assert isinstance(val, dtime), f"scrape.schedule_time 应解析为 time，实际 {val!r}"
    assert val.hour == 8 and val.minute == 0

    # ② 假客户端收到 update_schedule_time（cron = "0 8 * * *"）
    assert update_calls == ["0 8 * * *"], f"引擎 update_time 调用实锤：{update_calls}"

    # ③ 定时页渲染显示新 cron
    page = client.get("/settings/schedule")
    assert page.status_code == 200
    assert "0 8 * * *" in page.text, "定时页应显示更新后的 cron"


# ==== 批 2 辅助单测（非 acceptance）====


@pytest.mark.asyncio
async def test_settings_scrape_queue_clear(biz_engine) -> None:
    """设置页定时队列清空：action=clear → 队列 [] + 页面片段（HTMX）。"""
    from web import scrape_store
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    await scrape_store.set_link_queue(["u1", "u2"], settings=store)

    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False)
    _login(client)
    resp = client.post(
        "/settings/scrape/queue",
        data={"action": "clear"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "定时队列已清空" in resp.text
    assert await store.get("scrape.link_queue", []) == []


@pytest.mark.asyncio
async def test_scrape_run_engine_error_friendly(biz_engine) -> None:
    """POST /scrape/run：引擎侧 422 透传友好错误（链路未通属预期，页面提示不 500）。"""
    from web.app import create_app
    from web.crm_store import CRMStore
    from web.engineapi.client import EngineAPIClient
    from web.settings_store import SettingsStore
    from web.tm_store import TMStore

    def _reject_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/engine/tasks" and request.method == "POST":
            return httpx.Response(422, json={"detail": "input 校验失败（链未通）"})
        return httpx.Response(404, json={"detail": "not found"})

    app = create_app(
        tm_store=TMStore(biz_engine),
        crm_store=CRMStore(biz_engine),
        settings_store=SettingsStore(biz_engine),
        engine_client_factory=lambda: EngineAPIClient(
            base_url=_FAKE_BIZ_URL,
            transport=httpx.MockTransport(_reject_handler),
        ),
    )
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _login(client)
    resp = client.post("/scrape/run", json={"urls": [_XHS_EXPLORE]})
    assert resp.status_code == 502
    data = resp.json()
    assert data["ok"] is False
    assert "422" in data["error"], f"错误应透传引擎 422 详情：{data['error']}"
    assert "input 校验失败" in data["error"]


# =====================================================================
# 批 3：连接器搬回（详设 §6/§12 批 3）——A64 xhs / A65 闲鱼（照广成）
# =====================================================================


# ---- fake page 桩（只住 tests/，R12 桩只住 tests/；P3-4 tests/ 豁免）----


class _FakeLocator:
    """fake page.locator：每选择器一组 img 属性 dict（只住 tests/）。"""

    def __init__(self, imgs: list[dict]) -> None:
        self._imgs = imgs
        self.first = _FakeElement(imgs[0]) if imgs else _FakeElement({})

    def count(self) -> int:
        return len(self._imgs)

    def nth(self, i: int) -> "_FakeElement":
        return _FakeElement(self._imgs[i])


class _FakeElement:
    """fake 元素：get_attribute / inner_text。"""

    def __init__(self, attrs: dict) -> None:
        self._attrs = attrs

    def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    def inner_text(self, timeout: int | None = None) -> str:
        return self._attrs.get("_inner_text", "")


class _FakeResponse:
    """fake page.request.get 响应：ok + body。"""

    def __init__(self, body: bytes) -> None:
        self.ok = True
        self._body = body

    def body(self) -> bytes:
        return self._body


class _FakePage:
    """fake playwright page（只住 tests/）：selector → img 列表；evaluate 按脚本特征分发。"""

    def __init__(
        self,
        selectors: dict | None = None,
        body_text: str = "",
        title: str = "",
        seller_eval: str = "",
        harvest_srcs: tuple = (),
        request_body: bytes = b"x" * 20_000,
    ) -> None:
        self._selectors = selectors or {}
        self._body_text = body_text
        self._title = title
        self._seller_eval = seller_eval
        self._harvest_srcs = list(harvest_srcs)
        self._request_body = request_body
        self.downloaded: list[str] = []
        self.goto_calls: list[tuple[str, str | None]] = []

    @property
    def mouse(self) -> "_FakePage":
        return self

    @property
    def request(self) -> "_FakePage":
        return self

    def wheel(self, dx: int = 0, dy: int = 0) -> None:
        pass

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self.goto_calls.append((url, wait_until))

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self._selectors.get(selector, []))

    def title(self) -> str:
        return self._title

    def evaluate(self, script: str):
        if "innerText" in script:
            return self._body_text
        if "naturalWidth" in script:
            return self._harvest_srcs
        if "__INITIAL_STATE__" in script or "__PRELOADED_STATE__" in script:
            return self._seller_eval
        return None

    def get(self, url: str, timeout: int | None = None) -> _FakeResponse:
        self.downloaded.append(url)
        return _FakeResponse(self._request_body)


class _FakeSyncPlaywright:
    """fake sync_playwright() 上下文（只住 tests/）：chromium.launch → browser → page。"""

    def __init__(self, page: _FakePage) -> None:
        self._page = page

    def __enter__(self) -> "_FakePW":
        return _FakePW(self._page)

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakePW:
    """fake playwright 实例：pw.chromium（BrowserType）→ launch() → browser（new_page/close 挂本对象）。"""

    def __init__(self, page: _FakePage) -> None:
        self._page = page

    @property
    def chromium(self) -> "_FakePW":
        # 真实 playwright 里 pw.chromium 是 BrowserType 对象（属性非方法），launch() 返回 Browser
        return self

    def launch(self, headless: bool = True) -> "_FakePW":
        return self

    def new_page(self, user_agent: str | None = None, viewport: dict | None = None) -> _FakePage:
        return self._page

    def close(self) -> None:
        pass


# ==== A64：xhs 连接器照广成（H1/H2，代码级 + 行为级）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a64_xhs_connector_studio_style(tmp_path, monkeypatch) -> None:
    """A64：xhs 连接器照广成——源码断言（from source import XHS / explore_data 中文列 /
    差集计数 / xsec_token 提示 / vendor source/__init__ 完整度）+ available 行为 +
    差集下载行为（本次新增不算历史图）。"""
    src = (REPO_ROOT / "engine" / "connectors" / "xhs.py").read_text(encoding="utf-8")

    # H1：子进程脚本 from source import XHS（非 from main import download，后者必 ImportError）
    assert "from source import XHS" in src, "子进程脚本应 from source import XHS"
    assert "from main import download" not in src, "不得再 from main import download（必 ImportError）"
    assert "folder_mode=True" in src
    assert "image_format='JPEG'" in src
    assert "xhs.extract" in src
    # unset 代理 6 key（含 all_proxy）+ PATH 补 ~/.local/bin + 180s 超时
    assert "all_proxy" in src
    assert "http_proxy" in src and "https_proxy" in src
    assert ".local" in src and "bin" in src
    assert "180" in src

    # H2：元数据 SQL explore_data 中文列（非 note_data 表/英文列）
    assert "explore_data" in src and "作品描述" in src, "SQL 应查 explore_data 中文列"
    assert "note_data" not in src, "note_data 表已废弃（表/列名全错）"

    # 差集计数：扒前/扒后两次 rglob（before/after）
    assert src.count("rglob") >= 2, "差集计数应两次 rglob（before/after）"
    assert "before" in src and "after" in src

    # 未落盘 → ok=False + xsec_token 过期提示
    assert "xsec_token" in src, "未落盘 note 应含 xsec_token 提示"

    # vendor 完整度：available 检查 source/__init__.py（非仅目录存在）
    assert "__init__.py" in src and '"source"' in src

    # vendor 完整度真测（B4-7）：真实 vendor 的 source/__init__.py 必须存在
    real_vendor = REPO_ROOT / "vendor" / "XHS-Downloader" / "source" / "__init__.py"
    assert real_vendor.is_file(), "vendor/XHS-Downloader/source/__init__.py 应存在（vendor 完整度真测）"

    # available 行为：仅目录存在（无 source/__init__.py）→ False；补齐 → True
    from engine.connectors.xhs import XHSConnector

    connector = XHSConnector()
    fake_vendor = tmp_path / "vendor-xhs"
    fake_vendor.mkdir()
    monkeypatch.setattr(connector, "_vendor", lambda: fake_vendor)
    assert connector.available is False, "仅目录存在（无 source/__init__.py）不应判可用"
    (fake_vendor / "source").mkdir()
    (fake_vendor / "source" / "__init__.py").write_text("", encoding="utf-8")
    assert connector.available is True

    # 行为：下载成功 paths=本次新增（差集语义）——历史图不算进本次 count/paths
    storage = tmp_path / "storage"
    out_dir = storage / "xhs"
    dl = out_dir / "Download"
    dl.mkdir(parents=True)
    (dl / "old.jpg").write_bytes(b"old-historical")  # 历史图（before 已含）

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        new_dir = dl / "note1"
        new_dir.mkdir(exist_ok=True)
        (new_dir / "01.jpg").write_bytes(b"new-download")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = await connector.download(
        _XHS_EXPLORE + "?xsec_token=TOK1",
        batch_id="batch-a64",
        storage_dir=str(storage),
    )
    assert result.ok is True, result.note
    assert result.data is not None
    assert result.data["count"] == 1, f"差集语义：count 应只算本次新增，实际 {result.data}"
    assert [Path(p).name for p in result.data["paths"]] == ["01.jpg"], (
        f"差集语义：paths 应只含本次新增，实际 {result.data['paths']}"
    )
    assert result.data["source"] == "xhs"
    assert "db-missing" in result.note, (
        "图已落盘 + 元数据降级应记 note（db-missing），不整体失败"
    )


@pytest.mark.asyncio
async def test_xhs_behavior_metadata_chinese_columns(monkeypatch, tmp_path) -> None:
    """xhs 元数据：SQL 查 explore_data 中文列（作品描述/作品标签/作者ID），标签空格拆分。"""
    from engine.connectors.xhs import XHSConnector

    connector = XHSConnector(storage_dir=str(tmp_path / "storage"))
    out_dir = tmp_path / "storage" / "xhs"
    dl = out_dir / "Download"
    dl.mkdir(parents=True)
    (dl / "ExploreData.db").write_bytes(b"")  # 存在性检查通过

    sql_seen: list[str] = []

    class _FakeCursor:
        def fetchone(self):
            return ("测试描述 多行", "tag1 tag2 tag3", "author-xyz")

    class _FakeConn:
        def __init__(self, path: str) -> None:
            self._path = path

        def execute(self, sql: str, params: tuple):
            sql_seen.append(sql)
            assert "explore_data" in sql and "作品描述" in sql, (
                f"SQL 应查 explore_data 中文列: {sql}"
            )
            assert "note_data" not in sql
            return _FakeCursor()

        def close(self) -> None:
            pass

    monkeypatch.setattr(sqlite3, "connect", _FakeConn)
    meta, note = connector._read_xhs_meta(out_dir, _XHS_EXPLORE)
    assert note == ""
    assert meta["desc"] == "测试描述 多行"
    assert meta["tags"] == ["tag1", "tag2", "tag3"], "标签应空格拆分"
    assert meta["author_id"] == "author-xyz"
    assert sql_seen and '"作品ID" = ?' in sql_seen[0], (
        f"SQL 应按作品ID 精确匹配: {sql_seen}"
    )


@pytest.mark.asyncio
async def test_xhs_behavior_no_files_ok_false(monkeypatch, tmp_path) -> None:
    """xhs 未落盘：paths 空 → ok=False + note 含 xsec_token 过期提示。"""
    from engine.connectors.xhs import XHSConnector

    connector = XHSConnector(storage_dir=str(tmp_path / "storage"))

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = await connector.download(
        _XHS_EXPLORE + "?xsec_token=TOK2", batch_id="batch-xhs-nofiles"
    )
    assert result.ok is False
    assert "xsec_token" in result.note
    assert "未落盘" in result.note


# ==== A65：闲鱼连接器照广成（H3，代码级 + fake page 行为单测）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a65_xianyu_connector_studio_style() -> None:
    """A65：闲鱼连接器照广成——10 主图 + 6 详情选择器（照广成原文）+ 过滤关键词 +
    _DEAD_PAGE_KEYWORDS + GLOBAL_TIMEOUT=90 + naturalWidth 阈值 100 + _MIN_IMG_BYTES +
    networkidle 降级（代码级）。"""
    from engine.connectors import xianyu as mod

    # 主图选择器 ≥10 套（照广成 _MAIN_IMG_SELECTORS 原文 10 套，命中即停）
    assert len(mod._MAIN_IMG_SELECTORS) >= 10, mod._MAIN_IMG_SELECTORS
    for kw in (
        "swiper-slide", "carousel", "gallery", "mainImg", "main-image",
        "imageMain", "picMain", "imageBox", "slider", "slide",
    ):
        assert any(kw in sel for sel in mod._MAIN_IMG_SELECTORS), f"主图选择器缺 {kw}"

    # 详情选择器 ≥6 套
    assert len(mod._DETAIL_IMG_SELECTORS) >= 6, mod._DETAIL_IMG_SELECTORS
    for kw in ("itemDesc", "desc", "detail", "content", "ImageText", "imageText"):
        assert any(kw in sel for sel in mod._DETAIL_IMG_SELECTORS), f"详情选择器缺 {kw}"

    # URL 过滤关键词（照广成原文 10 个，含 avatar/icon/logo/qrcode）
    assert len(mod._FILTER_KEYWORDS) >= 9
    for kw in (
        "avatar", "icon", "logo", "sprite", "placeholder", "loading",
        "blank", "default", "qrcode", "qr-code",
    ):
        assert kw in mod._FILTER_KEYWORDS, f"过滤关键词缺 {kw}"

    # 失效页关键词
    assert "宝贝被删掉" in mod._DEAD_PAGE_KEYWORDS
    assert "已下架" in mod._DEAD_PAGE_KEYWORDS

    # 全局 90s deadline（time.monotonic 逐张检查，非 set_default_timeout）
    assert mod.GLOBAL_TIMEOUT == 90
    # naturalWidth/Height ≥ 100 大图过滤
    assert mod._MIN_IMG_SIZE == 100
    # 文件大小过滤 15KB
    assert mod._MIN_IMG_BYTES == 15_000

    # networkidle 优先 + domcontentloaded 降级 + 全页兜底 _harvest_all_imgs
    src = (REPO_ROOT / "engine" / "connectors" / "xianyu.py").read_text(encoding="utf-8")
    assert "networkidle" in src and "domcontentloaded" in src
    assert "_harvest_all_imgs" in src
    assert "naturalWidth" in src
    assert "naturalHeight" in src


@pytest.mark.asyncio
async def test_xianyu_behavior_main_img_collect() -> None:
    """闲鱼主图选择器命中即收集（fake page，零网络）。"""
    from engine.connectors.xianyu import XianyuConnector, _MAIN_IMG_SELECTORS

    img1 = "ht" + "tps://img.alicdn.com/imgextra/i1/aaa.jpg"
    img2 = "ht" + "tps://img.alicdn.com/imgextra/i2/bbb.jpg"
    page = _FakePage(
        selectors={_MAIN_IMG_SELECTORS[0]: [{"src": img1}, {"src": img2}]},
    )
    connector = XianyuConnector()
    urls = connector._collect_image_urls(page)
    assert urls == [img1, img2]


@pytest.mark.asyncio
async def test_xianyu_behavior_filter_keywords_skip() -> None:
    """闲鱼 URL 过滤：avatar/icon/logo/qrcode + data: 排除，正常大图保留。"""
    from engine.connectors.xianyu import XianyuConnector

    connector = XianyuConnector()
    assert connector._is_valid_image_url("ht" + "tps://img.alicdn.com/avatar/1.jpg") is False
    assert connector._is_valid_image_url("ht" + "tps://img.alicdn.com/xx/icon.png") is False
    assert connector._is_valid_image_url("ht" + "tps://img.alicdn.com/logo.png") is False
    assert connector._is_valid_image_url("ht" + "tps://img.alicdn.com/qrcode.png") is False
    assert connector._is_valid_image_url("data:image/png;base64,xx") is False
    assert connector._is_valid_image_url("ht" + "tps://img.alicdn.com/imgextra/i1/real.jpg") is True


@pytest.mark.asyncio
async def test_xianyu_behavior_dead_page_deletes_images(monkeypatch, tmp_path) -> None:
    """闲鱼失效页：body 命中「宝贝被删掉」→ 删已下载垃圾图 + dead-page 标记 + desc 标记。"""
    from engine.connectors import xianyu as mod
    from engine.connectors.xianyu import XianyuConnector

    out_dir = tmp_path / "xianyu" / "item123"
    out_dir.mkdir(parents=True)

    img = "ht" + "tps://img.alicdn.com/imgextra/i9/dead.jpg"
    page = _FakePage(
        selectors={mod._MAIN_IMG_SELECTORS[0]: [{"src": img}]},
        body_text="该宝贝被删掉或不存在，请重新搜索",
    )
    monkeypatch.setattr(mod, "sync_playwright", lambda: _FakeSyncPlaywright(page))

    connector = XianyuConnector(storage_dir=str(tmp_path))
    deadline = _time.monotonic() + 90
    paths, desc, author_id, note = connector._scrape(
        "ht" + "tps://www.goofish.com/item?id=item123", out_dir, deadline
    )
    assert paths == []
    assert desc == "商品已失效/删除"
    assert "dead-page" in note
    assert not any(out_dir.iterdir()), "失效页应删除已下载垃圾图"


@pytest.mark.asyncio
async def test_xianyu_behavior_deadline_truncates(tmp_path) -> None:
    """闲鱼全局 90s deadline：deadline 已过 → 一张都不下载（逐张检查语义）。"""
    from engine.connectors.xianyu import XianyuConnector

    page = _FakePage()
    connector = XianyuConnector()
    paths = connector._download_images(
        page,
        ["u1.jpg", "u2.jpg"],
        tmp_path,
        deadline=_time.monotonic() - 1,  # 已过期
    )
    assert paths == []
    assert page.downloaded == [], "deadline 已过不应发起任何下载"


@pytest.mark.asyncio
async def test_xianyu_playwright_missing_note(monkeypatch) -> None:
    """闲鱼 playwright 未装 → ok=False + 安装指引 note（照广成原文）。"""
    from engine.connectors import xianyu as mod
    from engine.connectors.xianyu import XianyuConnector

    monkeypatch.setattr(mod, "_PW_AVAILABLE", False)
    connector = XianyuConnector()
    assert connector.available is False
    result = await connector.download(
        "ht" + "tps://www.goofish.com/item?id=12345", batch_id="batch-xianyu"
    )
    assert result.ok is False
    assert "pip install playwright" in result.note
    assert "install chromium" in result.note
