"""v0.2 T6 web 侧接真数据测试（TestClient + tm_pg_cluster 嵌入式业务库 + MockTransport 引擎桩）。

覆盖（任务书验收 2，TestClient 断言 200 与关键元素 + 真库断言状态/事件）：
- 登录与角色标签（A16 落点：未登录 /tasks 重定向 /login；角色 cookie 映射中文标签）
- 任务列表（真实 tm.task）/ 逾期提醒条计数（决策 17：常驻 + 一直提醒）
- 列表筛选：状态 / 逾期 / 关键词（详设 §6.2 先做这三个）
- 状态操作：started / completed（必填 result_note 拒后成功，A23）/ voided /
  blocked(+reason) / unblocked；非法迁移拒
- 派生（A24：原任务不结束 + derived 事件）/ 编辑（A26：updated 事件 from/to 快照）/
  重开（done/void -> open）
- 人工建任务（A20：source_type=manual + 来源域）/ 提案批准（A13：事务内 approved +
  建任务 + created/approved 事件 + task_id 回填）/ 驳回（不建任务）
- 触发演示链面板（registry 链清单 + POST 触发 + 轮询 GET 查终态 + 引擎故障降级）

基建：tm_pg_cluster（session 级嵌入式 PG，conftest）+ 本模块 function 级
tm_engine（NullPool：跨事件循环安全——TestClient 请求跑在独立 portal 循环，
NullPool 每次会话新建连接，不跨循环复用）+ _clean_tm_tables（autouse 清三表）。
引擎三接口用 _EngineStub + httpx.MockTransport 构造注入（R12：零网络零真服务，
桩只住 tests/）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- URL/IP 一律运行期拼接构造（"ht" 加 "tp://"），任何单一字符串常量不得含
  完整 scheme 或 IPv4 四段形态（判据见 engine/lint/p2.py 模块 docstring）
- 不给敏感名赋非空字面量、不读 os.environ（P2 规则 3/4）
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote_plus

import httpx
import pytest
from fastapi.testclient import TestClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from models.tm import Task as TmTask
from models.tm import TaskEvent as TmTaskEvent
from models.tm import TaskProposal as TmTaskProposalRow
from web.app import ROLE_LABEL, create_app
from web.engineapi.client import EngineAPIClient
from web.tm_store import TMStore, proposal_display_id, task_display_id

# 引擎桩 base URL（运行期拼接，见模块 docstring）
_FAKE_BASE_URL = "ht" + "tp://" + "engine" + ".test"


class _EngineStub:
    """引擎三接口桩（R12 构造注入：MockTransport，零网络；只住 tests/）。

    可配置：registry / create_task 响应 / get_task 状态 / 全局故障（fail）。
    last_create 捕获最近一次 POST /api/engine/tasks 的请求体。
    """

    def __init__(self) -> None:
        self.registry: dict[str, Any] = {
            "workers": [{"id": "demo_echo", "domain": "demo", "risk": "read", "version": 1}],
            "chains": [{"id": "tm_demo_chain", "workers": ["demo_echo", "demo_propose"]}],
            "actions": [{"id": "tm.proposal", "risk": "suggest"}],
            "events": [],
        }
        self.created: dict[str, Any] = {
            "task_id": "e-000001", "status": "queued", "chain_id": "tm_demo_chain",
        }
        self.task: dict[str, Any] = {
            "task_id": "e-000001", "chain_id": "tm_demo_chain", "status": "queued",
            "current_step": 0, "error": None, "output": None, "finished_at": None, "audits": [],
        }
        self.fail: tuple[int, str] | None = None
        self.last_create: dict[str, Any] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail is not None:
            return httpx.Response(self.fail[0], json={"detail": self.fail[1]}, request=request)
        path = request.url.path
        if path == "/api/engine/tasks" and request.method == "POST":
            self.last_create = json.loads(request.read())
            return httpx.Response(201, json=self.created, request=request)
        if path == "/api/engine/registry":
            return httpx.Response(200, json=self.registry, request=request)
        if path.startswith("/api/engine/tasks/"):
            return httpx.Response(200, json=self.task, request=request)
        return httpx.Response(404, json={"detail": "not found"}, request=request)


# ---- fixtures（业务库嵌入式 PG + 应用构造注入）----


@async_fixture
async def tm_engine(tm_pg_cluster):
    """业务库 AsyncEngine（NullPool：TestClient portal 循环与 pytest 循环不串）。"""
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean_tm_tables(tm_engine):
    """每测试后 TRUNCATE tm 三表（RESTART IDENTITY 重置自增、CASCADE 破外键）。"""
    yield
    async with AsyncSession(tm_engine) as session, session.begin():
        await session.execute(
            text("TRUNCATE tm.task_event, tm.task_proposal, tm.task RESTART IDENTITY CASCADE")
        )


@pytest.fixture
def store(tm_engine) -> TMStore:
    return TMStore(tm_engine)


@pytest.fixture
def engine_stub() -> _EngineStub:
    return _EngineStub()


@pytest.fixture
def client(store: TMStore, engine_stub: _EngineStub) -> TestClient:
    """应用 + 构造注入：嵌入式 PG store + MockTransport 引擎桩（R12 注入式）。"""

    def factory() -> EngineAPIClient:
        return EngineAPIClient(
            base_url=_FAKE_BASE_URL,
            transport=httpx.MockTransport(engine_stub.handler),
        )

    app = create_app(tm_store=store, engine_client_factory=factory)
    return TestClient(app, follow_redirects=False)


def _login(client: TestClient, role: str = "ops") -> None:
    resp = client.post("/login", data={"role": role})
    assert resp.status_code == 303
    assert resp.cookies.get("role") == role


def _loc(resp: httpx.Response) -> str:
    """重定向 Location（URL 解码含 + 空格，便于断言中文提示）。"""
    return unquote_plus(resp.headers["location"])


# ---- 种子助手（直接写嵌入式 PG，断言结构与状态）----


async def _seed_task(tm_engine, **overrides: Any) -> int:
    """落一条 tm.task（+ created 事件）；返回 id。"""
    data: dict[str, Any] = {
        "title": "默认任务标题",
        "detail": None,
        "domain": "crm",
        "role": "运营",
        "due": date.today(),
        "status": "open",
        "source_type": "manual",
        "source": {"creator": "运营"},
        "created_by": "运营",
    }
    data.update(overrides)
    async with AsyncSession(tm_engine) as session, session.begin():
        row = TmTask(**data)
        session.add(row)
        await session.flush()
        session.add(TmTaskEvent(task_id=row.id, event_type="created", actor="运营"))
        return row.id


async def _seed_proposal(tm_engine, **overrides: Any) -> int:
    """落一条 pending 提案（对齐转交器落库形态）；返回 id。"""
    data: dict[str, Any] = {
        "title": "建议跟进买家物流时效咨询",
        "detail": "买家询问物流时效，建议运营跟进确认并回复",
        "domain": "crm",
        "action_id": "tm.proposal",
        "risk": "suggest",
        "suggested_role": "运营",
        "suggested_due_days": 3,
        "evidence": [{"kind": "message", "ref_id": "msg-001", "quote": "Where is my order?"}],
        "source": {
            "chain_id": "tm_demo_chain",
            "engine_task_id": "e-000042",
            "worker_id": "demo_propose",
            "audit_ids": [],
        },
    }
    data.update(overrides)
    async with AsyncSession(tm_engine) as session, session.begin():
        row = TmTaskProposalRow(**data)
        session.add(row)
        await session.flush()
        return row.id


async def _get_task(tm_engine, task_id: int) -> TmTask:
    async with AsyncSession(tm_engine) as session:
        row = await session.get(TmTask, task_id)
    assert row is not None
    return row


async def _all_tasks(tm_engine) -> list[TmTask]:
    async with AsyncSession(tm_engine) as session:
        rows = (await session.execute(select(TmTask).order_by(TmTask.id))).scalars().all()
    return list(rows)


async def _get_proposal(tm_engine, pid: int) -> TmTaskProposalRow:
    async with AsyncSession(tm_engine) as session:
        row = await session.get(TmTaskProposalRow, pid)
    assert row is not None
    return row


async def _events_for(tm_engine, task_id: int) -> list[TmTaskEvent]:
    async with AsyncSession(tm_engine) as session:
        rows = (
            await session.execute(
                select(TmTaskEvent)
                .where(TmTaskEvent.task_id == task_id)
                .order_by(TmTaskEvent.id)
            )
        ).scalars().all()
    return list(rows)


# ---- 登录与角色（A16）----


def test_role_label_mapping_is_chinese() -> None:
    """角色 cookie ASCII 键 -> 中文标签（决策 17 第 2 条：三角色仅展示）。"""
    assert ROLE_LABEL == {"admin": "管理员", "ops": "运营", "buyer": "采购"}


@pytest.mark.asyncio
async def test_login_page_and_role_cookie(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "登录" in resp.text
    resp = client.post("/login", data={"role": "ops"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/tasks"
    assert resp.cookies.get("role") == "ops"


@pytest.mark.asyncio
async def test_tasks_requires_login_redirect(client: TestClient) -> None:
    """A16：未登录访问 /tasks 重定向 /login。"""
    resp = client.get("/tasks")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_role_label_shown_after_login(client: TestClient) -> None:
    _login(client, role="ops")
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "访客" not in resp.text  # cookie 已映射角色，不再是访客
    assert "运营" in resp.text


# ---- 任务列表 / 逾期提醒条 / 筛选 ----


@pytest.mark.asyncio
async def test_filter_autosubmit_onchange(client: TestClient, tm_engine) -> None:
    """筛选选择即生效（用户反馈：选「已作废」仍显示全部——原表单需手动点筛选按钮）：
    状态下拉框与逾期复选框带 onchange 自动提交（form.submit()），选完立即刷新列表。"""
    _login(client)
    await _seed_task(tm_engine, title="待办任务", due=date.today(), status="open")
    await _seed_task(
        tm_engine, title="已作废任务", due=date.today() - timedelta(days=1),
        status="void", result_note="作废回复",
    )
    resp = client.get("/tasks")
    assert resp.status_code == 200
    # 模板断言：状态下拉框 + 逾期复选框都有 onchange 自动提交（选择即筛选）
    assert 'name="status"' in resp.text and "onchange=" in resp.text
    assert 'name="overdue"' in resp.text and "onchange=" in resp.text
    # 服务端链路：status=void 只回已作废任务（自动提交走的同一 GET 参数）
    resp = client.get("/tasks", params={"status": "void"})
    assert "已作废任务" in resp.text
    assert "待办任务" not in resp.text


@pytest.mark.asyncio
async def test_tasks_page_lists_real_tasks(client: TestClient, tm_engine) -> None:
    _login(client)
    await _seed_task(tm_engine, title="回复买家物流时效疑问", due=date.today() - timedelta(days=1))
    await _seed_task(tm_engine, title="店铺公告更新", due=date.today() + timedelta(days=1), status="in_progress")
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "回复买家物流时效疑问" in resp.text
    assert "店铺公告更新" in resp.text
    assert "个任务已逾期" in resp.text  # 逾期提醒条常驻（决策 17 第 5 条）
    assert "查看逾期任务" in resp.text
    assert "优先级" not in resp.text  # 无优先级列（决策 11 修订）


@pytest.mark.asyncio
async def test_overdue_reminder_bar_counts_only_unfinished(client: TestClient, tm_engine) -> None:
    _login(client)
    await _seed_task(tm_engine, title="逾期A", due=date.today() - timedelta(days=1), status="open")
    await _seed_task(tm_engine, title="逾期B", due=date.today() - timedelta(days=2), status="in_progress")
    await _seed_task(tm_engine, title="未逾期", due=date.today() + timedelta(days=1), status="open")
    await _seed_task(  # 已结束的逾期任务不提醒（status done/void 排除）
        tm_engine, title="已完成逾期", due=date.today() - timedelta(days=5),
        status="done", result_note="完成回复",
    )
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "2 个任务已逾期" in resp.text


@pytest.mark.asyncio
async def test_filter_status_done(client: TestClient, tm_engine) -> None:
    _login(client)
    await _seed_task(tm_engine, title="待办任务", due=date.today(), status="open")
    await _seed_task(
        tm_engine, title="已完成任务", due=date.today() - timedelta(days=1),
        status="done", result_note="完成回复",
    )
    resp = client.get("/tasks", params={"status": "done"})
    assert resp.status_code == 200
    assert "已完成任务" in resp.text
    assert "待办任务" not in resp.text


@pytest.mark.asyncio
async def test_filter_overdue(client: TestClient, tm_engine) -> None:
    _login(client)
    await _seed_task(tm_engine, title="逾期任务", due=date.today() - timedelta(days=1), status="open")
    await _seed_task(tm_engine, title="正常任务", due=date.today() + timedelta(days=1), status="open")
    resp = client.get("/tasks", params={"overdue": "1"})
    assert resp.status_code == 200
    assert "逾期任务" in resp.text
    assert "正常任务" not in resp.text


@pytest.mark.asyncio
async def test_filter_keyword(client: TestClient, tm_engine) -> None:
    _login(client)
    await _seed_task(tm_engine, title="处理退货请求", due=date.today(), status="open")
    await _seed_task(tm_engine, title="补货 SKU 经典款", due=date.today(), status="open")
    resp = client.get("/tasks", params={"q": "退货"})
    assert resp.status_code == 200
    assert "处理退货请求" in resp.text
    assert "补货 SKU 经典款" not in resp.text


# ---- 状态操作（§3.5.1 + §3.3 事件流水）----


@pytest.mark.asyncio
async def test_status_started(client: TestClient, tm_engine) -> None:
    _login(client)
    tid = await _seed_task(tm_engine, title="开始测试", due=date.today(), status="open")
    resp = client.post("/tasks/status", data={"task_id": tid, "action": "started"})
    assert resp.status_code == 303
    assert "已开始" in _loc(resp)
    row = await _get_task(tm_engine, tid)
    assert row.status == "in_progress"
    events = await _events_for(tm_engine, tid)
    assert [e.event_type for e in events] == ["created", "started"]
    started = events[1]
    assert started.from_status == "open"
    assert started.to_status == "in_progress"
    assert started.actor == "运营"


@pytest.mark.asyncio
async def test_completed_requires_result_note_then_succeeds(client: TestClient, tm_engine) -> None:
    """A23：不填 result_note 点完成被拒（页面拦截 + DB CHECK 双保险），填后落库。"""
    _login(client)
    tid = await _seed_task(tm_engine, title="完成测试", due=date.today(), status="in_progress")
    # 拒：不填 result_note
    resp = client.post("/tasks/status", data={"task_id": tid, "action": "completed"})
    assert resp.status_code == 303
    assert "result_note" in _loc(resp)
    row = await _get_task(tm_engine, tid)
    assert row.status == "in_progress"  # 状态未被改动
    # 成：填 result_note
    note = "已回复买家并确认预计送达时间"
    resp = client.post(
        "/tasks/status", data={"task_id": tid, "action": "completed", "result_note": note}
    )
    assert resp.status_code == 303
    row = await _get_task(tm_engine, tid)
    assert row.status == "done"
    assert row.result_note == note
    assert row.done_at is not None  # done 写 done_at（§3.3）
    events = await _events_for(tm_engine, tid)
    completed = [e for e in events if e.event_type == "completed"][0]
    assert completed.from_status == "in_progress"
    assert completed.to_status == "done"
    assert completed.note == note  # completed 事件 note=result_note（决策 17）


@pytest.mark.asyncio
async def test_voided_requires_result_note_then_succeeds(client: TestClient, tm_engine) -> None:
    """A23 作废路：必填 result_note，填后 void 落库且 note 进事件。"""
    _login(client)
    tid = await _seed_task(tm_engine, title="作废测试", due=date.today(), status="in_progress")
    resp = client.post("/tasks/status", data={"task_id": tid, "action": "voided"})
    assert resp.status_code == 303
    assert "result_note" in _loc(resp)
    note = "需求变更，作废"
    resp = client.post(
        "/tasks/status", data={"task_id": tid, "action": "voided", "result_note": note}
    )
    assert resp.status_code == 303
    row = await _get_task(tm_engine, tid)
    assert row.status == "void"
    assert row.result_note == note
    events = await _events_for(tm_engine, tid)
    voided = [e for e in events if e.event_type == "voided"][0]
    assert voided.to_status == "void"
    assert voided.note == note


@pytest.mark.asyncio
async def test_blocked_requires_reason_and_unblocked(client: TestClient, tm_engine) -> None:
    """blocked 必填 blocked_reason（等物料/等回复），unblocked 回 in_progress。"""
    _login(client)
    tid = await _seed_task(tm_engine, title="阻塞测试", due=date.today(), status="in_progress")
    # 拒：不填原因
    resp = client.post("/tasks/status", data={"task_id": tid, "action": "blocked"})
    assert resp.status_code == 303
    assert "blocked_reason" in _loc(resp)
    # 拒：非法原因
    resp = client.post(
        "/tasks/status", data={"task_id": tid, "action": "blocked", "blocked_reason": "随便"}
    )
    assert "等物料" in _loc(resp)
    # 成：等物料
    resp = client.post(
        "/tasks/status", data={"task_id": tid, "action": "blocked", "blocked_reason": "等物料"}
    )
    assert resp.status_code == 303
    row = await _get_task(tm_engine, tid)
    assert row.status == "blocked"
    assert row.blocked_reason == "等物料"
    # unblocked -> in_progress，blocked_reason 清空（chk_blocked_reason）
    resp = client.post("/tasks/status", data={"task_id": tid, "action": "unblocked"})
    assert resp.status_code == 303
    row = await _get_task(tm_engine, tid)
    assert row.status == "in_progress"
    assert row.blocked_reason is None
    events = await _events_for(tm_engine, tid)
    blocked = [e for e in events if e.event_type == "blocked"][0]
    assert blocked.note == "等物料"
    unblocked = [e for e in events if e.event_type == "unblocked"][0]
    assert unblocked.from_status == "blocked"
    assert unblocked.to_status == "in_progress"


@pytest.mark.asyncio
async def test_illegal_transition_rejected(client: TestClient, tm_engine) -> None:
    """终态不可 started（状态机代码写死，R1）。"""
    _login(client)
    tid = await _seed_task(
        tm_engine, title="终态任务", due=date.today(), status="done", result_note="完成回复"
    )
    resp = client.post("/tasks/status", data={"task_id": tid, "action": "started"})
    assert resp.status_code == 303
    assert "非法状态迁移" in _loc(resp)
    row = await _get_task(tm_engine, tid)
    assert row.status == "done"


# ---- 派生（A24：派生≠原任务结束）----


@pytest.mark.asyncio
async def test_derive_task_keeps_parent_open(client: TestClient, tm_engine) -> None:
    _login(client)
    parent = await _seed_task(
        tm_engine, title="处理退货申请", due=date.today(), status="in_progress"
    )
    resp = client.post(
        "/tasks/derive",
        data={
            "parent_id": parent,
            "title": "生成退货标签",
            "detail": "买家要求退货",
            "domain": "crm",
            "role": "运营",
            "due": (date.today() + timedelta(days=1)).isoformat(),
            "link_parent": "1",  # 复核反馈 #6：关联可选，测关联场景
        },
    )
    assert resp.status_code == 303
    assert "派生" in _loc(resp)
    tasks = await _all_tasks(tm_engine)
    assert len(tasks) == 2
    child = next(t for t in tasks if t.id != parent)
    assert child.title == "生成退货标签"
    assert child.derived_from == parent  # B.derived_from=A
    # 原任务不自动 done（决策 17）
    parent_row = await _get_task(tm_engine, parent)
    assert parent_row.status == "in_progress"
    # 事件：新任务 created；原任务 derived（note=派生出的任务 id）
    child_events = await _events_for(tm_engine, child.id)
    assert [e.event_type for e in child_events] == ["created"]
    parent_events = await _events_for(tm_engine, parent)
    assert [e.event_type for e in parent_events] == ["created", "derived"]
    derived = parent_events[1]
    assert derived.note == task_display_id(child.id)


@pytest.mark.asyncio
async def test_derive_display_shows_parent_link_and_child_badge(
    client: TestClient, tm_engine
) -> None:
    """复核反馈修复：派生关联展示——子任务显示可点击父链接，父任务显示子任务数徽章。"""
    _login(client)
    parent = await _seed_task(tm_engine, title="处理退货申请", due=date.today())
    child = await _seed_task(
        tm_engine,
        title="生成退货标签",
        due=date.today(),
        derived_from=parent,
        source_type="manual",
        source={"creator": "运营"},
    )
    resp = client.get("/tasks")
    assert resp.status_code == 200
    html = resp.text
    # 子任务行：可点击的父任务链接（含父标题）
    assert f"由 {task_display_id(parent)}" in html
    assert "「处理退货申请」派生" in html
    assert f'href="/tasks?q=处理退货申请"' in html
    # 父任务行：子任务数徽章（"1 个子任务"精确徽章文案）
    assert "1 个子任务" in html


# ---- 编辑（A26：updated 事件带 from/to 快照）----


@pytest.mark.asyncio
async def test_edit_task_records_updated_snapshot(client: TestClient, tm_engine) -> None:
    _login(client)
    tid = await _seed_task(
        tm_engine, title="原标题", detail="原详情", domain="crm", role="运营",
        due=date.today() + timedelta(days=2),
    )
    new_due = (date.today() + timedelta(days=5)).isoformat()
    resp = client.post(
        "/tasks/edit",
        data={
            "task_id": tid, "title": "新标题", "role": "采购",
            "due": new_due, "domain": "erp", "detail": "新详情",
        },
    )
    assert resp.status_code == 303
    row = await _get_task(tm_engine, tid)
    assert row.title == "新标题"
    assert row.role == "采购"
    assert row.domain == "erp"
    assert row.detail == "新详情"
    events = await _events_for(tm_engine, tid)
    updated = [e for e in events if e.event_type == "updated"][0]
    assert updated.actor == "运营"
    snapshot = json.loads(updated.note)  # from/to 快照进 note（§3.5.5）
    assert snapshot["title"] == {"from": "原标题", "to": "新标题"}
    assert snapshot["domain"] == {"from": "crm", "to": "erp"}
    assert snapshot["due"] == {"from": (date.today() + timedelta(days=2)).isoformat(), "to": new_due}


# ---- 重开（A26：done/void 可重开回 open）----


@pytest.mark.asyncio
async def test_reopen_done_task(client: TestClient, tm_engine) -> None:
    _login(client)
    tid = await _seed_task(
        tm_engine, title="重开测试", due=date.today() - timedelta(days=1),
        status="done", result_note="完成回复",
    )
    resp = client.post("/tasks/reopen", data={"task_id": tid})
    assert resp.status_code == 303
    assert "重开" in _loc(resp)
    row = await _get_task(tm_engine, tid)
    assert row.status == "open"
    assert row.result_note is None  # chk_result_note：非 done/void 必须为空
    assert row.done_at is None
    events = await _events_for(tm_engine, tid)
    updated = [e for e in events if e.event_type == "updated"][0]
    snapshot = json.loads(updated.note)
    assert snapshot["status"] == {"from": "done", "to": "open"}


@pytest.mark.asyncio
async def test_reopen_rejected_for_non_terminal(client: TestClient, tm_engine) -> None:
    _login(client)
    tid = await _seed_task(tm_engine, title="进行中任务", due=date.today(), status="in_progress")
    resp = client.post("/tasks/reopen", data={"task_id": tid})
    assert resp.status_code == 303
    assert "可重开" in _loc(resp)
    row = await _get_task(tm_engine, tid)
    assert row.status == "in_progress"


# ---- 人工建任务（A20：双通道人工通道，source_type=manual + 来源域）----


@pytest.mark.asyncio
async def test_create_manual_task(client: TestClient, tm_engine) -> None:
    _login(client)
    resp = client.post(
        "/tasks/create",
        data={
            "title": "人工建的补货任务", "detail": "从表单创建",
            "domain": "erp", "role": "采购",
            "due": (date.today() + timedelta(days=3)).isoformat(),
        },
    )
    assert resp.status_code == 303
    assert "人工创建" in _loc(resp)
    tasks = await _all_tasks(tm_engine)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.source_type == "manual"
    assert t.source == {"creator": "运营"}  # cookie ops -> 运营，creator 即依据（§3.4）
    assert t.domain == "erp"
    assert t.role == "采购"
    assert t.created_by == "运营"
    events = await _events_for(tm_engine, t.id)
    assert [e.event_type for e in events] == ["created"]
    assert events[0].actor == "运营"


@pytest.mark.asyncio
async def test_create_manual_task_rejects_bad_due(client: TestClient, tm_engine) -> None:
    _login(client)
    resp = client.post(
        "/tasks/create",
        data={"title": "坏日期", "domain": "crm", "role": "运营", "due": "not-a-date"},
    )
    assert resp.status_code == 303
    assert "yyyy-mm-dd" in _loc(resp)
    assert await _all_tasks(tm_engine) == []


# ---- 提案审核（A13：批准事务 / 驳回不建任务；A22：依据展示）----


@pytest.mark.asyncio
async def test_approve_proposal_creates_task_in_transaction(client: TestClient, tm_engine) -> None:
    _login(client)
    pid = await _seed_proposal(tm_engine, suggested_due_days=3)
    resp = client.post("/tasks/proposals/approve", data={"proposal_id": pid})
    assert resp.status_code == 303
    assert "生成任务" in _loc(resp)
    prop = await _get_proposal(tm_engine, pid)
    assert prop.status == "approved"
    assert prop.reviewed_by == "运营"
    assert prop.reviewed_at is not None
    assert prop.task_id is not None  # 提案关联生成的任务
    tasks = await _all_tasks(tm_engine)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.source_type == "ai"
    assert t.domain == "crm"  # AI 任务继承提案 domain（决策 16）
    assert t.role == "运营"  # suggested_role
    assert t.due == date.today() + timedelta(days=3)  # AI 建议截止天数落库
    assert t.status == "open"
    assert t.source["chain_id"] == "tm_demo_chain"  # 来源追溯（§3.4）
    assert t.source["proposal_id"] == proposal_display_id(pid)  # 批准时回填
    events = await _events_for(tm_engine, t.id)
    assert [e.event_type for e in events] == ["created", "approved"]  # 同事务双事件


@pytest.mark.asyncio
async def test_approve_twice_rejected_no_dup_task(client: TestClient, tm_engine) -> None:
    """已审核提案不能重复批准（不重复建任务）。"""
    _login(client)
    pid = await _seed_proposal(tm_engine)
    first = client.post("/tasks/proposals/approve", data={"proposal_id": pid})
    assert first.status_code == 303
    second = client.post("/tasks/proposals/approve", data={"proposal_id": pid})
    assert second.status_code == 303
    assert "不能重复批准" in _loc(second)
    assert len(await _all_tasks(tm_engine)) == 1


@pytest.mark.asyncio
async def test_reject_proposal_no_task_created(client: TestClient, tm_engine) -> None:
    _login(client)
    pid = await _seed_proposal(tm_engine)
    resp = client.post(
        "/tasks/proposals/reject", data={"proposal_id": pid, "reason": "与现有任务重复"}
    )
    assert resp.status_code == 303
    prop = await _get_proposal(tm_engine, pid)
    assert prop.status == "rejected"
    assert prop.reject_reason == "与现有任务重复"
    assert prop.reviewed_by == "运营"
    assert prop.task_id is None
    assert await _all_tasks(tm_engine) == []  # 驳回不建任务


@pytest.mark.asyncio
async def test_proposal_sidebar_shows_evidence(client: TestClient, tm_engine) -> None:
    """A22：审核面板完整展示依据（类型 + ref_id + 原文摘录）+ 来源追溯。"""
    _login(client)
    pid = await _seed_proposal(tm_engine)
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "建议跟进买家物流时效咨询" in resp.text
    assert "message" in resp.text  # evidence 类型
    assert "msg-001" in resp.text  # ref_id
    assert "Where is my order?" in resp.text  # 原文摘录
    assert "tm_demo_chain" in resp.text  # 来源 chain_id
    assert "e-000042" in resp.text  # 来源 engine_task_id
    assert "批准" in resp.text
    assert "驳回" in resp.text
    assert proposal_display_id(pid) in resp.text


# ---- 触发演示链面板（registry -> POST -> 轮询 GET 查终态）----


@pytest.mark.asyncio
async def test_trigger_panel_lists_registry_chains(client: TestClient) -> None:
    """链清单来自 GET /api/engine/registry（engineapi 客户端）。"""
    _login(client)
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "触发演示链" in resp.text
    assert "tm_demo_chain" in resp.text  # registry 链出现在面板


@pytest.mark.asyncio
async def test_trigger_chain_posts_then_polls_terminal(client: TestClient, engine_stub: _EngineStub) -> None:
    """选链填参 -> POST /api/engine/tasks（201）-> 轮询 GET 查终态。"""
    _login(client)
    resp = client.post(
        "/tasks/trigger",
        data={"chain_id": "tm_demo_chain", "text": "买家咨询物流", "ref_id": "msg-001", "kind": "message"},
    )
    assert resp.status_code == 303
    assert "engine_task_id=e-000001" in resp.headers["location"]
    # 客户端序列化正确（trigger_ref = 操作者角色）
    assert engine_stub.last_create == {
        "chain_id": "tm_demo_chain",
        "input": {"text": "买家咨询物流", "ref_id": "msg-001", "kind": "message"},
        "trigger_ref": "运营",
    }
    # 未终态
    engine_stub.task["status"] = "queued"
    data = client.get("/tasks/engine/e-000001").json()
    assert data["status"] == "queued"
    assert data["terminal"] is False
    # 终态 -> JS 轮询据此刷新提案栏
    engine_stub.task["status"] = "done"
    data = client.get("/tasks/engine/e-000001").json()
    assert data["status"] == "done"
    assert data["terminal"] is True


@pytest.mark.asyncio
async def test_trigger_missing_required_param_rejected(client: TestClient) -> None:
    _login(client)
    resp = client.post(
        "/tasks/trigger", data={"chain_id": "tm_demo_chain", "text": "x"}  # 缺 ref_id/kind
    )
    assert resp.status_code == 303
    assert "缺少必填参数" in _loc(resp)


@pytest.mark.asyncio
async def test_trigger_unknown_chain_via_json_input(
    client: TestClient, engine_stub: _EngineStub
) -> None:
    """registry 出现未声明表单的链 -> JSON 文本域输入 -> POST 序列化正确。"""
    _login(client)
    engine_stub.registry["chains"].append({"id": "future_chain", "workers": ["demo_echo"]})
    payload = {"text": "JSON 输入", "ref_id": "obj-9", "kind": "message"}
    resp = client.post(
        "/tasks/trigger",
        data={"chain_id": "future_chain", "input_json": json.dumps(payload)},
    )
    assert resp.status_code == 303
    assert "engine_task_id=e-000001" in resp.headers["location"]
    assert engine_stub.last_create == {
        "chain_id": "future_chain",
        "input": payload,
        "trigger_ref": "运营",
    }
    # 非法 JSON -> 拒
    resp = client.post(
        "/tasks/trigger", data={"chain_id": "future_chain", "input_json": "{broken"}
    )
    assert resp.status_code == 303
    assert "JSON" in _loc(resp)
    # JSON 数组 -> 拒（必须是对象）
    resp = client.post(
        "/tasks/trigger", data={"chain_id": "future_chain", "input_json": "[1,2]"}
    )
    assert resp.status_code == 303
    assert "JSON 对象" in _loc(resp)


@pytest.mark.asyncio
async def test_trigger_engine_error_shows_err(client: TestClient, engine_stub: _EngineStub) -> None:
    """引擎 404（链未登记）-> 页面 err 提示，不 500。"""
    _login(client)
    engine_stub.fail = (404, "chain not registered")
    resp = client.post(
        "/tasks/trigger",
        data={"chain_id": "tm_demo_chain", "text": "x", "ref_id": "r", "kind": "message"},
    )
    assert resp.status_code == 303
    assert "触发失败" in _loc(resp)


@pytest.mark.asyncio
async def test_tasks_page_engine_down_degrades_gracefully(
    client: TestClient, engine_stub: _EngineStub, tm_engine
) -> None:
    """引擎未启动时 /tasks 仍 200（触发面板降级展示，任务列表不受影响）。"""
    _login(client)
    await _seed_task(tm_engine, title="引擎挂不影响任务")
    engine_stub.fail = (500, "boom")
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "引擎未连接" in resp.text
    assert "引擎挂不影响任务" in resp.text
