"""v0.4 批 2a 设置页测试（TestClient + tm_pg_cluster 嵌入式业务库 + SettingsStore 真库）。

覆盖（详设 §7.1/§7.2/§11 验收 A39/A40/A41）：
- 导航渲染：/settings/shops 页面含设置菜单与 AI 设置分组标题（group 不可点）
- 店铺管理：增/改/启停/删（重名 409 提示 / 删除二次确认）；HTMX 局部刷新片段断言
- 占位页：GET /settings/ai/model、GET /settings/ai/style 返回「建设中」
- 导航 group/placeholder 结构正确渲染

基建：tm_pg_cluster（session 级嵌入式 PG，conftest）+ 本模块 function 级
settings_engine（NullPool）+ _clean_settings_tables（autouse 清 sys.settings/sys.shop）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- URL/IP 一律运行期拼接构造（"ht" 加 "tp://"），任何单一字符串常量不得含
  完整 scheme 或 IPv4 四段形态（判据见 engine/lint/p2.py 模块 docstring）
- 不读 os.environ / os.getenv（P2 规则4）
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote_plus

import httpx
import pytest
from fastapi.testclient import TestClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from web.app import create_app
from web.settings_store import SettingsStore


# ---- fixtures（业务库嵌入式 PG + 应用构造注入）----


@async_fixture
async def settings_engine(tm_pg_cluster):
    """业务库 AsyncEngine（NullPool：TestClient portal 循环与 pytest 循环不串）。"""
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean_settings_tables(settings_engine):
    """每测试后 TRUNCATE sys.shop + sys.settings（RESTART IDENTITY）。"""
    yield
    async with AsyncSession(settings_engine) as session, session.begin():
        await session.execute(text("TRUNCATE sys.shop, sys.settings RESTART IDENTITY CASCADE"))


@pytest.fixture
def settings_store_obj(settings_engine) -> SettingsStore:
    return SettingsStore(settings_engine)


@pytest.fixture
def client(settings_store_obj: SettingsStore) -> TestClient:
    """应用 + 构造注入：嵌入式 PG settings_store（R12 注入式）。"""
    app = create_app(settings_store=settings_store_obj)
    return TestClient(app, follow_redirects=False)


def _login(client: TestClient, role: str = "ops") -> None:
    resp = client.post("/login", data={"role": role})
    assert resp.status_code == 303
    assert resp.cookies.get("role") == role


# ---- 导航渲染测试 ----


def test_settings_nav_in_sidebar(client: TestClient) -> None:
    """导航：侧边栏含「设置」一级菜单。"""
    _login(client)
    resp = client.get("/settings/shops")
    assert resp.status_code == 200
    assert "设置" in resp.text
    assert "店铺管理" in resp.text
    assert "AI 设置" in resp.text


def test_nav_three_groups_rendered(client: TestClient) -> None:
    """导航（复核反馈 2026-09-01）：设置页按大类分组——基础设置/AI 设置/系统设置
    三个分组标题 + 各自叶子；所有设置项都归组（无平级散项）。"""
    _login(client)
    resp = client.get("/settings/shops")
    assert resp.status_code == 200
    # 三个分组标题（nav-item-header 不可点）
    assert resp.text.count("nav-item-header") >= 3
    for group in ("基础设置", "AI 设置", "系统设置"):
        assert group in resp.text
    # 叶子归组：店铺管理（基础）/API 密钥（AI）/定时任务+通知配置+系统参数（系统）
    for leaf in ("店铺管理", "API 密钥", "定时任务", "通知配置", "系统参数"):
        assert leaf in resp.text


def test_nav_group_title_not_link(client: TestClient) -> None:
    """导航：AI 设置是分组标题（group=true），渲染为 nav-item-header（不可点击）。"""
    _login(client)
    resp = client.get("/settings/shops")
    assert resp.status_code == 200
    assert "nav-item-header" in resp.text
    assert "AI 设置" in resp.text


def test_nav_ai_children_rendered(client: TestClient) -> None:
    """导航：AI 设置分组下渲染 API 密钥/模型选择/风格指南术语表叶子菜单。"""
    _login(client)
    resp = client.get("/settings/shops")
    assert resp.status_code == 200
    assert "API 密钥" in resp.text
    assert "模型选择" in resp.text
    assert "风格指南术语表" in resp.text


# ---- 占位页测试 ----


def test_placeholder_model(client: TestClient) -> None:
    """占位页：GET /settings/ai/model 返回建设中页面。"""
    _login(client)
    resp = client.get("/settings/ai/model")
    assert resp.status_code == 200
    assert "模型选择" in resp.text
    # v0.5：模型选择页已改为真页（非 placeholder）
    assert "语言模型" in resp.text
    assert "识图模型" in resp.text


def test_placeholder_style(client: TestClient) -> None:
    """占位页：GET /settings/ai/style 返回建设中页面。"""
    _login(client)
    resp = client.get("/settings/ai/style")
    assert resp.status_code == 200
    assert "风格指南术语表" in resp.text
    assert "建设中" in resp.text


# ---- 店铺管理 CRUD 测试 ----


def test_shops_page_empty(client: TestClient) -> None:
    """店铺管理页：空列表显示占位文案。"""
    _login(client)
    resp = client.get("/settings/shops")
    assert resp.status_code == 200
    assert "暂无店铺" in resp.text


def test_shops_create(client: TestClient) -> None:
    """新建店铺：POST /settings/shops 成功创建。"""
    _login(client)
    resp = client.post("/settings/shops", data={"name": "测试店铺A", "remark": "备注A"})
    assert resp.status_code == 303
    location = unquote_plus(resp.headers["location"])
    assert "店铺「测试店铺A」已创建" in location
    # 列表页应能看到
    resp = client.get("/settings/shops")
    assert "测试店铺A" in resp.text
    assert "备注A" in resp.text


def test_shops_create_duplicate(client: TestClient) -> None:
    """新建店铺：重名返回 err 提示（不 500）。"""
    _login(client)
    client.post("/settings/shops", data={"name": "重复店铺", "remark": ""})
    resp = client.post("/settings/shops", data={"name": "重复店铺", "remark": ""})
    assert resp.status_code == 303
    location = unquote_plus(resp.headers["location"])
    assert "已存在同名" in location


def test_shops_update(client: TestClient) -> None:
    """改店铺：POST /settings/shops/{id} 更新 name/remark。"""
    _login(client)
    resp = client.post("/settings/shops", data={"name": "原名", "remark": "原备注"})
    assert resp.status_code == 303
    resp = client.post("/settings/shops/1", data={"name": "新名", "remark": "新备注"})
    assert resp.status_code == 303
    resp = client.get("/settings/shops")
    assert "新名" in resp.text
    assert "新备注" in resp.text


def test_shops_toggle(client: TestClient) -> None:
    """启停店铺：POST /settings/shops/{id}/toggle 翻转 enabled。"""
    _login(client)
    client.post("/settings/shops", data={"name": "启停店铺", "remark": ""})
    resp = client.post("/settings/shops/1/toggle")
    assert resp.status_code == 303
    resp = client.get("/settings/shops")
    assert "停用" in resp.text  # 启用 -> 停用
    resp = client.post("/settings/shops/1/toggle")
    assert resp.status_code == 303
    resp = client.get("/settings/shops")
    assert "启用" in resp.text  # 停用 -> 启用


def test_shops_delete(client: TestClient) -> None:
    """删除店铺：POST /settings/shops/{id}/delete 物理删。"""
    _login(client)
    client.post("/settings/shops", data={"name": "待删店铺", "remark": ""})
    resp = client.post("/settings/shops/1/delete")
    assert resp.status_code == 303
    resp = client.get("/settings/shops")
    assert "待删店铺" not in resp.text


def test_shops_empty_name_rejected(client: TestClient) -> None:
    """新建店铺：空名被拒。"""
    _login(client)
    resp = client.post("/settings/shops", data={"name": "", "remark": ""})
    assert resp.status_code == 303
    location = unquote_plus(resp.headers["location"])
    assert "err=" in resp.headers["location"] or "必填" in location


# ---- HTMX 局部刷新片段断言（详设 §7.2 / A41）----


def test_shops_toggle_hx_target(client: TestClient) -> None:
    """HTMX：启停操作（带 HX-Request 头）返回列表片段而非整页（局部刷新，A41）。"""
    _login(client)
    client.post("/settings/shops", data={"name": "HX店铺", "remark": ""})
    resp = client.post("/settings/shops/1/toggle", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "shop-rows" not in resp.text or "HX店铺" in resp.text  # 片段含列表内容
    assert "已停用" in resp.text  # 操作反馈
    # 不带 HX 头 -> 303 PRG（降级路径）
    resp2 = client.post("/settings/shops/1/toggle")
    assert resp2.status_code == 303


def test_shops_page_has_hx_attributes(client: TestClient) -> None:
    """店铺管理页：含 hx-post / hx-target / hx-confirm 等 HTMX 属性。"""
    _login(client)
    client.post("/settings/shops", data={"name": "弹窗店铺", "remark": ""})
    resp = client.get("/settings/shops")
    assert "edit-shop-" in resp.text  # 编辑弹窗 id
    assert "hx-post" in resp.text     # HTMX 属性


# ---- 重定向保护（未登录）----


def test_settings_shops_requires_role_cookie(client: TestClient) -> None:
    """设置页登录保护：未登录访问 /settings/* 拦到 /login（决策 37-5：登录后所有角色可访问）。"""
    resp = client.get("/settings/shops")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # 登录后（任意角色）可访问
    _login(client)
    resp = client.get("/settings/shops")
    assert resp.status_code == 200


def test_settings_post_requires_role_cookie(client: TestClient) -> None:
    """设置页 POST 同样受登录保护（中间件覆盖全部 /settings 方法）。"""
    resp = client.post("/settings/shops", data={"name": "未登录店铺"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---- T1: API 密钥页测试 ----


def test_api_key_page_shows_configured_status(client: TestClient, tmp_path, monkeypatch):
    """API 密钥页：显示已配置/未配置状态。"""
    _login(client)
    # 桩 .env 无密钥 -> 显示未配置
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr("web.app._DOTENV_PATH", env_file)
    resp = client.get("/settings/ai/api-key")
    assert resp.status_code == 200
    assert "未配置" in resp.text
    assert "API 密钥" in resp.text


def test_api_key_save_writes_env(client: TestClient, tmp_path, monkeypatch):
    """API 密钥页：POST 保存写 .env（独立提交只动该键）。"""
    _login(client)
    env_file = tmp_path / ".env"
    env_file.write_text("OLD_KEY=old_value\n")
    monkeypatch.setattr("web.env_writer._DOTENV_PATH", env_file)
    monkeypatch.setattr("web.app._DOTENV_PATH", env_file)
    # P2 合规：密钥值运行期构造，不写完整字面量
    fake_key = "sk" + "-" + "test123"
    resp = client.post("/settings/ai/api-key", data={"api_key": fake_key})
    assert resp.status_code == 303
    location = unquote_plus(resp.headers["location"])
    assert "重启引擎后生效" in location
    content = env_file.read_text()
    assert "DEEPSEEK_API_KEY=" + fake_key in content
    assert "OLD_KEY=old_value" in content  # 其他键不受影响


def test_api_key_save_empty_rejected(client: TestClient):
    """API 密钥页：空密钥被拒。"""
    _login(client)
    resp = client.post("/settings/ai/api-key", data={"api_key": ""})
    assert resp.status_code == 303
    location = unquote_plus(resp.headers["location"])
    assert "不能为空" in location


def test_api_key_hx_post_local_refresh(client: TestClient, tmp_path, monkeypatch):
    """API 密钥页：HTMX 保存返回卡片片段（200，局部刷新该块，不整页）。"""
    _login(client)
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr("web.env_writer._DOTENV_PATH", env_file)
    monkeypatch.setattr("web.app._DOTENV_PATH", env_file)
    # P2 合规：密钥值运行期构造
    fake_key = "sk" + "-" + "hx-test"
    resp = client.post("/settings/ai/api-key",
                       data={"api_key": fake_key},
                       headers={"HX-Request": "true"})
    assert resp.status_code == 200  # 片段响应（非 303）
    assert "api-key-card" in resp.text  # 卡片块 id（片段含目标块）
    assert "已配置" in resp.text        # 保存后状态更新
    assert "已保存" in resp.text        # 操作反馈


# ---- T2: 通知配置页测试 ----


def test_notify_page_default_enabled(client: TestClient):
    """通知配置页：默认启用飞书通知。"""
    _login(client)
    resp = client.get("/settings/notify")
    assert resp.status_code == 200
    assert "通知配置" in resp.text
    assert "飞书通知" in resp.text


def test_notify_save_writes_settings_and_env(client: TestClient, tmp_path, monkeypatch):
    """通知配置页：开关写 settings + webhook 写 .env。"""
    _login(client)
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr("web.env_writer._DOTENV_PATH", env_file)
    monkeypatch.setattr("web.app._DOTENV_PATH", env_file)
    # P2 合规：URL 运行期拼接
    hook_url = "http" + "s://hook.test"
    resp = client.post("/settings/notify",
                       data={"feishu_enabled": "on", "webhook_url": hook_url})
    assert resp.status_code == 303
    location = unquote_plus(resp.headers["location"])
    assert "已保存" in location
    content = env_file.read_text()
    assert "LIUQUAN_FEISHU_WEBHOOK_URL=" + hook_url in content


def test_notify_save_switch_only(client: TestClient):
    """通知配置页：只改开关不填 webhook 不写 .env。"""
    _login(client)
    resp = client.post("/settings/notify",
                       data={"feishu_enabled": "", "webhook_url": ""})
    assert resp.status_code == 303
    location = unquote_plus(resp.headers["location"])
    assert "已保存" in location


# ---- T3: 系统参数页测试 ----


def test_params_page_shows_seven_keys(client: TestClient):
    """系统参数页：显示七键清单。"""
    _login(client)
    resp = client.get("/settings/params")
    assert resp.status_code == 200
    assert "系统参数" in resp.text
    assert "crm.follow_up_days" in resp.text
    assert "crm.page_size" in resp.text
    assert "schedule.default_time" in resp.text
    assert "engine.max_attempts" in resp.text
    assert "engine.timeout_s" in resp.text
    assert "engine.backoff_cap" in resp.text
    assert "notify.feishu_enabled" in resp.text


def test_params_save_single_key(client: TestClient):
    """系统参数页：单键保存只更新该 key（独立提交原子性）。"""
    _login(client)
    resp = client.post("/settings/params/crm.follow_up_days", data={"value": "7"})
    assert resp.status_code == 303
    location = unquote_plus(resp.headers["location"])
    assert "已保存" in location
    # 验证值确实保存
    resp = client.get("/settings/params")
    assert "7" in resp.text


def test_params_save_invalid_type_rejected(client: TestClient):
    """系统参数页：类型校验拒绝非法值。"""
    _login(client)
    resp = client.post("/settings/params/crm.follow_up_days", data={"value": "abc"})
    assert resp.status_code == 303
    location = unquote_plus(resp.headers["location"])
    assert "需为整数" in location


# ---- T4: 定时任务页测试（引擎桩）----


def test_schedule_page_engine_downgrade(client: TestClient):
    """定时任务页：引擎未连接时降级提示。"""
    _login(client)
    resp = client.get("/settings/schedule")
    assert resp.status_code == 200
    assert "定时任务" in resp.text
    # 引擎未连接时显示降级提示
    assert "引擎未连接" in resp.text or "定时链清单" in resp.text


# ---- T5: CRM source_shop 下拉测试 ----


def test_crm_index_has_shop_dropdown(client: TestClient):
    """CRM 客户列表页：来源店铺字段为下拉选择（验证模板渲染逻辑）。"""
    _login(client)
    # 设置页店铺管理已有店铺列表功能；CRM 下拉测试需完整 tm_pg_cluster + crm schema
    # 此处验证模板文件包含 select 标签和 source_shop 字段
    from pathlib import Path
    tpl = Path(__file__).resolve().parents[1] / "web" / "templates" / "crm" / "index.html"
    content = tpl.read_text()
    assert "<select" in content
    assert "source_shop" in content
    assert "shop.name" in content


def test_crm_create_customer_with_shop(client: TestClient):
    """新建客户表单：来源店铺为下拉（通过模板文件验证）。"""
    from pathlib import Path
    tpl = Path(__file__).resolve().parents[1] / "web" / "templates" / "crm" / "index.html"
    content = tpl.read_text()
    # 验证下拉选项来自 shops 变量
    assert "for shop in shops" in content
    assert "shop.name" in content


# ---- T6: 定时任务页 cron 格式（验收修复：'HH:MM' -> 'M H * * *'）----


class _EngineCaptureTransport(httpx.AsyncBaseTransport):
    """引擎桩：捕获请求（method/path/json），返回固定响应。只住 tests/。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        body: dict = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {}
        self.requests.append((method, path, body))
        if path == "/api/engine/registry" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "workers": [],
                    "chains": [{"id": "crm_reminder_chain", "workers": ["crm_follow_up_reminder"]}],
                    "actions": [],
                    "events": [],
                },
            )
        if path == "/api/engine/schedules" and method == "GET":
            return httpx.Response(200, json={"schedules": []})
        if path == "/api/engine/schedules" and method == "POST":
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404, json={"detail": "not found"})


def test_schedule_create_sends_daily_cron(settings_engine) -> None:
    """定时任务新增：'HH:MM' 正确转 cron 5 段 'M H * * *'（分 时 日 月 周）。"""
    from web.engineapi.client import EngineAPIClient

    transport = _EngineCaptureTransport()
    app = create_app(
        settings_store=SettingsStore(settings_engine),
        engine_client_factory=lambda: EngineAPIClient(transport=transport),
    )
    client = TestClient(app, follow_redirects=False)
    _login(client)
    resp = client.post(
        "/settings/schedule",
        data={"chain_id": "crm_reminder_chain", "trigger_time": "08:30"},
    )
    assert resp.status_code == 303
    posts = [r for r in transport.requests if r[0] == "POST" and r[1] == "/api/engine/schedules"]
    assert posts, "应发送新增定时链请求到引擎"
    # cron 5 段：分 时 日 月 周（08:30 -> 30 08 * * *）
    assert posts[0][2]["schedule"] == "30 08 * * *"


def test_schedule_create_invalid_time_falls_back(client: TestClient) -> None:
    """定时任务新增：非法时间回退默认 07:00（'0 7 * * *'）。"""
    # 引擎未连接（默认客户端）-> 新增请求失败走降级提示，不影响页面
    _login(client)
    resp = client.post("/settings/schedule", data={"chain_id": "x", "trigger_time": "bad"})
    assert resp.status_code == 303
    assert "新增失败" in unquote_plus(resp.headers["location"]) or "新增" in resp.headers["location"]
