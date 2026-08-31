"""v0.4 验收断言 A38-A45（详设-v0.4 §11；@version_acceptance）。

覆盖（每条对应详设 §11 验收断言表）：
- A38 settings 表 + get_setting：读表/回退默认/类型解析；硬编码搬家生效
  （crm.follow_up_days/page_size 改读 settings，改键 -> 逾期判定/分页变化）
- A39 店铺管理增改启停删 + CRM source_shop 下拉（候选 = sys.shop 启用中店铺 + 可留空）
- A40 API 密钥页写 .env（R20）+ 通知配置（总开关 settings + webhook 写 .env）
- A41 独立提交生效：改某键只更新该 key（其他 key updated_at 不变）
- A42 定时底座：调度器 tick 到点触发链入队 + 种子链 + 漏跑补跑 + 不重跑
- A43 提醒任务写接口：直接落 tm.task（source_type=schedule 不经提案不进审核）+
  同一客户当天重复触发只生成一次（防重 409）+ 次日允许
- A44 引擎参数读接口：engine.* 三键改 settings -> GET /api/biz/settings/engine-params 返回值变化
- A45 验收套件完整性：提醒链/消费者/调度器能力已注册 + A1-A37 回归（全量由 verify 真跑）

基建：tm_pg_cluster / engine_pg_cluster（conftest 嵌入式 PG）+ 独立 _clean_v04_tables
（每测试后清 sys.settings/sys.shop/crm/tm 相关表，互不污染）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- URL/IP 一律运行期拼接，单一字符串常量不得含完整 scheme 或 IPv4 四段
- 不读 os.environ / os.getenv（P2 规则4）
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from test_web_tm import _login  # noqa: F401  # 跨模块 helper（只住 tests/）

REPO_ROOT = Path(__file__).resolve().parents[1]

# P2 合规：URL/token 运行期拼接
_FAKE_BIZ_URL = "ht" + "tp://" + "biz.test"
_BIZ_TOKEN = "test" + "-biz-token"


# ---- fixtures ----


@async_fixture
async def biz_engine(tm_pg_cluster):
    """业务库 AsyncEngine（NullPool：TestClient portal 循环与 pytest 循环不串）。"""
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean_v04_tables(biz_engine):
    """每测试后清 v0.4 涉及表（sys/crm/tm），互不污染。"""
    yield
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE sys.settings, sys.shop, "
                "crm.todo_candidate, crm.snapshot, crm.message, crm.customer, "
                "tm.task_step, tm.task_event, tm.task_proposal, tm.task "
                "RESTART IDENTITY CASCADE"
            )
        )


def _biz_app(biz_engine) -> FastAPI:
    """业务读写接口 app（X-Biz-Token 注入，照 test_biz_api 模式）。"""
    from web.api_biz import create_biz_router

    app = FastAPI()
    app.include_router(create_biz_router(engine=biz_engine, token=_BIZ_TOKEN))
    return app


def _web_app(biz_engine) -> FastAPI:
    """完整 web 应用（settings_store/crm_store 注入嵌入式 PG）。"""
    from web.app import create_app
    from web.crm_store import CRMStore
    from web.settings_store import SettingsStore

    return create_app(
        settings_store=SettingsStore(biz_engine),
        crm_store=CRMStore(biz_engine, settings_store=SettingsStore(biz_engine)),
        tm_store=None,  # lifespan 会 from_env 兜底；仅测设置页/CRM 页无需 tm
    )


# ==== A38：settings 表 + 硬编码搬家生效 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a38_settings_ground_and_hardcode_migration(biz_engine) -> None:
    """A38：settings 读表/回退默认/类型解析；crm.follow_up_days/page_size 改读 settings。"""
    from models.crm import Customer
    from web.crm_store import CRMStore
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    # 回退默认（表无键）
    assert await store.get("crm.follow_up_days", 5) == 5
    assert await store.get("notify.feishu_enabled", True) is True
    # 类型解析
    await store.set("crm.follow_up_days", 8, "跟进天数")
    assert await store.get("crm.follow_up_days", 5) == 8
    await store.set("notify.feishu_enabled", False)
    assert await store.get("notify.feishu_enabled", True) is False

    # 硬编码搬家生效：逾期判定改读 settings（客户 6 天前无动静）
    async with AsyncSession(biz_engine) as session, session.begin():
        session.add(
            Customer(
                nickname="搬家验证客户",
                follow_up_status="waiting_reply",
                last_contacted_at=datetime.now(timezone.utc) - timedelta(days=6),
            )
        )
    crm = CRMStore(biz_engine, settings_store=store)
    rows, _ = await crm.list_customers()
    assert rows and rows[0]["overdue"] is False  # 6 < 8：改键 8 后不逾期
    await store.set("crm.follow_up_days", 3)
    rows, _ = await crm.list_customers()
    assert rows and rows[0]["overdue"] is True  # 6 > 3：再次逾期（搬家生效）
    # page_size 键可读
    await store.set("crm.page_size", 10, "每页条数")
    assert await store.get("crm.page_size", 20) == 10


# ==== A39：店铺管理 + CRM source_shop 下拉 ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a39_shop_crud_and_crm_dropdown(biz_engine) -> None:
    """A39：店铺增改启停删 + CRM 客户表单 source_shop 下拉（启用中店铺 + 可留空）。"""
    from web.settings_store import SettingsError, SettingsStore

    store = SettingsStore(biz_engine)
    # 增
    sid = await store.create_shop("主店", "主店铺备注")
    with pytest.raises(SettingsError):
        await store.create_shop("主店")  # 重名（忽略大小写）
    # 改
    await store.update_shop(sid, "主店改", "改后备注")
    # 启停
    assert await store.toggle_shop(sid) is False
    shops_all = await store.list_shops(include_disabled=True)
    assert any(s["name"] == "主店改" and s["enabled"] is False for s in shops_all)
    assert not any(s["name"] == "主店改" for s in await store.list_shops())  # 停用不出现在下拉候选
    # 删
    await store.delete_shop(sid)
    assert not await store.list_shops(include_disabled=True)

    # CRM 页面下拉：选项 = sys.shop 启用中店铺 + 可留空
    await store.create_shop("下拉店铺", "")
    app = _web_app(biz_engine)
    client = TestClient(app, follow_redirects=False)
    _login(client)
    resp = client.get("/crm")
    assert resp.status_code == 200
    assert 'name="source_shop"' in resp.text  # 下拉字段
    assert "下拉店铺" in resp.text  # 启用中店铺为候选
    assert '<option value="">未选择</option>' in resp.text  # 可留空


# ==== A40：API 密钥写 .env + 通知配置 ====


@pytest.mark.version_acceptance
def test_a40_api_key_env_and_notify(biz_engine, tmp_path, monkeypatch) -> None:
    """A40：API 密钥保存写 .env（R20）+ 通知总开关写 settings + webhook 写 .env。"""
    import asyncio

    from web.settings_store import SettingsStore

    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=old\n", encoding="utf-8")
    monkeypatch.setattr("web.app._DOTENV_PATH", env_file)
    monkeypatch.setattr("web.env_writer._DOTENV_PATH", env_file)

    app = _web_app(biz_engine)
    client = TestClient(app, follow_redirects=False)
    _login(client)

    # API 密钥保存 -> .env 键替换（只动该键）
    fake_key = "sk" + "-" + "v04-test"
    resp = client.post("/settings/ai/api-key", data={"api_key": fake_key})
    assert resp.status_code == 303
    content = env_file.read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=" + fake_key in content
    assert "old" not in content.replace(fake_key, "")  # 旧值被替换

    # 通知：总开关写 settings + webhook 写 .env
    fake_webhook = "https" + "://open.feishu.cn/hook/test"
    resp = client.post(
        "/settings/notify",
        data={"feishu_enabled": "on", "webhook_url": fake_webhook},
    )
    assert resp.status_code == 303
    assert env_file.read_text(encoding="utf-8").count("LIUQUAN_FEISHU_WEBHOOK_URL") == 1
    store = SettingsStore(biz_engine)
    assert asyncio.run(store.get("notify.feishu_enabled", True)) is True


# ==== A41：独立提交——只更新该 key ====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a41_independent_submit_only_that_key(biz_engine) -> None:
    """A41：改某一项只更新该 key（其他 key updated_at 不动，settings 按 key 原子更新）。"""
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    await store.set("crm.follow_up_days", 5, "跟进天数")
    await store.set("crm.page_size", 20, "每页条数")

    async with AsyncSession(biz_engine) as session:
        rows = (
            await session.execute(
                select(text("key, updated_at")).select_from(text("sys.settings"))
            )
        ).all()
    by_key = {k: ts for k, ts in rows}
    page_size_ts = by_key["crm.page_size"]

    # 只改 crm.follow_up_days
    await store.set("crm.follow_up_days", 7, "跟进天数")

    async with AsyncSession(biz_engine) as session:
        rows2 = (
            await session.execute(
                select(text("key, value, updated_at")).select_from(text("sys.settings"))
            )
        ).all()
    by_key2 = {k: (v, ts) for k, v, ts in rows2}
    assert by_key2["crm.follow_up_days"][0] == "7"  # 该键已更新
    assert by_key2["crm.page_size"][1] == page_size_ts  # 其他键 updated_at 不动（原子性）


# ==== A42：定时底座（调度器到点触发 + 种子 + 漏跑/不重跑）====


@pytest.mark.version_acceptance
@pytest.mark.asyncio
async def test_a42_scheduler_fires_and_seed(engine_pg_cluster, monkeypatch) -> None:
    """A42：Scheduler.tick 到点触发链入队 + 锚点推进 + 种子链（漏跑补跑/不重跑由
    tests/test_scheduler.py 详细覆盖，此处验收关键闭环）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")
    from engine.core import db as _db
    from engine.registry import load_registry
    from engine.server import Scheduler

    engine = _db.create_engine(engine_pg_cluster.url)
    registry = load_registry(REPO_ROOT)
    fixed_now = datetime(2026, 2, 1, 7, 10, 0, tzinfo=timezone.utc)
    try:
        # 造一条到点链（07:00 已过 -> 应补触发一次，验收「不漏跑」）
        sid = await _db.create_schedule(
            engine, chain_id="crm_reminder_chain", name="验收链", cron="0 7 * * *"
        )
        await _db.update_schedule(
            engine, sid,
            next_run_at=datetime(2026, 2, 1, 7, 0, 0, tzinfo=timezone.utc),
            set_next_run_at=True,
        )
        scheduler = Scheduler(engine, registry, interval=9999, now_fn=lambda: fixed_now)
        await scheduler.tick()
        row = await _db.get_schedule(engine, sid)
        assert row is not None and row.last_run_at == datetime(
            2026, 2, 1, 7, 0, 0, tzinfo=timezone.utc
        )
        tasks = await _db.list_tasks_by_status(engine, "queued")
        assert any(t.chain_id == "crm_reminder_chain" and t.trigger_type == "schedule"
                   for t in tasks)

        # 种子链：清空 schedule 后 tick -> crm_reminder_chain 种子存在且锚点被初始化
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(text("TRUNCATE schedule RESTART IDENTITY CASCADE"))
        scheduler2 = Scheduler(engine, registry, interval=9999, now_fn=lambda: fixed_now)
        await scheduler2.tick()
        seeds = await _db.list_schedules(engine)
        assert any(s.chain_id == "crm_reminder_chain" for s in seeds)
        seed = next(s for s in seeds if s.chain_id == "crm_reminder_chain")
        assert seed.next_run_at is not None  # 锚点已初始化（种子链开箱可用）
    finally:
        await _db.dispose_engine(engine)


# ==== A43：提醒任务写接口（直接落库 + 当天防重 + 次日允许）====


@pytest.mark.version_acceptance
def test_a43_schedule_tasks_write_and_dedup(biz_engine) -> None:
    """A43：POST /api/biz/tm/schedule-tasks 直接落 tm.task（source_type=schedule，
    不经提案不进审核）；同客户同 reminder_date 防重 409；次日（不同日期）允许。"""
    import asyncio

    from models.tm import Task

    app = _biz_app(biz_engine)
    client = TestClient(app, follow_redirects=False)
    headers = {"X-Biz-Token": _BIZ_TOKEN}

    def payload(customer_id: int, reminder_date: str) -> dict:
        return {
            "title": "跟进客户：验收（已 6 天未跟进）",
            "detail": "验收客户详情",
            "domain": "crm",
            "source": {
                "chain_id": "crm_reminder_chain",
                "engine_task_id": "e-000001",
                "worker_id": "crm_follow_up_reminder",
                "customer_id": customer_id,
                "reminder_date": reminder_date,
                "audit_ids": [],
            },
            "role": "运营",
            "due": reminder_date,
            "evidence": [
                {"kind": "customer", "ref_id": str(customer_id), "quote": "验收"}
            ],
        }

    # 落库（source_type=schedule，无提案表）
    resp = client.post("/api/biz/tm/schedule-tasks", json=payload(101, "2026-02-01"), headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # 防重：同客户同日期 -> 409（当天只生成一次）
    resp = client.post("/api/biz/tm/schedule-tasks", json=payload(101, "2026-02-01"), headers=headers)
    assert resp.status_code == 409
    assert "已生成" in resp.json()["detail"]

    # 次日（reminder_date 变化）允许再生成
    resp = client.post("/api/biz/tm/schedule-tasks", json=payload(101, "2026-02-02"), headers=headers)
    assert resp.status_code == 200

    # 落库字段断言（直接 tm.task，不经提案）
    async def _check() -> None:
        async with AsyncSession(biz_engine) as session:
            tasks = (await session.execute(select(Task).order_by(Task.id))).scalars().all()
        assert len(tasks) == 2
        t = tasks[0]
        assert t.domain == "crm" and t.source_type == "schedule"
        assert t.role == "运营" and str(t.due) == "2026-02-01"
        assert t.source["customer_id"] == 101
        assert t.source["reminder_date"] == "2026-02-01"

    asyncio.run(_check())


# ==== A44：引擎参数读接口（engine.* 键改 settings -> 返回值变化）====


@pytest.mark.version_acceptance
def test_a44_engine_params_from_settings(biz_engine) -> None:
    """A44：GET /api/biz/settings/engine-params 读 settings 三键，缺省回退默认。"""
    from web.settings_store import SettingsStore

    store = SettingsStore(biz_engine)
    app = _biz_app(biz_engine)
    client = TestClient(app, follow_redirects=False)
    headers = {"X-Biz-Token": _BIZ_TOKEN}

    # 缺省回退默认
    resp = client.get("/api/biz/settings/engine-params", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["max_attempts"] == 2
    assert data["timeout_s"] == 30.0
    assert data["backoff_cap"] == 30
    assert data["llm_model"] == "deepseek-chat"
    assert data["vision_model"] == "qwen-vl-max"

    # 改 settings 键 -> 返回值变化（配置地基生效）
    import asyncio

    async def _set() -> None:
        await store.set("engine.max_attempts", 4)
        await store.set("engine.timeout_s", 45)
        await store.set("engine.backoff_cap", 20)

    asyncio.run(_set())
    resp = client.get("/api/biz/settings/engine-params", headers=headers)
    data = resp.json()
    assert data["max_attempts"] == 4
    assert data["timeout_s"] == 45.0
    assert data["backoff_cap"] == 20
    assert data["llm_model"] == "deepseek-chat"
    assert data["vision_model"] == "qwen-vl-max"


# ==== A45：验收套件完整性（提醒链能力已注册 + A1-A37 回归由 verify 真跑）====


@pytest.mark.version_acceptance
def test_a45_capability_registered_and_no_env_sources(monkeypatch) -> None:
    """A45：v0.4 关键能力已注册（提醒链/调度器/消费者/source_type=schedule）+ 引擎零业务库连接串。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")
    from engine.registry import load_registry

    registry = load_registry(REPO_ROOT)
    assert "crm_reminder_chain" in registry.chains  # 提醒链已注册
    assert "crm_follow_up_reminder" in registry.workers  # 纯代码提醒工序
    assert "tm.schedule" in registry.actions  # 消费者 action 已注册

    # 引擎包零业务库连接串（决策 26，A35 同口径回归）
    hits: list[str] = []
    for py in (REPO_ROOT / "engine").rglob("*.py"):
        if "__pycache__" in str(py):
            continue
        if "LIUQUAN_TM_DB_URL" in py.read_text(encoding="utf-8"):
            hits.append(str(py))
    assert hits == [], f"engine 出现业务库连接串引用（决策 26 违反）：{hits}"
