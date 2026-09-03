"""v0.6 验收断言（详设-v0.6 §10 + §15.4 + §15.7；@version_acceptance）：A57-A75 + B5 收口四断言 + 批 1-9 辅助单测。

覆盖（对应详设 §10 验收断言表 + §12 批 1-5 + §15 批 6-8）：
- A61 图来源标记 + SKU×店铺留位（迁移 0011 结构断言）+ 批 6 迁移 0012 netdisk
  三列：scrape.link_record 表存在 +
  关键列（url/normalized_url/source/status/image_count/batch_id/error_note/
  degraded_note/netdisk_status/netdisk_url/netdisk_uploaded_at）+
  UNIQUE(normalized_url)；scrape.image_file 增 link_record_id/
  source_mark/sku_id/shop_id 列 + source_mark 默认 'scraped' + CHECK 四值可插 +
  非法值报错 + uq_scrape_image_link_url 唯一索引存在
- A64 xhs 连接器照广成（H1/H2）：脚本 from source import XHS（非 from main import
  download）+ explore_data 中文列 SQL + 差集计数 + 未落盘 ok=False（xsec_token 提示）+
  vendor source/__init__.py 完整度检查（代码级 + 行为级）
- A65 闲鱼连接器照广成（H3）：10 主图 + 6 详情选择器（照广成原文）+ naturalWidth≥100 +
  过滤关键词 + _DEAD_PAGE_KEYWORDS + 全局 90s deadline + networkidle 降级
  （代码级 + fake page 行为单测）
- A57/A58/A59/A60（批 6 改语义）/A62/A63/A66/A67/A68/A69（批 2-4 + 批 6 链路与落盘）：
  立即扒端到端落素材库 /
  拆两链 input 契约 / 链接幂等 / 一链接一文件夹（zip 导出移除，归集 + meta.txt）/
  定时队列 + 定时链 / 定时时间设置生效 /
  suggest 链诚实化（真接 LLM → 提案 pending）/ biz_client 注入 + image_inspect 写回 /
  失败与降级明确提示 / 一链接一文件夹 + meta.txt 内容齐全（作者缺失记「无」）
- A70/A71/A72（批 7）：夸克上传 flow（勾选上传回填 uploaded / 未勾选零调用）/
  未授权 -1408 → failed + error_note 登录提示 / 历史补传入口（按钮 DOM + POST 入参实锤）
- A73/A74（批 8，详设 §15.3/§15.4）：定时队列管理在扒图页（/scrape 含队列块 +
  /settings/scrape 无队列块）/ 设置键 netdisk.upload_default 生效（改 false →
  设置键 + engine-params + 扒图页复选框初始值 + 引擎定时 input upload_netdisk=false）
- A75（批 9，详设 §15.7）：独立任务详情页 GET /tasks/{id}（完整 title/detail 全文/
  tags/domain 徽章/source_type + task.source 逐键 + 关联提案 evidence + 事件时间线 +
  步骤与完成守卫提示；越界 404；列表行 title 变链接入口）
- B5 收口四断言（批 5，planned → implemented）：
  A29 候选忽略 dismissed 不建任务不飞书（行为断言）/
  A47 贴链接 → image_file 落库 + 提案审核真实链路（下载链 + suggest 链组合）/
  A48 提案批准 → tm.task domain=scrape 落 TM 清单（扒图徽章）/
  A51 seo-optimize 规格 config/spec.yaml 可改行为变化实锤
- 批 1-4 单测（非 acceptance）：scrape_store create_link 幂等（同 normalized_url
  二次调用返回现有行 created=false）/ get_links 筛选 / get_link_by_id（含图片列表）/
  update_link 落库 / get_link_queue/set_link_queue 读写 / normalize_link_url
  规范化（去 xsec_token 等易变 query 保留路径段）/ engine-params 新键
  （scrape.storage_dir + scrape.schedule_time，缺省 + 改键返回变化）/ 消费者
  from_queue 语义 / 调度器 input 模板 / 工序 biz_client 未注入降级

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

from test_runner import (  # noqa: F401  # 跨模块 fixture：引擎库嵌入式 PG + 清表（随模块收集）
    FakeAgent,
    _clean_engine_tables,
    db_engine,
    make_agent_factory,
)
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
    """每测试后清本模块涉及表（sys.settings + scrape.link_record/scrape.image_file +
    crm 五表 + tm 四表），互不污染（TRUNCATE CASCADE：image_file.link_record_id FK
    级联一并处理）。

    批 5 起本模块测试写 crm/tm 表（A29 候选 / A47/A48 提案+任务）——不清理会在
    pytest -m version_acceptance 选取子集时污染 test_web_crm（A36 删除级联断言
    crm/tm 计数为 0 实锤抓到过，2026-09-03 批 5 修正）。"""
    yield
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE sys.settings, scrape.link_record, scrape.image_file, "
                "crm.todo_candidate, crm.snapshot, crm.message, crm.message_image, "
                "crm.customer, tm.task_event, tm.task_step, tm.task_proposal, tm.task "
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
    """A61：link_record 表 + 关键列（含批 6 迁移 0012 netdisk 三列）+ UNIQUE(normalized_url)；
    image_file 增 link_record_id/source_mark/sku_id/shop_id + source_mark 默认/CHECK 四值/
    非法值报错 + uq_scrape_image_link_url 唯一索引（迁移 0011/0012 结构断言，真 SQL）。"""
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
            # 批 6（迁移 0012，详设-v0.6 §15.2）：夸克网盘上传三列
            "netdisk_status", "netdisk_url", "netdisk_uploaded_at",
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
async def test_scrape_store_link_netdisk_columns(biz_engine) -> None:
    """迁移 0012 netdisk 三列（批 6，详设-v0.6 §15.2）：结构存在 + create_link
    默认值 + update_link 支持 netdisk_status/netdisk_url/netdisk_uploaded_at 回填。"""
    from datetime import datetime, timezone

    from web import scrape_store

    # ---- 结构断言（迁移 0012：netdisk_status CHECK 四值 + url + uploaded_at）----
    async with AsyncSession(biz_engine) as session:
        cols = await session.execute(
            text(
                "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'scrape' AND table_name = 'link_record' "
                "AND column_name IN ('netdisk_status', 'netdisk_url', 'netdisk_uploaded_at')"
            )
        )
        rows = {r[0]: r for r in cols}
        assert set(rows) == {"netdisk_status", "netdisk_url", "netdisk_uploaded_at"}, (
            f"迁移 0012 应建 netdisk 三列，实际 {sorted(rows)}"
        )
        # netdisk_status 非空 + 默认 'none'
        assert rows["netdisk_status"][1] == "NO"
        assert "'none'" in (rows["netdisk_status"][2] or "")
        # netdisk_uploaded_at 应为 timestamptz（information_schema 无类型，另行断言）
    async with AsyncSession(biz_engine) as session:
        chk = await session.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'chk_scrape_link_netdisk_status'"
            )
        )
        row = chk.first()
        assert row is not None and "netdisk_status" in row[0] and "uploaded" in row[0], (
            "netdisk_status 应有 CHECK（none/pending/uploaded/failed）"
        )

    # ---- create_link 默认值（netdisk_status='none'，url 空）----
    link = await scrape_store.create_link("url-netdisk", "xhs", "b-netdisk")
    assert link["netdisk_status"] == "none", (
        f"netdisk_status 默认应为 none，实际 {link.get('netdisk_status')!r}"
    )
    assert link.get("netdisk_url") is None
    assert link.get("netdisk_uploaded_at") is None

    # ---- update_link 支持网盘三列回填（批 7 上传后调用形态）----
    uploaded_at = datetime(2026, 9, 3, 10, 30, 0, tzinfo=timezone.utc)
    updated = await scrape_store.update_link(
        link["id"],
        netdisk_status="uploaded",
        netdisk_url="ht" + "tps://pan.quark.cn/s/abc123456789",
        netdisk_uploaded_at=uploaded_at,
    )
    assert updated["netdisk_status"] == "uploaded"
    assert updated["netdisk_url"] == "ht" + "tps://pan.quark.cn/s/abc123456789"
    assert updated["netdisk_uploaded_at"] is not None

    # 只更新部分字段其余保留
    partial = await scrape_store.update_link(link["id"], netdisk_status="failed")
    assert partial["netdisk_status"] == "failed"
    assert partial["netdisk_url"] == "ht" + "tps://pan.quark.cn/s/abc123456789"


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
    """A59：web 层幂等/去重——①POST /scrape/queue 同链接（不同 xsec_token）
    两次 → 队列（scrape.link_queue 键）只 1 条（normalized_url 去重；批 8
    §15.3 队列路由从设置页迁到扒图页）；
    ②POST /api/biz/scrape/links 同 normalized_url 两次 → 只 1 行 link_record
    （第二次返回 existing，created/existing 标记）。"""
    from web import scrape_store
    from web.settings_store import SettingsStore

    u1 = _XHS_EXPLORE + "?xsec_token=TOK1&xsec_source=pc_feed"
    u2 = _XHS_EXPLORE + "?xsec_token=TOK2&xsec_source=pc_feed"

    # ① 扒图页「定时队列」加入去重（normalized_url 幂等追加；批 8 起走 /scrape/queue）
    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False)
    _login(client)

    resp1 = client.post("/scrape/queue", data={"action": "add", "urls": u1})
    assert resp1.status_code == 200
    assert resp1.json().get("ok") is True
    resp2 = client.post("/scrape/queue", data={"action": "add", "urls": u2})
    assert resp2.status_code == 200
    assert resp2.json().get("count") == 1, f"去重后应 1 条，实际 {resp2.json()}"
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


# ==== A60（批 6 改语义，详设 §15.4）：zip 导出移除 → 一链接一文件夹 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a60_link_folder_export(db_engine, biz_engine, tmp_path) -> None:
    """A60（批 6 改语义，详设 §15.4）：下载链跑完（fake connector + fake biz_client）
    → 链接文件夹存在且含全部图片（01.xxx 归集命名）+ link_record.storage_dir 指向
    该文件夹 + meta.txt 在场；详情页展示「本地文件夹」；zip 导出路由已移除（404）。"""
    from web import scrape_store
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    await store.set("scrape.storage_dir", str(tmp_path), "扒图存储目录")

    fake_conn = _FakeConnector(tmp_path)
    biz_client = _biz_client_for(biz_engine)
    runner, _ = _build_runner(
        db_engine,
        biz_client=biz_client,
        connectors={"xhs": fake_conn, "xianyu": fake_conn, "http_image": fake_conn},
        storage_dir=str(tmp_path),  # 批 6：EngineContext.storage_dir（归集建链接文件夹）
    )
    result = await runner.run(
        "scrape_download_chain",
        {"urls": [_XHS_EXPLORE], "batch_id": "batch-a60", "from_queue": False},
    )
    assert result.status == "done", f"链应 DONE：{result.error}"

    # ---- 链接文件夹存在 + 含全部图片（01.xxx 归集命名）+ meta.txt ----
    links = await scrape_store.get_links(limit=50)
    assert len(links) == 1, f"应 1 条链接记录，实际 {len(links)}"
    link = links[0]
    assert link["status"] == "done"
    assert link["image_count"] == 2

    link_folder = tmp_path / "xhs" / "abc123"
    assert link_folder.is_dir(), f"链接文件夹应存在：{link_folder}"
    assert link["storage_dir"] == "xhs/abc123", (
        f"link_record.storage_dir 应指向链接文件夹，实际 {link['storage_dir']!r}"
    )
    files = sorted(p.name for p in link_folder.iterdir() if p.is_file())
    assert files == ["01.png", "02.png", "meta.txt"], (
        f"链接文件夹应含全部图片（归集命名）+ meta.txt，实际 {files}"
    )
    assert (link_folder / "meta.txt").read_text(encoding="utf-8").startswith("标题：测试商品描述")

    # ---- 图片 local_path 全部指向链接文件夹内（相对 storage_dir）----
    detail = await scrape_store.get_link_by_id(link["id"])
    imgs = detail["images"]
    assert len(imgs) == 2, f"应 2 张图挂链接，实际 {len(imgs)}"
    for img in imgs:
        assert img["local_path"].startswith("xhs/abc123/"), (
            f"local_path 应在链接文件夹内，实际 {img['local_path']!r}"
        )
        assert Path(str(tmp_path), str(img["local_path"])).is_file()

    # ---- 页面可见：详情页展示本地文件夹；zip 导出按钮/路由移除 ----
    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _login(client)
    page = client.get(f"/scrape/links/{link['id']}")
    assert page.status_code == 200
    assert "本地文件夹" in page.text, "详情页应展示本地文件夹"
    assert "xhs/abc123" in page.text, "详情页应展示 storage_dir 相对路径"
    assert "图包导出" not in page.text, "图包导出按钮应已移除"
    resp_export = client.get(f"/scrape/links/{link['id']}/export")
    assert resp_export.status_code == 404, "zip 导出路由应已移除（404）"


# ==== A69（批 6，详设 §15.4）：一链接一文件夹 + meta.txt 内容齐全 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a69_link_folder_and_meta_txt(db_engine, biz_engine, tmp_path) -> None:
    """A69：下载链跑完（正常链接 + 作者缺失降级链接）→ 每个链接文件夹存在
    （含全部图片 + meta.txt）；meta.txt 内容齐全（标题/来源/原链接/作者ID/描述/
    标签/图片数/爬取时间（YYYY-MM-DD HH:MM:SS）/批次/网盘分享链接行）；
    作者缺失链接 meta.txt 记「作者ID：无」；降级 note 进备注行。"""
    import re as _re

    from web import scrape_store

    degrade_url = "ht" + "tps://www.xiaohongshu.com/explore/degrade456"
    fake_conn = _FakeConnector(tmp_path, degrade_urls={degrade_url})
    biz_client = _biz_client_for(biz_engine)
    runner, _ = _build_runner(
        db_engine,
        biz_client=biz_client,
        connectors={"xhs": fake_conn, "xianyu": fake_conn, "http_image": fake_conn},
        storage_dir=str(tmp_path),  # 批 6：EngineContext.storage_dir（归集建链接文件夹）
    )
    result = await runner.run(
        "scrape_download_chain",
        {"urls": [_XHS_EXPLORE, degrade_url], "batch_id": "batch-a69", "from_queue": False},
    )
    assert result.status == "done", f"链接级失败不应阻断链：{result.error}"

    links = await scrape_store.get_links(limit=50)
    assert len(links) == 2, f"应 2 条链接记录，实际 {len(links)}"

    # ---- 正常链接：meta.txt 内容齐全（作者在场，无备注行）----
    normal_folder = tmp_path / "xhs" / "abc123"
    assert normal_folder.is_dir(), "正常链接文件夹应存在"
    meta_text = (normal_folder / "meta.txt").read_text(encoding="utf-8")
    assert meta_text.startswith("标题：测试商品描述")
    assert "来源：xhs（小红书）" in meta_text
    assert f"原链接：{_XHS_EXPLORE}" in meta_text, "meta.txt 应含原链接"
    assert "作者ID：seller-1" in meta_text
    assert "描述：测试商品描述" in meta_text
    assert "标签：tag1, tag2" in meta_text
    assert "图片数：2" in meta_text
    # 爬取时间格式：YYYY-MM-DD HH:MM:SS
    m = _re.search(r"^爬取时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$", meta_text, _re.M)
    assert m is not None, f"meta.txt 应含爬取时间（YYYY-MM-DD HH:MM:SS），实际：\n{meta_text}"
    assert f"批次：batch-a69" in meta_text
    assert "网盘分享链接：未上传" in meta_text, "本批未上传网盘，meta.txt 网盘行应为「未上传」"
    assert "备注：" not in meta_text, "无降级/失败的链接不应有备注行"
    normal_files = sorted(
        p.name for p in normal_folder.iterdir() if p.is_file() and p.name != "meta.txt"
    )
    assert normal_files == ["01.png", "02.png"], (
        f"正常链接文件夹应含 2 张归集图片，实际 {normal_files}"
    )

    # ---- 作者缺失（降级 db-missing）链接：meta.txt 作者ID：无 + 降级进备注 ----
    degrade_folder = tmp_path / "xhs" / "degrade456"
    assert degrade_folder.is_dir(), "降级链接文件夹应存在"
    deg_text = (degrade_folder / "meta.txt").read_text(encoding="utf-8")
    assert "来源：xhs（小红书）" in deg_text
    assert f"原链接：{degrade_url}" in deg_text
    assert "作者ID：无" in deg_text, f"作者缺失应记「无」，实际：\n{deg_text}"
    assert "备注：db-missing" in deg_text, "降级 note 应进 meta.txt 备注行"
    assert "图片数：2" in deg_text
    deg_files = sorted(
        p.name for p in degrade_folder.iterdir() if p.is_file() and p.name != "meta.txt"
    )
    assert deg_files == ["01.png", "02.png"]


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
async def test_scrape_queue_route_clear(biz_engine) -> None:
    """扒图页定时队列清空（批 8：路由从 /settings/scrape/queue 迁到 /scrape/queue）：
    action=clear → 队列 [] + HTMX 片段（含「定时队列已清空」提示）。"""
    from web import scrape_store
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    await scrape_store.set_link_queue(["u1", "u2"], settings=store)

    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False)
    _login(client)
    resp = client.post(
        "/scrape/queue",
        data={"action": "clear"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "定时队列已清空" in resp.text
    assert 'id="scrape-queue-card"' in resp.text, "HTMX 片段应返回队列卡片"
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


# =====================================================================
# 批 4：链路修通（详设 §5/§7/§10/§12 批 4）——A57/A58/A62/A66/A67/A68
# 真 worker 全链路模式（照 test_workers.py：嵌入式 PG 业务库+引擎库 +
# 真注册表 + TaskRunner + FakeAgent/fake connector/fake biz_client，零网络）。
#
# 基建约定：
# - biz_engine：业务库 AsyncEngine（tm_pg_cluster 嵌入式 PG，autouse 清表）
# - db_engine：引擎库 AsyncEngine（engine_pg_cluster，test_runner 跨模块 autouse 清表）
# - biz_client 真写接口路径：BizApiClient + ASGITransport 到真 web.api_biz app
#   （决策 26 引擎零业务库连接串——测试里引擎也走写接口，落库为真）
# - fake connector 只住 tests/（R12）；fake provider 只住 tests/
# - P2：URL/IP 一律运行期拼接；不读 os.environ
# =====================================================================


class _FakeBizResponse:
    """fake httpx.Response（ASGI 路径不需要——真 ASGITransport；本类供纯记录桩用）。"""

    def __init__(self, status_code: int = 200, json_body: dict | None = None) -> None:
        self.status_code = status_code
        self._json = json_body or {}

    def json(self) -> dict:
        return self._json


class _FakeConnector:
    """fake 扒图 connector（只住 tests/，零网络）：按 url 分派 失败/降级/正常。

    - 正常：返回假落盘路径（tmp 真 PNG，PIL 体检可读宽高）+ 元数据
    - fail_urls：ok=False + note 含「xsec_token 过期」（A68 失败路径）
    - degrade_urls：ok=True + note='db-missing'（A68 元数据降级路径）
    """

    def __init__(
        self,
        storage: Path,
        *,
        fail_urls: set[str] | None = None,
        degrade_urls: set[str] | None = None,
        images_per_link: int = 2,
    ) -> None:
        self._storage = Path(storage)
        self._fail_urls = fail_urls or set()
        self._degrade_urls = degrade_urls or set()
        self._images_per_link = images_per_link
        self.available = True
        self.download_calls: list[str] = []

    def _make_images(self, url: str, seq: int) -> list[str]:
        """落盘假真图（PIL 生成 80×60 纯灰 PNG：宽高可读、无水印）。"""
        from PIL import Image

        out_dir = self._storage / "xhs" / f"note{seq}"
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(self._images_per_link):
            p = out_dir / f"{i + 1:02d}.png"
            Image.new("RGB", (80, 60), (200, 200, 200)).save(p, "PNG")
            paths.append(str(p))
        return paths

    async def download(self, url: str, *, batch_id: str = "", **kwargs: object) -> Any:
        self.download_calls.append(url)
        if url in self._fail_urls:
            return ConnectorResult(
                ok=False,
                note="xsec_token 过期，请重新取最新分享链接（未落盘任何图片）",
                data={"url": url, "batch_id": batch_id},
            )
        seq = len(self.download_calls)
        if url in self._degrade_urls:
            paths = self._make_images(url, seq)
            return ConnectorResult(
                ok=True,
                note="db-missing",
                data={
                    "paths": paths,
                    "count": len(paths),
                    "desc": "",
                    "tags": [],
                    "author_id": "",
                    "day_dir": "",
                    "source": "xhs",
                    "url": url,
                },
            )
        paths = self._make_images(url, seq)
        return ConnectorResult(
            ok=True,
            note="",
            data={
                "paths": paths,
                "count": len(paths),
                "desc": "测试商品描述",
                "tags": ["tag1", "tag2"],
                "author_id": "seller-1",
                "day_dir": "",
                "source": "xhs",
                "url": url,
            },
        )


def _biz_client_for(biz_engine) -> "BizApiClient":
    """真写接口客户端（ASGITransport 到真 web.api_biz app，嵌入式 PG）。"""
    from engine.actions.biz_client import BizApiClient

    return BizApiClient(
        base_url=_FAKE_BIZ_URL,
        token=_BIZ_TOKEN,
        transport=httpx.ASGITransport(app=_biz_app(biz_engine)),
    )


def _build_runner(
    db_engine,
    *,
    biz_client,
    connectors: dict | None = None,
    providers: dict | None = None,
    agent_output=None,
    storage_dir: str | None = None,  # 批 6：EngineContext.storage_dir（归集建链接文件夹）
) -> tuple["TaskRunner", list]:
    """真注册表 + 真模型注册表 + FakeAgent + 注入 biz_client/connectors/providers。"""
    from engine.core.llm import load_models
    from engine.core.runner import TaskRunner
    from engine.registry import load_registry
    from test_runner import FakeAgent, make_agent_factory

    registry = load_registry(REPO_ROOT)
    model_registry = load_models(REPO_ROOT / "models.yaml")
    factory, agents = make_agent_factory(FakeAgent, output=agent_output)
    runner = TaskRunner(
        db_engine,
        registry,
        agent_factory=factory,
        model_registry=model_registry,
        repo_root=REPO_ROOT,
        writable_check=lambda: True,
        backoff=0.0,
        providers=providers or {},
        connectors=connectors or {},
        biz_client=biz_client,
        storage_dir=storage_dir,
    )
    return runner, agents


from typing import Any

from engine.connectors import ConnectorResult


# ==== A57：立即扒端到端（贴链接 → download 链真跑 → link_record done + 图挂链接落库）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a57_scrape_link_to_link_record_and_images(
    db_engine, biz_engine, tmp_path
) -> None:
    """A57：立即扒端到端——ScrapeChainInput{urls, batch_id, from_queue:false} →
    scrape_download_chain 真跑（fake connector 假路径文件 + biz_client 走真写接口落库）
    → link_record status=done + image_file 挂 link_record（width/height/watermark/
    source_mark='scraped'）→ 素材库读接口可见。"""
    from web import scrape_store

    fake_conn = _FakeConnector(tmp_path)
    biz_client = _biz_client_for(biz_engine)
    runner, _ = _build_runner(
        db_engine,
        biz_client=biz_client,
        connectors={"xhs": fake_conn, "xianyu": fake_conn, "http_image": fake_conn},
        storage_dir=str(tmp_path),  # 批 6：EngineContext.storage_dir（归集建链接文件夹）
    )
    result = await runner.run(
        "scrape_download_chain",
        {"urls": [_XHS_EXPLORE], "batch_id": "batch-a57", "from_queue": False},
    )
    assert result.status == "done", f"链应 DONE，实际 {result.status}: {result.error}"

    # ---- link_record 落库（一链接一条，status=done + 图数 + 元数据）----
    links = await scrape_store.get_links(limit=50)
    assert len(links) == 1, f"应只有 1 条链接记录，实际 {len(links)}"
    link = links[0]
    assert link["status"] == "done", f"链接应 done，实际 {link['status']}"
    assert link["source"] == "xhs"
    assert link["batch_id"] == "batch-a57"
    assert link["image_count"] == 2
    assert link["desc"] == "测试商品描述"
    assert link["tags"] == ["tag1", "tag2"]
    assert link["author_id"] == "seller-1"
    assert link["error_note"] is None or link["error_note"] == ""

    # ---- 批 6 一链接一文件夹（详设 §15.1）：storage_dir 指向链接文件夹 + 文件在场 ----
    assert link["storage_dir"] == "xhs/abc123", (
        f"storage_dir 应指向链接文件夹（xhs/<note id>），实际 {link['storage_dir']!r}"
    )
    link_folder = tmp_path / "xhs" / "abc123"
    assert link_folder.is_dir(), f"链接文件夹应存在：{link_folder}"
    files = sorted(p.name for p in link_folder.iterdir() if p.is_file())
    assert files == ["01.png", "02.png", "meta.txt"], (
        f"链接文件夹应含按序编号图片 + meta.txt，实际 {files}"
    )

    # ---- image_file 挂 link_record（体检字段 + 来源标记 + local_path 相对路径）----
    detail = await scrape_store.get_link_by_id(link["id"])
    imgs = detail["images"]
    assert len(imgs) == 2, f"应 2 张图挂链接，实际 {len(imgs)}"
    for img in imgs:
        assert img["link_record_id"] == link["id"], "图片应挂 link_record"
        assert img["source_mark"] == "scraped", "图片来源标记应为 scraped"
        assert img["width"] == 80 and img["height"] == 60, (
            f"PIL 内联体检应写回宽高，实际 {img['width']}×{img['height']}"
        )
        assert img["watermark"] is False, "纯灰图应无水印"
        assert img["status"] == "downloaded"
        assert img["url"].startswith(_XHS_EXPLORE), "图片 url 应基于分享链接（#img-N 稳定幂等键）"
        # 批 6 路径形态：local_path = 链接文件夹内相对路径（相对 storage_dir）
        assert img["local_path"].startswith("xhs/abc123/"), (
            f"local_path 应指向链接文件夹内相对路径，实际 {img['local_path']!r}"
        )
        assert Path(str(tmp_path), str(img["local_path"])).is_file(), (
            f"local_path 磁盘文件应存在：{img['local_path']}"
        )

    # ---- 素材库读接口可见 ----
    read_client = TestClient(_biz_app(biz_engine), raise_server_exceptions=False)
    resp = read_client.get("/api/biz/scrape/links", headers={"X-Biz-Token": _BIZ_TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["links"]) == 1
    assert body["links"][0]["status"] == "done"
    resp_img = read_client.get(
        f"/api/biz/scrape/images?link_record_id={link['id']}",
        headers={"X-Biz-Token": _BIZ_TOKEN},
    )
    assert resp_img.status_code == 200
    assert len(resp_img.json()) == 2


# ==== A58：拆两链 + input 契约各自明确（引擎侧契约校验不 422 + web 提交不 422）====


class _StubRunnerAPI:
    """A58 用最小 runner 桩（引擎 API 路径测试不应触发执行）。"""

    async def consume_once(self):  # pragma: no cover
        return None


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a58_two_chains_input_contracts(db_engine, biz_engine) -> None:
    """A58：拆两链注册——registry 有 scrape_download_chain（input.model=ScrapeChainInput，
    steps=[link_record_create, batch_image_download]）+ scrape_suggest_chain
    （input.model=SuggestionInput，steps=[product_suggestion]）；
    web POST /scrape/run {urls, batch_id} 不 422（引擎侧契约校验通过）。"""
    import json as _json

    from engine.registry import load_registry

    registry = load_registry(REPO_ROOT)

    # ---- 拆两链注册断言 ----
    dl = registry.chains["scrape_download_chain"]
    assert dl.input.model == "ScrapeChainInput"
    assert [s.worker for s in dl.steps] == ["link_record_create", "batch_image_download"]
    sg = registry.chains["scrape_suggest_chain"]
    assert sg.input.model == "SuggestionInput"
    assert [s.worker for s in sg.steps] == ["product_suggestion"]

    # ---- 引擎侧契约校验：POST /api/engine/tasks 不 422（201）----
    from engine.server import create_app
    from httpx import ASGITransport, AsyncClient

    engine_app = create_app(
        engine=db_engine,
        registry=registry,
        runner=_StubRunnerAPI(),
        consumers={},
        scheduler_enabled=False,
    )
    async with AsyncClient(
        transport=ASGITransport(app=engine_app), base_url=_FAKE_BIZ_URL
    ) as client:
        resp = await client.post(
            "/api/engine/tasks",
            json={
                "chain_id": "scrape_download_chain",
                "input": {"urls": [_XHS_EXPLORE], "batch_id": "batch-a58", "from_queue": False},
                "trigger_ref": "运营",
            },
        )
        assert resp.status_code == 201, f"download 链 input 应过 ScrapeChainInput 校验：{resp.text}"

    # ---- web POST /scrape/run 不 422（引擎桩按 ScrapeChainInput 校验，通过返回 201）----
    from models.workers import ScrapeChainInput

    seen_payloads: list[dict] = []

    def _accepting_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/engine/tasks" and request.method == "POST":
            body = _json.loads(request.content)
            seen_payloads.append(body.get("input") or {})
            # 照真引擎：链 input 过 ScrapeChainInput 校验，不过则 422
            try:
                ScrapeChainInput.model_validate(body.get("input") or {})
            except Exception:
                return httpx.Response(422, json={"detail": "input 校验失败"})
            return httpx.Response(201, json={"task_id": "e-000101", "status": "queued"})
        return httpx.Response(404, json={"detail": "not found"})

    app = _web_app(biz_engine, _accepting_handler)
    client = TestClient(app, follow_redirects=False)
    _login(client)
    resp_web = client.post("/scrape/run", json={"urls": [_XHS_EXPLORE]})
    assert resp_web.status_code == 200, f"web /scrape/run 不应 422：{resp_web.text}"
    assert resp_web.json()["ok"] is True
    payload = seen_payloads[-1]
    assert payload["urls"] == [_XHS_EXPLORE]
    assert payload["batch_id"], "web 应生成 batch_id"
    assert payload["from_queue"] is False


# ==== A62：定时队列 + 定时链跑通（种子 → from_queue 真跑 → 建记录 → 下载落库 → 清队列）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a62_schedule_queue_chain_run(db_engine, biz_engine, tmp_path) -> None:
    """A62：定时队列——设置键 link_queue 存 urls → ensure_seed_schedules 含
    scrape_download_chain 种子 → 模拟定时 input {from_queue:true, batch_id} 真跑 →
    link_record_create 经 provider（fake 读真实队列）建记录 → 下载落库 →
    消费者清队列（link_queue 键空）；done 链接重跑跳过不重下。"""
    from engine.core.db import ensure_seed_schedules, list_schedules
    from models.workers import LinkQueueData
    from web import scrape_store
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)

    # ---- ① 定时队列存 urls（设置键 scrape.link_queue）----
    await scrape_store.set_link_queue([_XHS_EXPLORE], settings=store)
    assert await scrape_store.get_link_queue(settings=store) == [_XHS_EXPLORE]

    # ---- ② 种子存在（定时扒图，cron 默认 0 7 * * *）----
    await ensure_seed_schedules(db_engine)
    rows = await list_schedules(db_engine)
    chain_ids = {r.chain_id for r in rows}
    assert "scrape_download_chain" in chain_ids, "ensure_seed_schedules 应含 scrape_download_chain 种子"
    seed = next(r for r in rows if r.chain_id == "scrape_download_chain")
    assert seed.name == "定时扒图"
    assert seed.cron == "0 7 * * *"
    assert seed.enabled is True

    # ---- ③ 模拟定时 input {from_queue: true, batch_id} 真跑 ----
    fake_conn = _FakeConnector(tmp_path)
    biz_client = _biz_client_for(biz_engine)

    async def _queue_provider(params) -> LinkQueueData:
        urls = await scrape_store.get_link_queue(settings=store)
        return LinkQueueData(urls=urls)

    runner, _ = _build_runner(
        db_engine,
        biz_client=biz_client,
        connectors={"xhs": fake_conn, "xianyu": fake_conn, "http_image": fake_conn},
        providers={"scrape.link_queue": _queue_provider},
        storage_dir=str(tmp_path),  # 批 6：EngineContext.storage_dir（归集建链接文件夹）
    )
    result = await runner.run(
        "scrape_download_chain",
        {"urls": [], "batch_id": "sched-20260903070000", "from_queue": True},
    )
    assert result.status == "done", f"定时链应 DONE：{result.error}"

    # ---- ④ 建记录 + 下载落库 ----
    links = await scrape_store.get_links(limit=50)
    assert len(links) == 1, "队列 1 条 → 1 条链接记录"
    assert links[0]["status"] == "done"
    assert links[0]["batch_id"] == "sched-20260903070000"
    assert links[0]["image_count"] == 2
    assert fake_conn.download_calls == [_XHS_EXPLORE], "connector 应下载队列链接"

    # ---- ⑤ 消费者 scrape.download_done 清队列（from_queue=true）----
    from engine.actions import CONSUMERS
    from engine.actions.scrape_download_done import consume_scrape_download_done
    from engine.core.runner import TaskRunner
    from engine.registry import load_registry
    from engine.server import QueueConsumer

    registry = load_registry(REPO_ROOT)
    consumer = QueueConsumer(
        engine=db_engine,
        registry=registry,
        runner=runner,
        consumers={**CONSUMERS, "scrape.download_done": consume_scrape_download_done},
        biz_client=biz_client,
    )
    await consumer._after_task(result)
    assert await store.get("scrape.link_queue", []) == [], "from_queue=true 链完成应清空队列"

    # ---- ⑥ done 链接重跑跳过（幂等：不重复建记录、不重下）----
    calls_before = len(fake_conn.download_calls)
    result2 = await runner.run(
        "scrape_download_chain",
        {"urls": [_XHS_EXPLORE], "batch_id": "sched-rerun", "from_queue": True},
    )
    assert result2.status == "done", result2.error
    assert len(fake_conn.download_calls) == calls_before, "done 链接应幂等跳过不重下"
    links2 = await scrape_store.get_links(limit=50)
    assert len(links2) == 1, "同 normalized_url 重跑不重复建记录"


# ==== A66：product_suggestion 诚实化（REASON 真调 LLM + SuggestionResult 校验 + 提案落库）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a66_product_suggestion_real_llm(db_engine, biz_engine) -> None:
    """A66：suggest 链真跑（fake LLM 输出 SuggestionResult）→ REASON 相位真调 LLM
    （FakeAgent 计数）+ 输出过 SuggestionResult 校验 + proposals evidence ref_id ∈
    链 input image_ids（白名单）+ 消费者 scrape.suggest 落 tm.task_proposal pending。"""
    from models.workers import ScrapeImageContextData, SuggestionResult
    from web import scrape_store
    from web.settings_store import SettingsStore

    # ---- ① 图片行落库（provider 白名单来源 = image_file.id）----
    link = await scrape_store.create_link("url-a66", "xhs", "batch-a66")
    img_ids: list[int] = []
    async with AsyncSession(biz_engine) as session, session.begin():
        for u in ("img-a66-1", "img-a66-2"):
            row = (
                await session.execute(
                    text(
                        "INSERT INTO scrape.image_file (batch_id, source, url, link_record_id) "
                        "VALUES (:b, :s, :u, :l) RETURNING id"
                    ),
                    {"b": "batch-a66", "s": "xhs", "u": u, "l": link["id"]},
                )
            ).first()
            img_ids.append(int(row[0]))

    # ---- ② fake provider：scrape_image_context 按 image_ids 返回元数据 ----
    async def _image_provider(params) -> ScrapeImageContextData:
        ids = list(getattr(params, "image_ids", None) or [])
        return ScrapeImageContextData(
            images=[
                {
                    "id": i,
                    "desc": f"测试商品{i}",
                    "tags": ["花艺", "定制"],
                    "author_id": "seller-a66",
                    "source": "xhs",
                    "url": f"https-url-{i}",
                    "width": 80,
                    "height": 60,
                    "watermark": False,
                }
                for i in ids
            ]
        )

    # ---- ③ fake LLM 输出 SuggestionResult（evidence ref_id = 链 input image_ids）----
    def _suggestion_output(out_cls):
        assert out_cls is SuggestionResult
        return SuggestionResult(
            proposals=[
                {
                    "title": "选品建议：测试商品",
                    "detail": "卖点：花艺定制；目标市场：婚礼花艺；关键词：永生花 定制",
                    "domain": "scrape",
                    "action_id": "scrape.suggest",
                    "suggested_role": "运营",
                    "suggested_due_days": 3,
                    "evidence": [{"kind": "image", "ref_id": str(img_ids[0])}],
                }
            ],
            note="",
        )

    biz_client = _biz_client_for(biz_engine)
    runner, agents = _build_runner(
        db_engine,
        biz_client=biz_client,
        providers={"scrape.image_context": _image_provider},
        agent_output=_suggestion_output,
    )
    result = await runner.run(
        "scrape_suggest_chain", {"image_ids": img_ids}
    )
    assert result.status == "done", f"suggest 链应 DONE：{result.error}"
    assert agents and agents[0].calls == 1, "REASON 相位应真调 1 次 LLM（诚实化）"
    assert "测试商品" in agents[0].prompts[0], "prompt 应含图片元数据（诚实化：元数据入 prompt）"

    # ---- ④ 链末步输出过 SuggestionResult 校验（runner VERIFY 已保证）+ 提案消费 ----
    from engine.actions import CONSUMERS
    from engine.registry import load_registry
    from engine.server import QueueConsumer

    registry = load_registry(REPO_ROOT)
    consumer = QueueConsumer(
        engine=db_engine,
        registry=registry,
        runner=runner,
        consumers=CONSUMERS,
        biz_client=biz_client,
    )
    await consumer._after_task(result)

    # ---- ⑤ tm.task_proposal pending 落库 + evidence ref_id ∈ 白名单 ----
    # （按本任务 engine_task_id 过滤——业务库跨文件共享，防其他测试残留污染断言）
    task_label = f"e-{result.task_id:06d}"
    async with AsyncSession(biz_engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, title, status, domain, evidence, source "
                    "FROM tm.task_proposal "
                    "WHERE source->>'engine_task_id' = :tid"
                ),
                {"tid": task_label},
            )
        ).all()
    assert len(rows) == 1, f"本任务应 1 条提案落库，实际 {len(rows)}"
    prop = rows[0]
    assert prop.status == "pending"
    assert prop.domain == "scrape"
    assert prop.source.get("chain_id") == "scrape_suggest_chain"
    assert prop.source.get("worker_id") == "product_suggestion"
    assert prop.source.get("audit_ids"), "LLM 工序提案应带 audit_ids（追溯保证）"
    ev = prop.evidence
    assert isinstance(ev, list) and ev and ev[0].get("ref_id") == str(img_ids[0]), (
        f"evidence ref_id 应为链 input image_id，实际 {ev}"
    )


# ==== 复核反馈修复回归（2026-09-03）：product_suggestion 代码侧规范化真实 LLM 输出 ====
# 真实 DeepSeek 输出自由格式（缺 domain/action_id/role/due_days、evidence.ref_id 数字）→
# TaskProposal 校验必拒（提案进不了审核页）；_normalize_proposals 补全 + 白名单化。


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_product_suggestion_normalizes_real_llm_output(db_engine, biz_engine) -> None:
    """真实 LLM 不完整输出（照集成真跑日志：仅 title/detail/evidence ref_id 数字，
    缺 domain/action_id/suggested_role/suggested_due_days）→ run() 代码侧补全 →
    消费者落 tm.task_proposal pending（domain=scrape + evidence ref_id str 白名单内）。"""
    from models.workers import ScrapeImageContextData, SuggestionResult
    from web import scrape_store

    link = await scrape_store.create_link("url-norm", "xhs", "batch-norm")
    img_ids: list[int] = []
    async with AsyncSession(biz_engine) as session, session.begin():
        for u in ("img-norm-1", "img-norm-2"):
            row = (
                await session.execute(
                    text(
                        "INSERT INTO scrape.image_file (batch_id, source, url, link_record_id) "
                        "VALUES (:b, :s, :u, :l) RETURNING id"
                    ),
                    {"b": "batch-norm", "s": "xhs", "u": u, "l": link["id"]},
                )
            ).first()
            img_ids.append(int(row[0]))

    async def _image_provider(params) -> ScrapeImageContextData:
        ids = list(getattr(params, "image_ids", None) or [])
        return ScrapeImageContextData(
            images=[
                {
                    "id": i, "desc": f"三层楼空装饰{i}", "tags": ["定制"],
                    "author_id": "seller-norm", "source": "xhs", "url": f"u-{i}",
                    "width": 80, "height": 60, "watermark": False,
                }
                for i in ids
            ]
        )

    def _suggestion_output(out_cls):
        """照集成真跑日志：真实 LLM 输出缺 domain/action_id/role/due_days，ref_id 是数字。"""
        assert out_cls is SuggestionResult
        return SuggestionResult(
            proposals=[
                {
                    "title": "三层楼空摆件选品建议",
                    "detail": "卖点：三层结构适合桌面陈列；目标市场：家居装饰；关键词：三层 摆件",
                    "evidence": [{"kind": "image", "ref_id": img_ids[0]}],  # 数字 ref_id
                },
                {"detail": "无标题条目应被丢弃"},  # 无 title → 宁缺勿滥丢弃
            ],
            note="",
        )

    biz_client = _biz_client_for(biz_engine)
    runner, _ = _build_runner(
        db_engine,
        biz_client=biz_client,
        providers={"scrape.image_context": _image_provider},
        agent_output=_suggestion_output,
    )
    result = await runner.run("scrape_suggest_chain", {"image_ids": img_ids})
    assert result.status == "done", f"suggest 链应 DONE：{result.error}"

    # 末步输出（engine_step）proposals 已被规范化（领域字段补全 + 丢弃无标题 + ref_id 字符串）
    from models.workers import SuggestionResult as _SR

    async with AsyncSession(db_engine) as esession:
        step_rows = (
            await esession.execute(
                text(
                    "SELECT output FROM engine_step WHERE task_id = :tid "
                    "ORDER BY step_index DESC LIMIT 1"
                ),
                {"tid": result.task_id},
            )
        ).fetchall()
    assert step_rows, "链应产出 engine_step"
    final_out = _SR.model_validate(step_rows[0][0])
    assert len(final_out.proposals) == 1, f"应丢弃无标题条目，剩 1 条，实际 {final_out.proposals}"
    prop = final_out.proposals[0]
    assert prop["domain"] == "scrape" and prop["action_id"] == "scrape.suggest"
    assert prop["suggested_role"] in ("运营", "采购", "管理员")
    assert isinstance(prop["suggested_due_days"], int)
    ev = prop["evidence"]
    assert ev and all(isinstance(e.get("ref_id"), str) for e in ev), f"ref_id 应为字符串：{ev}"
    assert all(e["ref_id"] in {str(i) for i in img_ids} for e in ev), "evidence 应在勾选图白名单内"

    # 消费者落 tm.task_proposal pending（domain=scrape + evidence 可追溯）
    from engine.actions import CONSUMERS
    from engine.registry import load_registry
    from engine.server import QueueConsumer

    registry = load_registry(REPO_ROOT)
    consumer = QueueConsumer(
        engine=db_engine,
        registry=registry,
        runner=runner,
        consumers=CONSUMERS,
        biz_client=biz_client,
    )
    await consumer._after_task(result)
    async with AsyncSession(biz_engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, status, domain, source FROM tm.task_proposal "
                    "WHERE title = '三层楼空摆件选品建议' ORDER BY id DESC LIMIT 1"
                )
            )
        ).fetchall()
    assert rows, "规范化后的建议应落 tm.task_proposal"
    assert rows[0][1] == "pending" and rows[0][2] == "scrape"


# ==== A67：EngineContext.biz_client 注入 + image_inspect 经 biz_client 读路径 + PATCH 写回 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a67_biz_client_injected_inspect_written(tmp_path) -> None:
    """A67：EngineContext.biz_client 注入（frozen dataclass 字段断言）+ image_inspect
    worker 级测试（fake biz_client GET 返回 local_path（tmp 假图文件）+ PATCH 记录调用）
    → 体检宽高/水印写回实锤。"""
    from engine.core.context import EngineContext
    from models.workers import ImageInspectInput
    from pydantic import BaseModel

    class _DummyInput(BaseModel):
        text: str = "x"

    # ---- ① EngineContext.biz_client 字段（frozen dataclass 纯追加，默认 None）----
    ctx = EngineContext(
        worker_id="w", domain="scrape", inputs=_DummyInput(), config={}, context_data={}
    )
    assert ctx.biz_client is None
    ctx2 = EngineContext(
        worker_id="w",
        domain="scrape",
        inputs=_DummyInput(),
        config={},
        context_data={},
        biz_client="fake-client",
    )
    assert ctx2.biz_client == "fake-client"
    with pytest.raises(AttributeError):
        ctx2.biz_client = "other"  # type: ignore[misc]  # frozen dataclass 不可变

    # ---- ② image_inspect worker 级（fake biz_client：GET local_path + PATCH 记录）----
    from PIL import Image

    img_path = tmp_path / "inspect.png"
    Image.new("RGB", (320, 240), (128, 128, 128)).save(img_path, "PNG")

    class _RecordingClient:
        def __init__(self) -> None:
            self.patches: list[tuple[str, dict]] = []

        async def get(self, path: str, params: dict | None = None):
            assert path == "/scrape/images"
            return _FakeBizResponse(
                200, {"images": [{"id": 7, "local_path": str(img_path)}]}
            )

        async def patch(self, path: str, payload: dict):
            self.patches.append((path, payload))
            return _FakeBizResponse(200, {"ok": True})

    from engine.registry.workers.scrape.image_inspect import run as inspect_run

    client = _RecordingClient()
    ctx3 = EngineContext(
        worker_id="image_inspect",
        domain="scrape",
        inputs=_DummyInput(),
        config={},
        context_data={},
        biz_client=client,
    )
    result = await inspect_run.run(ImageInspectInput(image_ids=[7]), ctx3)
    assert len(result.images) == 1
    item = result.images[0]
    assert item.image_id == 7
    assert item.width == 320 and item.height == 240, "PIL 应读出真宽高"
    assert item.watermark is False, "纯灰图无水印"
    assert client.patches == [
        ("/scrape/images/7", {"width": 320, "height": 240, "watermark": False})
    ], "体检结果应 PATCH 写回落库实锤"

    # ---- ③ 文件缺失记 note 不阻断（单图失败不整体失败）----
    class _MissingClient:
        async def get(self, path: str, params: dict | None = None):
            return _FakeBizResponse(200, {"images": [{"id": 8, "local_path": str(tmp_path / "gone.png")}]})

        async def patch(self, path: str, payload: dict):
            return _FakeBizResponse(200, {"ok": True})

    result2 = await inspect_run.run(
        ImageInspectInput(image_ids=[8]),
        EngineContext(
            worker_id="image_inspect",
            domain="scrape",
            inputs=_DummyInput(),
            config={},
            context_data={},
            biz_client=_MissingClient(),
        ),
    )
    assert len(result2.images) == 1
    assert "不存在" in result2.images[0].note
    assert result2.note != ""


# ==== A68：失败/降级明确提示（connector 失败 → failed+error_note；降级 → degraded_note）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a68_failure_and_degraded_notes(db_engine, biz_engine, tmp_path) -> None:
    """A68：fake connector ok=False（note 含 xsec_token 过期）→ 链接 failed + error_note
    落库；ok=True 但降级 note（db-missing）→ degraded_note 落库 → 页面/读接口可见。"""
    from web import scrape_store

    fail_url = "ht" + "tps://www.xiaohongshu.com/explore/fail123"
    degrade_url = "ht" + "tps://www.xiaohongshu.com/explore/degrade456"

    fake_conn = _FakeConnector(
        tmp_path, fail_urls={fail_url}, degrade_urls={degrade_url}
    )
    biz_client = _biz_client_for(biz_engine)
    runner, _ = _build_runner(
        db_engine,
        biz_client=biz_client,
        connectors={"xhs": fake_conn, "xianyu": fake_conn, "http_image": fake_conn},
        storage_dir=str(tmp_path),  # 批 6：EngineContext.storage_dir（归集建链接文件夹）
    )
    result = await runner.run(
        "scrape_download_chain",
        {"urls": [fail_url, degrade_url], "batch_id": "batch-a68", "from_queue": False},
    )
    assert result.status == "done", f"链接级失败不应阻断链：{result.error}"

    links = await scrape_store.get_links(limit=50)
    by_url = {l["url"]: l for l in links}
    assert set(by_url) == {fail_url, degrade_url}

    # ---- 失败链接：status=failed + error_note（xsec_token 过期/未落盘）----
    failed = by_url[fail_url]
    assert failed["status"] == "failed"
    assert "xsec_token" in (failed["error_note"] or ""), (
        f"error_note 应透传 connector 失败原因：{failed['error_note']}"
    )
    assert failed["image_count"] == 0

    # ---- 降级链接：status=done + degraded_note（db-missing）----
    degraded = by_url[degrade_url]
    assert degraded["status"] == "done"
    assert degraded["degraded_note"] == "db-missing", (
        f"connector 降级 note 应透传 degraded_note：{degraded['degraded_note']}"
    )
    assert degraded["image_count"] == 2

    # ---- 页面可见（素材库列表渲染 error_note / degraded_note）----
    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False)
    _login(client)
    page = client.get("/scrape")
    assert page.status_code == 200
    assert "xsec_token" in page.text, "页面应展示 error_note（xsec_token 过期提示）"
    assert "db-missing" in page.text, "页面应展示 degraded_note（降级标注）"


# ==== 批 4 辅助单测（非 acceptance）：消费者/调度器模板/工序降级 ====


@pytest.mark.asyncio
async def test_download_done_consumer_from_queue_false_keeps_queue() -> None:
    """scrape.download_done：from_queue=false 只记 note 不清队列（立即扒语义）。"""
    from engine.actions.scrape_download_done import consume_scrape_download_done

    cleared: list[str] = []

    class _FakeBiz:
        async def post(self, path, payload):
            cleared.append(path)
            return _FakeBizResponse(200, {"ok": True, "queue": []})

    deliverable = {
        "links": [{"link_id": 1, "status": "done"}],
        "image_ids": [10, 11],
        "from_queue": False,
        "batch_id": "batch-x",
        "note": "",
    }
    outcome = await consume_scrape_download_done(deliverable, biz_client=_FakeBiz())
    assert outcome.status == "accepted"
    assert cleared == [], "from_queue=false 不应调清队列接口"


@pytest.mark.asyncio
async def test_download_done_consumer_from_queue_true_clears() -> None:
    """scrape.download_done：from_queue=true（task.input 注入）→ POST /scrape/queue/clear。"""
    from engine.actions.scrape_download_done import consume_scrape_download_done
    from types import SimpleNamespace

    cleared: list[str] = []

    class _FakeBiz:
        async def post(self, path, payload):
            cleared.append(path)
            return _FakeBizResponse(200, {"ok": True, "queue": []})

    deliverable = {
        "links": [],
        "image_ids": [],
        "from_queue": False,  # deliverable 默认 false——task.input 是真相
        "batch_id": "sched-x",
        "note": "",
    }
    task = SimpleNamespace(input={"batch_id": "sched-x", "from_queue": True})
    outcome = await consume_scrape_download_done(deliverable, task=task, biz_client=_FakeBiz())
    assert outcome.status == "accepted"
    assert cleared == ["/scrape/queue/clear"], "from_queue=true 应调清队列接口"


def test_schedule_input_template_by_chain() -> None:
    """调度器 input 模板（T7）：scrape_download_chain → {urls: [], batch_id: sched-<ts>, from_queue: true}；
    其余链 → {trigger_date: today} 不变（crm_reminder_chain/seo_healthcheck_chain 行为锁住）。

    2026-09-03 集成验收真跑修复：定时 input 必须含 urls 键（显式空列表）——链步骤 input
    表达式 task.input.urls 缺键时 runner _path_get 引用路径不存在即抛（实测任务 failed
    「引用路径 'urls' 不存在」），置空列表让表达式解析通过；urls 实际从定时队列读。"""
    from engine.server import _schedule_input_for

    inp = _schedule_input_for("scrape_download_chain", "20260903070000", "2026-09-03")
    assert inp == {
        "urls": [],
        "batch_id": "sched-20260903070000",
        "from_queue": True,
        # 批 7（详设 §15.2）：定时扒默认同步上传夸克（未来扒的都要传，用户拍板）
        "upload_netdisk": True,
    }, f"定时 input 必须含 urls 键（链步骤表达式依赖），实际 {inp!r}"

    for chain_id in ("crm_reminder_chain", "seo_healthcheck_chain", "other_chain"):
        assert _schedule_input_for(chain_id, "ts", "2026-09-03") == {"trigger_date": "2026-09-03"}


def test_schedule_time_to_cron_conversion() -> None:
    """scrape.schedule_time（HH:MM）→ daily cron（不补前导零，照 web 口径）；非法回退默认。"""
    from engine.server import _schedule_time_to_cron

    assert _schedule_time_to_cron("07:00") == "0 7 * * *"
    assert _schedule_time_to_cron("08:30") == "30 8 * * *"
    assert _schedule_time_to_cron("bad") == "0 7 * * *"
    assert _schedule_time_to_cron("25:00") == "0 7 * * *"


@pytest.mark.asyncio
async def test_link_record_create_queue_empty_quick_complete() -> None:
    """link_record_create：from_queue=true 且队列空 → link_ids 空（链快速完成）。"""
    from engine.core.context import EngineContext
    from engine.registry.workers.scrape.link_record_create import run as lrc_run
    from models.workers import LinkQueueData, LinkRecordCreateInput

    ctx = EngineContext(
        worker_id="link_record_create",
        domain="scrape",
        inputs=LinkRecordCreateInput(urls=[], batch_id="sched-x", from_queue=True),
        config={},
        context_data={"scrape_link_queue": LinkQueueData(urls=[])},
        biz_client=object(),  # 不触发——队列空直接返回
    )
    result = await lrc_run.run(
        LinkRecordCreateInput(urls=[], batch_id="sched-x", from_queue=True), ctx
    )
    assert result.link_ids == []
    assert result.urls == []
    assert result.from_queue is True


@pytest.mark.asyncio
async def test_batch_image_download_biz_client_missing_degrades() -> None:
    """batch_image_download：biz_client 未注入 → 降级 ScrapeBatchResult（不抛穿链）。"""
    from engine.core.context import EngineContext
    from engine.registry.workers.scrape.batch_image_download import run as bid_run
    from models.workers import BatchDownloadInput

    ctx = EngineContext(
        worker_id="batch_image_download",
        domain="scrape",
        inputs=BatchDownloadInput(link_ids=[1], batch_id="b"),
        config={},
        context_data={},
    )
    result = await bid_run.run(BatchDownloadInput(link_ids=[1], batch_id="b"), ctx)
    assert result.links == []
    assert "biz_client 未注入" in result.note


# =====================================================================
# 批 5：B5 收口四断言（详设-v0.6 §10 B5 表：A29/A47/A48/A51 planned → implemented）
# =====================================================================


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a29_dismiss_candidate_no_task_no_feishu(
    biz_engine, monkeypatch
) -> None:
    """A29（v0.3 B5 收口）：候选忽略 dismissed——不建任务、不飞书（行为断言）。

    web /crm/{id}/todos/dismiss → todo_candidate status=dismissed + tm.task
    无新增（domain=crm 数不变）+ 飞书 send_task_card 零调用（monkeypatch 桩，
    决策 21：确认生成任务才推卡片，dismiss 不触发）。
    对照：现有 test_candidate_invalid_status_rejected 只测 DB CHECK 约束非行为。
    """
    from models.crm import Customer, TodoCandidate

    # ---- ① 构造客户 + pending 候选 ----
    async with AsyncSession(biz_engine) as session, session.begin():
        c = Customer(
            nickname="Dismiss测试", source_shop="成品", follow_up_status="waiting_reply"
        )
        session.add(c)
        await session.flush()
        cand = TodoCandidate(
            customer_id=c.id,
            content="忽略我",
            reason="候选理由",
            suggested_tags=["报价"],
            evidence=[{"kind": "message", "ref_id": "m-1", "quote": "hi"}],
            status="pending",
        )
        session.add(cand)
        await session.flush()
        customer_id = c.id
        cand_id = cand.id

    async with AsyncSession(biz_engine) as session:
        before = (
            await session.execute(
                text("SELECT count(*) FROM tm.task WHERE domain='crm'")
            )
        ).scalar_one()

    # ---- ② 飞书零调用桩 ----
    sent: list[dict] = []

    async def _fake_send(view: dict) -> bool:
        sent.append(view)
        return True

    monkeypatch.setattr("web.app.send_task_card", _fake_send)

    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False)
    _login(client)

    # ---- ③ POST /crm/{id}/todos/dismiss ----
    resp = client.post(
        f"/crm/{customer_id}/todos/dismiss",
        data={"candidate_ids": [str(cand_id)]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"/crm/{customer_id}" in resp.headers["location"]

    # ---- ④ DB：candidate status=dismissed（含 dismissed_at，不回填任务）----
    async with AsyncSession(biz_engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, dismissed_at, confirmed_task_id "
                    "FROM crm.todo_candidate WHERE id = :id"
                ),
                {"id": cand_id},
            )
        ).first()
        assert row is not None, "候选应存在"
        assert row[0] == "dismissed", f"候选应 dismissed，实际 {row[0]}"
        assert row[1] is not None, "dismissed_at 应记录"
        assert row[2] is None, "dismissed 候选不应回填任务 id"

        # ---- ⑤ tm.task 无新增（domain=crm 数不变）----
        after = (
            await session.execute(
                text("SELECT count(*) FROM tm.task WHERE domain='crm'")
            )
        ).scalar_one()
        assert after == before, f"dismiss 不应创建任务（{before} -> {after}）"

    # ---- ⑥ 飞书零调用 ----
    assert sent == [], f"dismiss 不应触发飞书卡片，实际 {len(sent)} 次调用"


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a47_link_to_image_file_and_proposal_pending(
    db_engine, biz_engine, tmp_path
) -> None:
    """A47（v0.5 B5 收口）：丢链接进扒图 → 下载链真跑 → scrape.image_file 落库
    → 勾图 → suggest 链真跑 → 选品提案进审核页（tm.task_proposal status=pending）。

    组合断言（照 A57 立即扒落库 + A66 suggest 链真跑落提案模式）：fake connector
    落盘真 PNG + biz_client 走真写接口（ASGITransport 到真 web.api_biz）+ provider
    用真 HTTP 实现（决策 26 读取接口化，链路从 DB 真读元数据）+ fake LLM 输出
    SuggestionResult——零网络真链路。"""
    from engine.providers import build_providers
    from models.workers import SuggestionResult
    from web import scrape_store

    # ---- ① 贴链接 → 下载链真跑（link_record + image_file 挂链接落库）----
    fake_conn = _FakeConnector(tmp_path)
    biz_client = _biz_client_for(biz_engine)
    runner, _ = _build_runner(
        db_engine,
        biz_client=biz_client,
        connectors={"xhs": fake_conn, "xianyu": fake_conn, "http_image": fake_conn},
        storage_dir=str(tmp_path),  # 批 6：EngineContext.storage_dir（归集建链接文件夹）
    )
    result = await runner.run(
        "scrape_download_chain",
        {"urls": [_XHS_EXPLORE], "batch_id": "batch-a47", "from_queue": False},
    )
    assert result.status == "done", f"下载链应 DONE：{result.error}"

    links = await scrape_store.get_links(limit=50)
    assert len(links) == 1, "应 1 条链接记录"
    link = links[0]
    assert link["status"] == "done", f"链接应 done，实际 {link['status']}"
    assert link["image_count"] == 2
    detail = await scrape_store.get_link_by_id(link["id"])
    imgs = detail["images"]
    assert len(imgs) == 2, "下载链应落 2 张图（image_file 挂链接）"
    for img in imgs:
        assert img["link_record_id"] == link["id"], "图片应挂 link_record"
        assert img["source_mark"] == "scraped"
    img_ids = [img["id"] for img in imgs]

    # ---- ② 勾图 → suggest 链真跑（真 provider 读落库图片元数据 + fake LLM）----
    def _suggestion_output(out_cls):
        assert out_cls is SuggestionResult
        return SuggestionResult(
            proposals=[
                {
                    "title": "选品建议：测试商品",
                    "detail": "卖点：花艺定制；目标市场：婚礼花艺；关键词：永生花 定制",
                    "domain": "scrape",
                    "action_id": "scrape.suggest",
                    "suggested_role": "运营",
                    "suggested_due_days": 3,
                    "evidence": [{"kind": "image", "ref_id": str(img_ids[0])}],
                }
            ],
            note="",
        )

    providers = build_providers(
        base_url=_FAKE_BIZ_URL,
        token=_BIZ_TOKEN,
        transport=httpx.ASGITransport(app=_biz_app(biz_engine)),
    )
    runner2, agents = _build_runner(
        db_engine,
        biz_client=biz_client,
        providers=providers,
        agent_output=_suggestion_output,
    )
    result2 = await runner2.run("scrape_suggest_chain", {"image_ids": img_ids})
    assert result2.status == "done", f"suggest 链应 DONE：{result2.error}"
    assert agents and agents[0].calls == 1, "REASON 相位应真调 LLM（诚实化）"
    assert "测试商品" in agents[0].prompts[0], "prompt 应含落库图片元数据（诚实化）"

    # ---- ③ 提案进审核页（tm.task_proposal pending，evidence ref_id ∈ image_ids）----
    from engine.actions import CONSUMERS
    from engine.registry import load_registry
    from engine.server import QueueConsumer

    registry = load_registry(REPO_ROOT)
    consumer = QueueConsumer(
        engine=db_engine,
        registry=registry,
        runner=runner2,
        consumers=CONSUMERS,
        biz_client=biz_client,
    )
    await consumer._after_task(result2)

    task_label = f"e-{result2.task_id:06d}"
    async with AsyncSession(biz_engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, title, status, domain, evidence, source "
                    "FROM tm.task_proposal WHERE source->>'engine_task_id' = :tid"
                ),
                {"tid": task_label},
            )
        ).all()
    match = [
        r
        for r in rows
        if r.domain == "scrape"
        and isinstance(r.evidence, list)
        and r.evidence
        and r.evidence[0].get("ref_id") == str(img_ids[0])
    ]
    assert len(match) == 1, (
        f"本任务应 1 条提案（evidence ref_id=本批图片）落库，实际 {len(match)}"
        f"（同 engine_task_id 共 {len(rows)} 行）"
    )
    prop = match[0]
    assert prop.status == "pending", "提案应进审核页（status=pending）"
    assert prop.domain == "scrape"
    assert prop.source.get("chain_id") == "scrape_suggest_chain"
    assert prop.source.get("worker_id") == "product_suggestion"
    assert prop.source.get("audit_ids"), "LLM 工序提案应带 audit_ids（追溯保证）"


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a48_proposal_approve_task_domain_scrape(biz_engine) -> None:
    """A48（v0.5 B5 收口）：选品提案批准 → tm.task(domain=scrape) 落 TM 清单
    （来源徽章「扒图」bg-orange）。

    复用 v0.2 审核流模式（test_tm_proposal/test_web_tm）：构造 tm.task_proposal
    （domain=scrape，照 scrape.suggest 产出形态）→ web 批准 → tm.task 落库
    （domain=scrape，source_type=ai）→ TM 清单渲染「扒图」徽章（DOMAIN_META）。
    """
    import json as _json

    from web.tm_store import proposal_display_id

    # ---- ① 构造 tm.task_proposal（domain=scrape，照 scrape.suggest 产出形态）----
    async with AsyncSession(biz_engine) as session, session.begin():
        pid = (
            await session.execute(
                text(
                    "INSERT INTO tm.task_proposal "
                    "(title, detail, domain, action_id, risk, suggested_role, "
                    " suggested_due_days, evidence, source) "
                    "VALUES (:title, :detail, :domain, :action_id, :risk, :role, "
                    " :due, CAST(:evidence AS jsonb), CAST(:source AS jsonb)) "
                    "RETURNING id"
                ),
                {
                    "title": "选品建议：永生花花束",
                    "detail": "卖点：花艺定制；目标市场：婚礼花艺",
                    "domain": "scrape",
                    "action_id": "scrape.suggest",
                    "risk": "suggest",
                    "role": "运营",
                    "due": 3,
                    "evidence": _json.dumps(
                        [{"kind": "image", "ref_id": "101", "quote": "图片 101"}]
                    ),
                    "source": _json.dumps(
                        {
                            "chain_id": "scrape_suggest_chain",
                            "engine_task_id": "e-a48",
                            "worker_id": "product_suggestion",
                            "audit_ids": [],
                        }
                    ),
                },
            )
        ).scalar_one()

    # ---- ② web 批准 → tm.task 落库（domain=scrape，source_type=ai）----
    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False)
    _login(client)
    resp = client.post(
        "/tasks/proposals/approve",
        data={"proposal_id": str(pid)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with AsyncSession(biz_engine) as session:
        tasks = (
            await session.execute(
                text(
                    "SELECT id, title, domain, source_type, source "
                    "FROM tm.task WHERE domain='scrape' "
                    "AND source->>'proposal_id' = :pid"
                ),
                {"pid": proposal_display_id(pid)},
            )
        ).all()
        prop = (
            await session.execute(
                text("SELECT status, task_id FROM tm.task_proposal WHERE id=:id"),
                {"id": pid},
            )
        ).first()
    assert len(tasks) == 1, f"应 1 条 domain=scrape 任务落库，实际 {len(tasks)}"
    t = tasks[0]
    assert t.domain == "scrape", "AI 任务应继承提案 domain"
    assert t.source_type == "ai"
    assert t.source.get("chain_id") == "scrape_suggest_chain", "来源追溯应保留"
    assert prop is not None and prop[0] == "approved", "提案应 approved"
    assert prop[1] is not None, "提案应回填 task_id"

    # ---- ③ TM 清单渲染来源徽章「扒图」（DOMAIN_META scrape = bg-orange）----
    page = client.get("/tasks")
    assert page.status_code == 200
    assert "选品建议：永生花花束" in page.text, "TM 清单应展示 scrape 域任务"
    assert 'bg-orange">扒图' in page.text, "TM 清单应渲染来源徽章「扒图」（bg-orange）"


@pytest.mark.version_acceptance
def test_a51_seo_optimize_spec_config_change() -> None:
    """A51（v0.5 B5 收口）：seo-optimize 规格（titles 3-5 / tags≤13 / ≤20 字符）
    外置 config/spec.yaml 可改、改后归一化行为变化实锤（R10 规格外置）。

    ①默认规格断言：读 config/spec.yaml（titles min 3/max 5、tags max 13、
      tag max_length 20）；②默认 config（spec 外置值）→ 行为 = 默认规格
      （fake LLM 输出超限 tags/标题 → 按默认规格截断/封顶）；③临时注入改后
      spec（tags 13→8、长度 20→5、标题 5→3、材质 13→4）→ 工序归一化行为
      按新规格实锤（不改真实 spec.yaml 提交值）；④config 无 spec 回退默认。
    """
    import yaml as _yaml

    from engine.core.context import EngineContext
    from engine.registry.workers.seo.seo_optimize.run import run
    from models.workers import SeoOptimizeInput, SeoOptimizationReport, SeoProductText

    spec_path = (
        REPO_ROOT
        / "engine"
        / "registry"
        / "workers"
        / "seo"
        / "seo_optimize"
        / "config"
        / "spec.yaml"
    )
    assert spec_path.is_file(), "seo_optimize 规格应外置在 config/spec.yaml（R10）"

    # ---- ① 默认规格断言（读 config/spec.yaml：titles 3-5 / tags≤13 / ≤20 字符）----
    spec = _yaml.safe_load(spec_path.read_text(encoding="utf-8"))["spec"]
    assert spec["titles"]["min"] == 3 and spec["titles"]["max"] == 5
    assert spec["tags"]["max"] == 13
    assert spec["tags"]["max_length"] == 20

    def _make_ctx(config: dict) -> EngineContext:
        """fake LLM 输出超限样本（7 标题 / 16 标签（含 30 字符长标签）/ 20 材质 /
        300 字符 alt / 20 关键词）——归一化必须按规格截断/封顶。"""
        return EngineContext(
            worker_id="seo_optimize",
            domain="seo",
            inputs=SeoOptimizeInput(
                product_text=SeoProductText(
                    title="Test", tags=["test"], description="D"
                )
            ),
            config=config,
            context_data={},
            llm_output={
                "titles": [
                    {"title": f"标题{i}", "angle": "Main"} for i in range(7)
                ],
                "tags": ["x" * 30] + [f"tag{i}" for i in range(15)],
                "listing_description": "desc",
                "materials": [f"m{i}" for i in range(20)],
                "alt_text": "A" * 300,
                "seo_keywords": [f"kw{i}" for i in range(20)],
            },
        )

    inputs = SeoOptimizeInput(
        product_text=SeoProductText(title="Test", tags=["test"], description="D")
    )

    # ---- ② 默认 config（spec 外置值）→ 行为 = 默认规格 ----
    default = run(inputs, _make_ctx({"spec": spec}))
    assert isinstance(default, SeoOptimizationReport)
    assert len(default.titles) == 5, "默认规格标题上限 5"
    assert all(len(t["title"]) <= 140 for t in default.titles)
    assert len(default.tags) == 13, f"默认规格标签上限 13，实际 {len(default.tags)}"
    assert all(len(t) <= 20 for t in default.tags), "默认规格标签每条 ≤20 字符"
    assert len(default.materials) == 13
    assert len(default.seo_keywords) == 10

    # ---- ③ 改 spec（临时注入，不改真实提交值）→ 行为按新规格实锤 ----
    modified = {
        "spec": {
            **spec,
            "tags": {"max": 8, "max_length": 5},
            "titles": {"min": 3, "max": 3, "max_length": 140},
            "materials": {"max": 4, "max_length": 50},
        }
    }
    changed = run(inputs, _make_ctx(modified))
    assert len(changed.tags) == 8, (
        f"改 tags.max=8 后应只保留 8 个标签，实际 {len(changed.tags)}"
    )
    assert all(len(t) <= 5 for t in changed.tags), (
        "改 tags.max_length=5 后标签每条应 ≤5 字符"
    )
    assert len(changed.titles) == 3, (
        f"改 titles.max=3 后应只保留 3 个标题，实际 {len(changed.titles)}"
    )
    assert len(changed.materials) == 4, (
        f"改 materials.max=4 后应只保留 4 个材质，实际 {len(changed.materials)}"
    )

    # ---- ④ config 无 spec 回退默认（旧调用 config={} 行为不变）----
    fallback = run(inputs, _make_ctx({}))
    assert len(fallback.tags) == 13
    assert len(fallback.titles) == 5


# =====================================================================
# 批 7（详设 §15.2/§15.4/§15.6）：夸克网盘上传——A70 / A71 / A72 + 辅助单测
# =====================================================================
# fake quark 桩只住 tests/（R12）：_FakeQuarkConnector（connector 级注入）
# + subprocess monkeypatch（web quark 工具 / connector 内部调用）——零真实
# 夸克 CLI/网络调用（详设 §15.6：真实授权延后 manual 项）。
# 测试 URL 运行期拼接（P2：单一字符串常量不得含完整 scheme）。

# 假分享链接（P2：scheme 运行期拼接）
_FAKE_SHARE_URL = "ht" + "tps://pan.quark.cn/s/liuquanfake"
# 第二条下载链接（A70 未勾选分支用：normalized_url 与 _XHS_EXPLORE 不同）
_XHS_OTHER = "ht" + "tps://www.xiaohongshu.com/explore/def456"


class _FakeQuarkConnector:
    """fake 夸克 connector（只住 tests/，零真实 CLI/网络）。

    upload_folder 记录 (local_path, sub_folder) 调用；ok 可配——
    ok=False（未授权 -1408 语义）→ note 含「夸克未授权，请在 设置 → 扒图设置
    完成登录」（A71 失败路径）；ok=True → 返回 share_url（A70 成功路径）。
    """

    def __init__(
        self,
        *,
        ok: bool = True,
        note: str = "",
        share_url: str | None = None,
    ) -> None:
        self.available = True
        self._ok = ok
        self._note = note
        self._share_url = share_url or _FAKE_SHARE_URL
        self.upload_calls: list[tuple[str, str]] = []

    async def upload_folder(
        self, local_path: str, sub_folder: str
    ) -> ConnectorResult:
        self.upload_calls.append((str(local_path), str(sub_folder)))
        if not self._ok:
            return ConnectorResult(ok=False, note=self._note)
        return ConnectorResult(
            ok=True,
            note="",
            data={"share_url": self._share_url, "fids": ["fid-1", "fid-2"]},
        )


async def _run_download_chain_and_consume(
    db_engine,
    biz_engine,
    tmp_path,
    *,
    urls: list,
    batch_id: str,
    upload_netdisk: bool,
    connectors: dict,
) -> Any:
    """跑 download 链（注入 fake connector）→ QueueConsumer._after_task 触发
    链完成消费者（scrape.download_done，含上传执行）。返回 (result, consumer)。"""
    from engine.actions import CONSUMERS
    from engine.actions.scrape_download_done import consume_scrape_download_done
    from engine.registry import load_registry
    from engine.server import QueueConsumer

    fake_conn = _FakeConnector(tmp_path)
    biz_client = _biz_client_for(biz_engine)
    runner, _ = _build_runner(
        db_engine,
        biz_client=biz_client,
        connectors=connectors,
        storage_dir=str(tmp_path),
    )
    result = await runner.run(
        "scrape_download_chain",
        {
            "urls": urls,
            "batch_id": batch_id,
            "from_queue": False,
            "upload_netdisk": upload_netdisk,
        },
    )
    assert result.status == "done", f"下载链应 DONE：{result.error}"
    consumer = QueueConsumer(
        engine=db_engine,
        registry=load_registry(REPO_ROOT),
        runner=runner,
        consumers={**CONSUMERS, "scrape.download_done": consume_scrape_download_done},
        biz_client=biz_client,
    )
    await consumer._after_task(result)
    return result


# ==== A70（批 7，详设 §15.4）：上传网盘 flow（勾选上传 / 未勾选不传）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a70_netdisk_upload_flow(
    db_engine, biz_engine, tmp_path
) -> None:
    """A70：download 链跑完（fake connector 落盘 + fake quark connector 注入）
    → upload_netdisk=true → 链完成消费者上传执行 → link_record.netdisk_status=
    uploaded + netdisk_url 回填 + netdisk_uploaded_at 落库 + 文件夹 meta.txt 网盘
    分享链接行更新为 share_url（PATCH handler 重写）；
    upload_netdisk=false → netdisk_status=none 且零 quark 调用。"""
    from web import scrape_store
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    await store.set("scrape.storage_dir", str(tmp_path), "扒图存储目录")

    # ---- ① upload_netdisk=true：链跑完 → 消费者上传 → 回填 uploaded ----
    fake_quark = _FakeQuarkConnector()
    connectors = {
        "xhs": _FakeConnector(tmp_path),
        "xianyu": _FakeConnector(tmp_path),
        "http_image": _FakeConnector(tmp_path),
        "quark": fake_quark,
    }
    await _run_download_chain_and_consume(
        db_engine,
        biz_engine,
        tmp_path,
        urls=[_XHS_EXPLORE],
        batch_id="batch-a70-on",
        upload_netdisk=True,
        connectors=connectors,
    )

    links = await scrape_store.get_links(limit=50)
    link = next(l for l in links if l["batch_id"] == "batch-a70-on")
    assert link["status"] == "done"
    assert link["netdisk_status"] == "uploaded", (
        f"upload_netdisk=true 链完成应回填 netdisk_status=uploaded，实际 {link['netdisk_status']!r}"
    )
    assert link["netdisk_url"] == _FAKE_SHARE_URL, (
        f"netdisk_url 应回填分享链接，实际 {link['netdisk_url']!r}"
    )
    assert link["netdisk_uploaded_at"] is not None, "上传成功应回填 netdisk_uploaded_at"
    assert link["storage_dir"] == "xhs/abc123"

    # quark 调用实锤：上传本地链接文件夹到网盘「扒图素材/小红书」子目录
    assert len(fake_quark.upload_calls) == 1, (
        f"应恰 1 次 quark 上传调用，实际 {fake_quark.upload_calls}"
    )
    local_path, sub_folder = fake_quark.upload_calls[0]
    assert local_path == str((tmp_path / "xhs" / "abc123").resolve()), (
        f"上传目录应为链接文件夹，实际 {local_path}"
    )
    assert sub_folder == "小红书", f"xhs 链接应上传到「扒图素材/小红书」，实际 {sub_folder!r}"

    # meta.txt 网盘行更新为 share_url（PATCH handler 重写实锤）
    meta_text = (tmp_path / "xhs" / "abc123" / "meta.txt").read_text(encoding="utf-8")
    assert f"网盘分享链接：{_FAKE_SHARE_URL}" in meta_text, (
        f"meta.txt 网盘分享链接行应回填 share_url，实际：\n{meta_text}"
    )
    assert "网盘分享链接：未上传" not in meta_text, "meta.txt 网盘行不应再是「未上传」"

    # ---- ② upload_netdisk=false：新链接跑完 → netdisk_status=none + 零 quark 调用 ----
    calls_before = len(fake_quark.upload_calls)
    await _run_download_chain_and_consume(
        db_engine,
        biz_engine,
        tmp_path,
        urls=[_XHS_OTHER],
        batch_id="batch-a70-off",
        upload_netdisk=False,
        connectors=connectors,
    )
    links2 = await scrape_store.get_links(limit=50)
    link_off = next(l for l in links2 if l["batch_id"] == "batch-a70-off")
    assert link_off["netdisk_status"] == "none", (
        f"未勾选 upload_netdisk → netdisk_status 应保持 none，实际 {link_off['netdisk_status']!r}"
    )
    assert len(fake_quark.upload_calls) == calls_before, (
        "未勾选 upload_netdisk 不应触发任何 quark 上传调用"
    )
    meta_off = (tmp_path / "xhs" / "def456" / "meta.txt").read_text(encoding="utf-8")
    assert "网盘分享链接：未上传" in meta_off, (
        f"未勾选上传时 meta.txt 网盘行应保持「未上传」：\n{meta_off}"
    )


# ==== A71（批 7，详设 §15.4）：未授权/上传失败 → failed + error_note + 页面可见 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a71_netdisk_failure_note(
    db_engine, biz_engine, tmp_path
) -> None:
    """A71：fake quark ok=False（-1408 未授权语义）→ upload_netdisk=true 链完成
    → link_record.netdisk_status=failed + error_note 含「夸克」与登录提示
    （页面可见：素材库读接口 + 列表页网盘失败徽章）。"""
    from web import scrape_store
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    await store.set("scrape.storage_dir", str(tmp_path), "扒图存储目录")

    fake_quark = _FakeQuarkConnector(
        ok=False,
        note="夸克未授权，请在 设置 → 扒图设置 完成登录",
    )
    connectors = {
        "xhs": _FakeConnector(tmp_path),
        "xianyu": _FakeConnector(tmp_path),
        "http_image": _FakeConnector(tmp_path),
        "quark": fake_quark,
    }
    await _run_download_chain_and_consume(
        db_engine,
        biz_engine,
        tmp_path,
        urls=[_XHS_EXPLORE],
        batch_id="batch-a71",
        upload_netdisk=True,
        connectors=connectors,
    )

    links = await scrape_store.get_links(limit=50)
    link = next(l for l in links if l["batch_id"] == "batch-a71")
    assert link["netdisk_status"] == "failed", (
        f"未授权上传应回填 netdisk_status=failed，实际 {link['netdisk_status']!r}"
    )
    note = link["error_note"] or ""
    assert "夸克" in note, f"error_note 应含「夸克」提示，实际 {note!r}"
    assert "设置 → 扒图设置" in note or "登录" in note, (
        f"error_note 应提示夸克登录入口，实际 {note!r}"
    )
    # 上传失败不阻断下载链本身（链接 status 仍 done）
    assert link["status"] == "done"

    # ---- 页面/读接口可见 ----
    read_client = TestClient(_biz_app(biz_engine), raise_server_exceptions=False)
    resp = read_client.get("/api/biz/scrape/links", headers={"X-Biz-Token": _BIZ_TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    row = next(l for l in body["links"] if l["batch_id"] == "batch-a71")
    assert row["netdisk_status"] == "failed", "素材库读接口应可见 netdisk_status=failed"

    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _login(client)
    page = client.get("/scrape")
    assert page.status_code == 200
    assert "上传失败" in page.text, "素材库列表页应展示网盘状态徽章「上传失败」"
    assert "夸克" in page.text, "素材库列表页应可见夸克上传失败提示"


# ==== A72（批 7，详设 §15.4）：历史补传入口（按钮 + POST /scrape/links/{id}/upload）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a72_history_upload_entry(biz_engine) -> None:
    """A72：素材库链接行「上传网盘」按钮——status=done 且 netdisk_status≠uploaded
    时显示（DOM/HTML 断言），已上传（uploaded）行隐藏；POST /scrape/links/{id}/upload
    → 先 PATCH netdisk_status=pending → create_task(scrape_upload_chain,
    {link_ids: [id]})（mock 引擎客户端断言入参）；uploaded/非 done 链接拒绝。"""
    import json as _json

    from web import scrape_store

    # ---- ① 造两条 done 链接：一条未上传、一条已上传 ----
    l1 = await scrape_store.create_link("ht" + "tps://www.xiaohongshu.com/explore/aaa111", "xhs", "batch-a72")
    l2 = await scrape_store.create_link("ht" + "tps://www.xiaohongshu.com/explore/bbb222", "xhs", "batch-a72")
    l1 = await scrape_store.update_link(l1["id"], status="done", image_count=2)
    l2 = await scrape_store.update_link(
        l2["id"],
        status="done",
        image_count=2,
        netdisk_status="uploaded",
        netdisk_url=_FAKE_SHARE_URL,
    )
    assert l1 is not None and l2 is not None
    link1_id, link2_id = l1["id"], l2["id"]

    # ---- ② 素材库列表页：done 未上传行显示「上传网盘」，uploaded 行隐藏 ----
    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _login(client)
    page = client.get("/scrape")
    assert page.status_code == 200
    assert f'data-link-id="{link1_id}"' in page.text, (
        f"done 未上传链接行应渲染「上传网盘」按钮（data-link-id={link1_id}）"
    )
    assert f'data-link-id="{link2_id}"' not in page.text, (
        f"uploaded 链接行不应渲染「上传网盘」按钮（data-link-id={link2_id}）"
    )
    assert page.text.count("btn-netdisk-upload") == 1, (
        f"仅未上传链接行应有「上传网盘」按钮（btn-netdisk-upload），"
        f"实际出现 {page.text.count('btn-netdisk-upload')} 次"
    )
    assert _FAKE_SHARE_URL in page.text, "uploaded 链接行应展示可点的网盘分享链接"

    # ---- ③ POST /scrape/links/{id}/upload：pending 前置 + create_task 入参实锤 ----
    created: list[dict] = []

    def _upload_engine_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/engine/tasks" and request.method == "POST":
            body = _json.loads(request.content)
            created.append(
                {"chain_id": body.get("chain_id"), "input": body.get("input"),
                 "trigger_ref": body.get("trigger_ref")}
            )
            return httpx.Response(201, json={"task_id": "e-000200", "chain_id": "scrape_upload_chain"})
        if request.url.path == "/api/engine/registry":
            return httpx.Response(200, json={"chains": []})
        return httpx.Response(404, json={"detail": "not found"})

    app2 = _web_app(biz_engine, _upload_engine_handler)
    client2 = TestClient(app2, follow_redirects=False, raise_server_exceptions=False)
    _login(client2)
    resp = client2.post(f"/scrape/links/{link1_id}/upload")
    assert resp.status_code == 200, f"补传接口应 200，实际 {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("ok") is True
    assert len(created) == 1, f"应 1 次 create_task，实际 {created}"
    assert created[0]["chain_id"] == "scrape_upload_chain", (
        f"补传应创建 scrape_upload_chain，实际 {created[0]['chain_id']}"
    )
    assert created[0]["input"] == {"link_ids": [link1_id]}, (
        f"补传 input 应为 {{link_ids: [id]}}，实际 {created[0]['input']}"
    )

    # pending 前置落库（页面「上传中」徽章依据）
    after = await scrape_store.get_link_by_id(link1_id)
    assert after["link"]["netdisk_status"] == "pending", (
        f"点击补传应先置 netdisk_status=pending，实际 {after['link']['netdisk_status']!r}"
    )

    # ---- ④ 已上传链接拒绝补传（不重复建任务）----
    resp3 = client2.post(f"/scrape/links/{link2_id}/upload")
    assert resp3.status_code in (400, 409), (
        f"uploaded 链接补传应被拒，实际 {resp3.status_code}"
    )
    assert len(created) == 1, "uploaded 链接不应再 create_task"

    # ---- ⑤ 非 done 链接拒绝补传 ----
    lp = await scrape_store.create_link("ht" + "tps://www.xiaohongshu.com/explore/ccc333", "xhs", "batch-a72")
    resp4 = client2.post(f"/scrape/links/{lp['id']}/upload")
    assert resp4.status_code in (400, 409), (
        f"非 done 链接补传应被拒，实际 {resp4.status_code}"
    )
    assert len(created) == 1, "非 done 链接不应再 create_task"


# ==== 批 7 辅助单测：quark connector 子进程序列 / 设置页夸克登录块 ====


def test_quark_connector_upload_sequence_ok(tmp_path, monkeypatch) -> None:
    import asyncio
    """quark connector：create-folder（顶层+子目录）→ upload → share 全命令序列
    （subprocess 全 mock，零真实调用）→ ConnectorResult ok + share_url/fids 解析。"""
    import json as _json
    import subprocess as _sp

    from engine.connectors.quark import QuarkConnector

    calls: list[list[str]] = []
    fid_counter = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> _sp.CompletedProcess:
        calls.append(cmd)
        name = cmd[1] if len(cmd) > 1 else ""
        if name == "create-folder":
            fid_counter["n"] += 1
            data = {"fid": f"fid-root-{fid_counter['n']}"}
            line = _json.dumps({"code": 0, "msg": "ok", "type": "result", "data": data})
        elif name == "upload":
            data = {
                "successCount": 2,
                "instantUpload": False,
                "fids": ["file-fid-1", "file-fid-2"],
            }
            line = _json.dumps({"code": 0, "msg": "ok", "type": "result", "data": data})
        elif name == "share":
            data = {"share_url": _FAKE_SHARE_URL, "url_type": 1, "expired_type": 1}
            line = _json.dumps({"code": 0, "msg": "ok", "type": "result", "data": data})
        else:
            line = _json.dumps({"code": -1408, "msg": "未登录", "type": "result", "data": {}})
        return _sp.CompletedProcess(cmd, 0, line + "\n", "")

    monkeypatch.setattr(_sp, "run", _fake_run)
    (tmp_path / "quark.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    conn = QuarkConnector(script_path=str(tmp_path / "quark.sh"))
    result = asyncio.run(conn.upload_folder("/local/folder", "小红书"))
    assert result.ok is True, f"应成功：{result.note}"
    assert (result.data or {}).get("share_url") == _FAKE_SHARE_URL
    assert (result.data or {}).get("fids") == ["file-fid-1", "file-fid-2"]

    # 命令序列：create-folder（扒图素材 + 小红书）→ upload → share（url-type/expired-type 1）
    names = [c[1] for c in calls]
    assert names == ["create-folder", "create-folder", "upload", "share"], names
    assert "--parent-fid" in calls[0] and "0" in calls[0], "顶层目录父 fid 应为根（0）"
    assert "share" in calls[3] and "--url-type" in calls[3] and "--expired-type" in calls[3]
    assert calls[3][calls[3].index("--url-type") + 1] == "1"
    assert calls[3][calls[3].index("--expired-type") + 1] == "1"
    # 公共 session 参数（照广成 quark_upload.py run_quark 全量形态）
    assert all("--session-input" in c and "--session-id" in c for c in calls)


def test_quark_connector_upload_unauthorized_note(tmp_path, monkeypatch) -> None:
    import asyncio
    """quark connector：-1408 未授权（首个命令即失败）→ ok=False + note 含
    「夸克未授权，请在 设置 → 扒图设置 完成登录」提示。"""
    import json as _json
    import subprocess as _sp

    from engine.connectors.quark import QuarkConnector

    def _fake_run(cmd: list[str], **kwargs: object) -> _sp.CompletedProcess:
        line = _json.dumps(
            {"code": -1408, "msg": "登录状态已失效", "type": "result", "data": {}}
        )
        return _sp.CompletedProcess(cmd, 0, line + "\n", "")

    monkeypatch.setattr(_sp, "run", _fake_run)
    (tmp_path / "quark.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    conn = QuarkConnector(script_path=str(tmp_path / "quark.sh"))
    result = asyncio.run(conn.upload_folder("/local/folder", "小红书"))
    assert result.ok is False
    assert "夸克未授权" in result.note
    assert "设置 → 扒图设置" in result.note, f"note 应提示登录入口：{result.note}"


def test_quark_connector_login_status(tmp_path, monkeypatch) -> None:
    import asyncio
    """quark connector login_status()：get-user-info code=0 → ok=True；
    -1408 → ok=False（未授权提示）；工具路径缺失 available=False。"""
    import json as _json
    import subprocess as _sp

    from engine.connectors.quark import QuarkConnector

    mode = {"v": "ok"}

    def _fake_run(cmd: list[str], **kwargs: object) -> _sp.CompletedProcess:
        if mode["v"] == "ok":
            line = _json.dumps(
                {"code": 0, "msg": "ok", "type": "result",
                 "data": {"nickname": "测试用户"}}
            )
        else:
            line = _json.dumps(
                {"code": -1408, "msg": "未登录", "type": "result", "data": {}}
            )
        return _sp.CompletedProcess(cmd, 0, line + "\n", "")

    monkeypatch.setattr(_sp, "run", _fake_run)
    (tmp_path / "quark.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    conn = QuarkConnector(script_path=str(tmp_path / "quark.sh"))
    ok_res = asyncio.run(conn.login_status())
    assert ok_res.ok is True
    mode["v"] = "unauthorized"
    bad_res = asyncio.run(conn.login_status())
    assert bad_res.ok is False
    assert "夸克未授权" in bad_res.note

    # 工具路径缺失：available=False（纯文件系统判断，不触子进程）
    conn2 = QuarkConnector(script_path=str(tmp_path / "no-quark.sh"))
    assert conn2.available is False


@pytest.mark.asyncio
async def test_web_scrape_quark_settings_block(biz_engine, monkeypatch) -> None:
    """设置 → 扒图设置 夸克登录块：页面含块与文案；授权码登录/检测/退出路由
    （web 侧 subprocess 执行 quark.sh，测试 mock subprocess 零真实调用）；
    执行完不回显授权码（R20 精神不落库不落盘）。"""
    import json as _json
    import subprocess as _sp

    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)

    calls: list[list[str]] = []
    status_mode = {"v": "ok"}

    def _fake_run(cmd: list[str], **kwargs: object) -> _sp.CompletedProcess:
        calls.append(cmd)
        name = cmd[1] if len(cmd) > 1 else ""
        if name == "login":
            line = _json.dumps(
                {"code": 0, "msg": "ok", "type": "result", "data": {"nickname": "u"}}
            )
        elif name == "get-user-info":
            if status_mode["v"] == "ok":
                line = _json.dumps(
                    {"code": 0, "msg": "ok", "type": "result",
                     "data": {"nickname": "测试用户"}}
                )
            else:
                line = _json.dumps(
                    {"code": -1408, "msg": "未登录", "type": "result", "data": {}}
                )
        elif name == "logout":
            line = _json.dumps({"code": 0, "msg": "ok", "type": "result", "data": {}})
        else:
            line = _json.dumps({"code": -1, "msg": "unexpected", "type": "result", "data": {}})
        return _sp.CompletedProcess(cmd, 0, line + "\n", "")

    monkeypatch.setattr(_sp, "run", _fake_run)

    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _login(client)

    # ---- ① 设置页含夸克登录块（独立块）----
    page = client.get("/settings/scrape")
    assert page.status_code == 200
    assert "夸克网盘登录" in page.text or "夸克授权" in page.text, "设置页应含夸克登录块"
    assert "授权码" in page.text, "夸克登录块应含授权码输入"

    # ---- ② 授权码登录：执行 quark.sh login --token <code>；不回显授权码 ----
    resp = client.post("/settings/scrape/quark-login", data={"code": "test-auth-code-xyz"})
    assert resp.status_code == 200
    login_calls = [c for c in calls if len(c) > 1 and c[1] == "login"]
    assert login_calls, "登录应执行 quark.sh login"
    assert "--token" in login_calls[-1]
    assert "test-auth-code-xyz" in login_calls[-1], "授权码应传给 --token"
    assert "test-auth-code-xyz" not in resp.text, "执行完不应在页面残留授权码"
    assert "登录" in resp.text and ("成功" in resp.text or "已登录" in resp.text or "网盘" in resp.text)

    # ---- ③ 未授权状态：quark-status 提示夸克登录 ----
    status_mode["v"] = "unauthorized"
    resp2 = client.post("/settings/scrape/quark-status")
    assert resp2.status_code == 200
    assert "未授权" in resp2.text or "登录" in resp2.text, (
        f"未授权状态应提示登录，实际：{resp2.text[:200]}"
    )

    # ---- ④ 退出登录 ----
    resp3 = client.post("/settings/scrape/quark-logout")
    assert resp3.status_code == 200
    logout_calls = [c for c in calls if len(c) > 1 and c[1] == "logout"]
    assert logout_calls, "退出应执行 quark.sh logout"

    # 授权码/登录态不落 settings 键（R20：一次性输入不落库不落盘）
    async with AsyncSession(biz_engine) as session:
        rows = (
            await session.execute(
                text("SELECT key FROM sys.settings WHERE key LIKE '%quark%'")
            )
        ).all()
    assert not rows, f"quark 授权码/登录态不应落 settings 表：{rows}"


# =====================================================================
# 批 8（详设 §15.3/§15.4）：A73 定时队列在扒图页 / A74 netdisk.upload_default 生效
# =====================================================================


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a73_queue_on_scrape_page(biz_engine) -> None:
    """A73：定时队列管理在扒图页——①/scrape 含队列管理块（「加入定时队列」按钮 +
    队列条数/列表 + [清空] + 定时默认时间提示）②/settings/scrape 不再含队列块
    （无「加入定时队列」/ 队列卡片标记）——DOM/HTML 断言（照 A56 页面渲染模式）。

    批 8（详设 §15.3 F4，用户拍板）：设置 → 扒图设置 的定时队列块撤销，
    队列管理整块移回扒图页（路由 /settings/scrape/queue → /scrape/queue）。
    """
    from web import scrape_store
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    # 预置：队列 2 条（含一个带 xsec_token 的） + 定时默认时间 07:30
    q1 = _XHS_EXPLORE + "?xsec_token=TOK73A"
    q2 = _XHS_SHORT
    await scrape_store.set_link_queue([q1, q2], settings=store)
    await store.set("scrape.schedule_time", "07:30", "扒图定时默认时间")

    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _login(client)

    # ---- ① /scrape 含定时队列管理块 ----
    page = client.get("/scrape")
    assert page.status_code == 200, f"/scrape -> {page.status_code}"
    text = page.text
    assert "贴链接" in text  # A56 既有断言不破坏
    assert "加入定时队列" in text, "/scrape 应含「加入定时队列」按钮（回归扒图页）"
    assert 'id="scrape-queue-card"' in text, "/scrape 应含队列管理卡片（scrape-queue-card）"
    assert "清空队列" in text, "/scrape 队列块应含 [清空] 按钮"
    assert "定时默认时间：" in text and "07:30" in text, (
        "队列块应含定时默认时间提示（读设置键 scrape.schedule_time=07:30）：\n"
        + text[text.find("定时队列"):text.find("定时队列") + 400]
    )
    # 队列列表逐条渲染（HTML 转义后应含 url 原文）
    assert q1 in text and q2 in text, f"队列列表应含两条链接：{q1!r} / {q2!r}"

    # ---- ② /settings/scrape 不再含队列块 ----
    page2 = client.get("/settings/scrape")
    assert page2.status_code == 200, f"/settings/scrape -> {page2.status_code}"
    t2 = page2.text
    assert "存储目录" in t2, "/settings/scrape 仍应渲染存储目录块（A56 既有断言）"
    assert "加入定时队列" not in t2, (
        "设置页不应再含「加入定时队列」（批 8 撤销设置页队列块）"
    )
    assert "scrape-queue-card" not in t2, (
        "设置页不应再渲染队列卡片（scrape-queue-card）"
    )
    assert "定时队列" not in t2, "设置页不应再出现队列块标题「定时队列」"


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a74_netdisk_upload_default_setting(biz_engine) -> None:
    """A74：设置键 netdisk.upload_default 生效——改 false →
    ①设置键变化（settings_store bool 类型注册，缺省 true）
    ②扒图页「同步上传网盘」复选框初始值 false（页面渲染无 checked；改回 true 勾选）
    ③引擎侧：engine-params 返回变化（false）+ _read_engine_params_from_biz 读取 +
    调度模板参数化实锤（_schedule_input_for 缺省 true；netdisk.upload_default=false
    → 定时 input upload_netdisk=false）。（详设 §15.2/§15.5-4 + §15.6 批 8 技术定）
    """
    import re as _re

    from engine.server import (
        _ENGINE_INPUT_DEFAULTS,
        _apply_engine_params,
        _read_engine_params_from_biz,
        _schedule_input_for,
    )
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)

    # ---- ① 设置键类型注册：缺省回退 true ----
    assert await store.get("netdisk.upload_default", True) is True, (
        "netdisk.upload_default 未设置应回退默认 true"
    )

    # ---- ② engine-params 缺省返回 true；改 false → 返回 false ----
    biz_client_tc = TestClient(_biz_app(biz_engine), raise_server_exceptions=False)
    headers = {"X-Biz-Token": _BIZ_TOKEN}
    resp = biz_client_tc.get("/api/biz/settings/engine-params", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("netdisk.upload_default") is True, (
        f"engine-params 缺省应返回 netdisk.upload_default=true，实际 {data.get('netdisk.upload_default')!r}"
    )

    await store.set("netdisk.upload_default", False, "同步上传网盘默认开关")
    assert await store.get("netdisk.upload_default", True) is False, (
        "改 false 后设置键应解析为 False"
    )
    resp2 = biz_client_tc.get("/api/biz/settings/engine-params", headers=headers)
    assert resp2.json().get("netdisk.upload_default") is False, (
        "改 false 后 engine-params 应返回 netdisk.upload_default=false"
    )

    # ---- ③ 扒图页复选框初始值（页面渲染读设置键）----
    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _login(client)

    page = client.get("/scrape")
    assert page.status_code == 200
    assert 'id="upload-netdisk"' in page.text, "扒图页应含同步上传网盘复选框"
    assert _re.search(r'id="upload-netdisk"[^>]*checked', page.text) is None, (
        "netdisk.upload_default=false 时复选框初始值应未勾选（无 checked）"
    )
    # 对照：改回 true → 勾选
    await store.set("netdisk.upload_default", True, "同步上传网盘默认开关")
    page2 = client.get("/scrape")
    assert _re.search(r'id="upload-netdisk"[^>]*checked', page2.text) is not None, (
        "netdisk.upload_default=true 时复选框初始值应勾选（含 checked）"
    )

    # ---- ④ 引擎侧：读取 + 调度模板参数化 ----
    # ④a _read_engine_params_from_biz：engine-params 返回 false → 引擎参数 false
    def _params_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/biz/settings/engine-params":
            return httpx.Response(
                200,
                json={
                    "max_attempts": 2,
                    "timeout_s": 30.0,
                    "backoff_cap": 30,
                    "netdisk.upload_default": False,
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    from engine.actions.biz_client import BizApiClient

    params_client = BizApiClient(
        base_url=_FAKE_BIZ_URL,
        token=_BIZ_TOKEN,
        transport=httpx.MockTransport(_params_handler),
    )
    engine_params = await _read_engine_params_from_biz(params_client)
    assert engine_params["netdisk.upload_default"] is False, (
        f"引擎读 engine-params 应得 upload_default=false，实际 {engine_params['netdisk.upload_default']!r}"
    )

    # ④b 模板参数化：缺省 true（模块快照）；netdisk.upload_default=false → false
    inp_default = _schedule_input_for("scrape_download_chain", "20260903070000", "2026-09-03")
    assert inp_default["upload_netdisk"] is True, (
        f"模块快照缺省 upload_netdisk 应为 true，实际 {inp_default['upload_netdisk']!r}"
    )
    inp_false = _schedule_input_for(
        "scrape_download_chain", "20260903070000", "2026-09-03",
        defaults={"netdisk.upload_default": False},
    )
    assert inp_false["upload_netdisk"] is False, (
        f"netdisk.upload_default=false → 定时 input upload_netdisk 应为 false，实际 {inp_false['upload_netdisk']!r}"
    )
    assert inp_false["from_queue"] is True and inp_false["batch_id"] == "sched-20260903070000"

    # ④c 生产装配实锤：_apply_engine_params 把引擎参数写进模块快照（_build_app 启动路径）
    prev = _ENGINE_INPUT_DEFAULTS.get("netdisk.upload_default", True)
    try:
        _apply_engine_params({"netdisk.upload_default": False})
        inp_after = _schedule_input_for(
            "scrape_download_chain", "20260903070000", "2026-09-03"
        )
        assert inp_after["upload_netdisk"] is False, (
            "_apply_engine_params(false) 后定时 input upload_netdisk 应为 false"
        )
    finally:
        _ENGINE_INPUT_DEFAULTS["netdisk.upload_default"] = prev  # 恢复模块快照（同进程防污染）


# =====================================================================
# 批 9（详设-v0.6 §15.7，用户拍板）：A75 独立任务详情页 GET /tasks/{id}
# =====================================================================


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a75_task_detail_page_renders(biz_engine) -> None:
    """A75：独立任务详情页（用户拍板，详设-v0.6 §15.7）——构造 task（tags +
    source JSONB + 多行 detail + ai source_type）+ 关联 task_proposal（approved +
    task_id 回填 + evidence）+ task_event 2 条 + task_step 1 条 →
    ①任务中心行 title 变链接 /tasks/{id}（详情入口）②GET /tasks/{id} 200 +
    完整 title + detail 全文（多行原文逐行在页面）+ domain 徽章 + tags +
    source_type + 来源与依据（task.source 逐键 + proposal evidence ref_id/quote 可见）
    + 事件时间线（中文标签/from 状态）可见 + 步骤（content + 待完成徽章 + 完成守卫
    提示）③越界 id → 404。页面渲染照 test_a48/_web_app + 引擎桩模式（零引擎依赖）。"""
    import json as _json
    import re as _re
    from datetime import date as _date

    detail_text = (
        "卖点：永生花花束 手工定制、花期 1-2 年\n"
        "目标市场：婚礼花艺 / 家居摆件 / 伴手礼\n"
        "关键词：永生花,定制花束,婚礼装饰"
    )
    # ---- ① 构造 task + 事件 + 步骤 + 关联提案（照批准流落库形态）----
    async with AsyncSession(biz_engine) as session, session.begin():
        task_id = (
            await session.execute(
                text(
                    "INSERT INTO tm.task (title, detail, domain, role, due, status, "
                    "source_type, source, created_by, tags) "
                    "VALUES (:title, :detail, :domain, :role, :due, 'in_progress', 'ai', "
                    "CAST(:source AS jsonb), :created_by, CAST(:tags AS jsonb)) "
                    "RETURNING id"
                ),
                {
                    "title": "选品建议：永生花花束",
                    "detail": detail_text,
                    "domain": "scrape",
                    "role": "运营",
                    "due": _date.today(),
                    "source": _json.dumps(
                        {
                            "chain_id": "scrape_suggest_chain",
                            "engine_task_id": "e-a75-001",
                            "worker_id": "product_suggestion",
                            "audit_ids": ["audit-a75-1"],
                        }
                    ),
                    "created_by": "管理员",
                    "tags": _json.dumps(["采购", "报价"]),
                },
            )
        ).scalar_one()
        # 事件时间线（created + started，from→to 记录）
        await session.execute(
            text(
                "INSERT INTO tm.task_event (task_id, event_type, from_status, to_status, actor) "
                "VALUES (:tid, 'created', NULL, NULL, '运营'), "
                "(:tid, 'started', 'open', 'in_progress', '运营')"
            ),
            {"tid": task_id},
        )
        # 步骤 1 条（open，未全勾 → 完成守卫提示）
        await session.execute(
            text(
                "INSERT INTO tm.task_step (task_id, content, status, sort_order) "
                "VALUES (:tid, '确认花材供应商报价', 'open', 1)"
            ),
            {"tid": task_id},
        )
        # 关联提案（approved + task_id 回填——决策 16 依据强制展示）
        await session.execute(
            text(
                "INSERT INTO tm.task_proposal (title, detail, domain, action_id, risk, "
                "suggested_role, suggested_due_days, evidence, source, status, "
                "reviewed_by, reviewed_at, task_id) "
                "VALUES (:title, :detail, :domain, 'scrape.suggest', 'suggest', '运营', 3, "
                "CAST(:evidence AS jsonb), CAST(:source AS jsonb), 'approved', '管理员', "
                "now(), :task_id)"
            ),
            {
                "title": "选品建议：永生花花束",
                "detail": "卖点：花艺定制；目标市场：婚礼花艺",
                "domain": "scrape",
                "evidence": _json.dumps(
                    [
                        {
                            "kind": "image",
                            "ref_id": "a75-img-1",
                            "quote": "永生花束实拍图（主图）",
                        }
                    ]
                ),
                "source": _json.dumps(
                    {
                        "chain_id": "scrape_suggest_chain",
                        "engine_task_id": "e-a75-001",
                        "worker_id": "product_suggestion",
                        "audit_ids": ["audit-a75-1"],
                    }
                ),
                "task_id": task_id,
            },
        )

    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _login(client)

    # ---- ② 任务中心行 title 变链接（详情入口）----
    listing = client.get("/tasks")
    assert listing.status_code == 200, f"/tasks -> {listing.status_code}"
    assert f'href="/tasks/{task_id}"' in listing.text, (
        "任务中心行 title 应变链接 → /tasks/{id}（详情入口）"
    )

    # ---- ③ 详情页渲染：完整 title + detail 全文 + 徽章 + 依据 + 时间线 + 步骤 ----
    resp = client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200, f"GET /tasks/{task_id} -> {resp.status_code}"
    page = resp.text
    for needle in ("任务详情", "来源与依据", "流转历史", "任务步骤"):
        assert needle in page, f"详情页应含区块标题：{needle}"
    assert "选品建议：永生花花束" in page, "详情页应含完整 title"
    for line in detail_text.splitlines():
        assert line in page, f"detail 全文应逐行完整可见（pre-wrap）：{line!r}"
    assert _re.search(r'class="badge bg-orange[^"]*">扒图<', page) is not None, (
        "详情页应渲染 domain 来源徽章「扒图」（bg-orange）"
    )
    assert "进行中" in page, "详情页应渲染状态徽章（in_progress=进行中）"
    assert _date.today().isoformat() in page, "详情页应展示截止日期"
    assert "AI 提案" in page, "详情页应展示 source_type 中文（ai=AI 提案）"
    for tag in ("采购", "报价"):
        assert f">{tag}<" in page, f"tags 应可见：{tag}"
    # 来源与依据：task.source 逐键可见 + 关联提案 evidence（决策 16）
    assert "chain_id" in page and "scrape_suggest_chain" in page, "task.source chain_id 应可见"
    assert "engine_task_id" in page and "e-a75-001" in page, "task.source engine_task_id 应可见"
    assert "audit_ids" in page and "audit-a75-1" in page, "task.source audit_ids 应可见"
    assert "a75-img-1" in page, "关联提案 evidence ref_id 应可见"
    assert "永生花束实拍图（主图）" in page, "关联提案 evidence quote 应可见"
    # 事件时间线（中文标签 + from 状态中文）
    assert "已创建" in page, "事件时间线应含 created 中文标签「已创建」"
    assert "已开始" in page, "事件时间线应含 started 中文标签「已开始」"
    assert "待处理" in page, "事件时间线应展示 from 状态中文（open=待处理）"
    # 步骤卡 + 完成守卫提示（未全勾，决策 31）
    assert "确认花材供应商报价" in page, "步骤卡应展示步骤 content"
    assert "待完成" in page, "open 步骤应渲染「待完成」状态徽章"
    assert "有未完成步骤" in page and "会被拒绝" in page, (
        "有未完成步骤时应展示完成守卫提示（决策 31）"
    )

    # ---- ④ 越界/不存在 id → 404 ----
    resp404 = client.get("/tasks/999999999")
    assert resp404.status_code == 404, f"越界 id 应 404，实际 {resp404.status_code}"


# ==== 复核反馈修复回归（2026-09-03）：缩略图 500（str→BIGINT）+ 打开本地文件夹 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_scrape_thumbnail_str_path_returns_image(biz_engine, tmp_path) -> None:
    """复核反馈 1 修复回归：/scrape/thumbnail/{id} 曾因 URL 路径参数 str 绑定 BIGINT 列
    → asyncpg 500（缩略图全占位）；int 注解 + store 强制 int 后应 200 返回真实图字节。"""
    from web import scrape_store
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    await store.set("scrape.storage_dir", str(tmp_path), "扒图存储目录")

    img_dir = tmp_path / "xhs" / "note123"
    img_dir.mkdir(parents=True)
    fake_bytes = b"\xff\xd8\xff\xe0fake-jpeg-content"
    (img_dir / "01.jpg").write_bytes(fake_bytes)

    link = await scrape_store.create_link(_XHS_EXPLORE, "xhs", "batch-thumb")
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO scrape.image_file (batch_id, source, url, link_record_id, "
                "local_path, source_mark, width, height, watermark, status) "
                "VALUES (:b, :s, :u, :l, :p, :m, :w, :h, :wm, 'downloaded')"
            ),
            {
                "b": "batch-thumb", "s": "xhs", "u": _XHS_EXPLORE + "#1", "l": link["id"],
                "p": "xhs/note123/01.jpg", "m": "scraped", "w": 80, "h": 60, "wm": False,
            },
        )

    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _login(client)

    # 模板渲染的 URL 是 str（int id 经 URL 路径成字符串）——打真实请求
    resp = client.get("/scrape/thumbnail/1")
    assert resp.status_code == 200, f"缩略图应 200（修复前 str 参数 500），实际 {resp.status_code}: {resp.text[:80]}"
    assert resp.content == fake_bytes, "缩略图应返回真实文件字节"
    # 越界 id 应 404（store 返回 None）而非 500
    resp_miss = client.get("/scrape/thumbnail/99999")
    assert resp_miss.status_code == 404


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_open_local_folder_route(biz_engine, tmp_path, monkeypatch) -> None:
    """复核反馈 2：POST /scrape/open-folder——kind=root 打开存储根目录（subprocess.Popen
    收到 xdg-open + 绝对路径）；kind=link 无文件夹 404；kind 未知 400；路径越界 403。"""
    import subprocess as _sp

    from web import scrape_store
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    root = tmp_path / "scrape-root"
    root.mkdir(parents=True)
    await store.set("scrape.storage_dir", str(root), "扒图存储目录")

    calls: list[list[str]] = []
    monkeypatch.setattr(_sp, "Popen", lambda cmd, **kw: calls.append(cmd))
    import platform as _platform
    monkeypatch.setattr(_platform, "system", lambda: "Linux")

    app = _web_app(biz_engine, _noop_engine_handler)
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    _login(client)

    # kind=root → 打开存储根
    resp = client.post("/scrape/open-folder", json={"kind": "root"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert str(root.resolve()) in data["path"]
    assert calls and calls[-1][0] == "xdg-open", f"应调 xdg-open，实际 {calls}"

    # kind=link 无下载文件夹 → 404
    link = await scrape_store.create_link(_XHS_EXPLORE, "xhs", "batch-open")
    resp2 = client.post("/scrape/open-folder", json={"kind": "link", "link_id": link["id"]})
    assert resp2.status_code == 404
    assert "无本地文件夹" in resp2.json()["error"]

    # kind=link 正常 → 打开链接文件夹
    link_dir = root / "xhs" / "abc456"
    link_dir.mkdir(parents=True)
    await scrape_store.update_link(link["id"], storage_dir="xhs/abc456")
    resp3 = client.post("/scrape/open-folder", json={"kind": "link", "link_id": link["id"]})
    assert resp3.status_code == 200
    assert resp3.json()["path"].endswith(str(link_dir))

    # kind 未知 → 400
    resp4 = client.post("/scrape/open-folder", json={"kind": "weird"})
    assert resp4.status_code == 400
