"""v0.3 T3（主代理接管实现）：业务读写接口 /api/biz/* 测试（决策 26 接口化）。

覆盖（详设 §6 + 任务书验收）：
- 鉴权：无 token / 错 token -> 401；正确 token 放行
- 写接口 POST /api/biz/tm/proposals：正常落库（pending/evidence 非空/source 对齐）/
  幂等（同 engine_task_id+worker_id 重复提交 skipped + 只落一条）/
  校验失败 422（evidence 空）/
  同事件防重（同 ref_id+action_id 已有待审提案 -> skipped）
- 写接口 POST /api/biz/crm/candidates：customer 404 / 正常落库 / 幂等
- 读接口 GET /api/biz/crm/context/{id}：组装（customer/messages/snapshot/
  existing_open_todos 含 tm 任务标题 + pending 候选）/ 404
- 读接口 GET /api/biz/tm/task-context/{id}：tags/events 组装 / 404

基建：tm_pg_cluster（嵌入式 PG，tm + crm 两 schema）+ NullPool engine +
TestClient（TestClient portal 循环与 pytest 循环不串，承 test_web_tm 模式）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from models.contract.task import EvidenceRef, SourceTrace
from models.crm import Customer, Message, Snapshot, TodoCandidate
from models.tm import Task, TaskProposal as TaskProposalORM
from web.api_biz import TodoCandidateWrite, create_biz_router

_BIZ_TOKEN = "test-biz-" + "token"


# ---- fixtures ----


@async_fixture
async def biz_engine(tm_pg_cluster):
    """业务库 AsyncEngine（NullPool：TestClient portal 循环与 pytest 循环不串）。"""
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean_biz_tables(biz_engine):
    """每测试后 TRUNCATE crm 四表 + tm 三表（RESTART IDENTITY CASCADE）。"""
    yield
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE crm.todo_candidate, crm.snapshot, crm.message, crm.customer, "
                "tm.task_event, tm.task_proposal, tm.task RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def biz_client(biz_engine) -> TestClient:
    app = FastAPI()
    app.include_router(create_biz_router(engine=biz_engine, token=_BIZ_TOKEN))
    return TestClient(app)


def _headers(token: str | None = _BIZ_TOKEN) -> dict:
    return {"X-Biz-Token": token} if token else {}


def _proposal_payload(**overrides) -> dict:
    base = {
        "title": "确认花材组合及婚礼日期",
        "detail": "买家需要确认花材组合与婚礼日期以便报价",
        "domain": "crm",
        "action_id": "tm.proposal",
        "suggested_role": "运营",
        "suggested_due_days": 1,
        "evidence": [
            {"kind": "message", "ref_id": "1", "quote": "Could you confirm the flowers?"}
        ],
        "source": {
            "chain_id": "crm_chat_chain",
            "engine_task_id": "e-000001",
            "worker_id": "todo_generate",
            "audit_ids": [],
        },
        "risk": "suggest",
    }
    base.update(overrides)
    return base


def _candidate_payload(**overrides) -> dict:
    base = {
        "customer_id": 1,
        "content": "确认花材组合及婚礼日期",
        "reason": "卖家在对话中明确答应确认花材",
        "suggested_tags": ["报价"],
        "evidence": [
            {"kind": "message", "ref_id": "1", "quote": "I can confirm the flowers for you"}
        ],
        "source": {
            "chain_id": "crm_chat_chain",
            "engine_task_id": "e-000002",
            "worker_id": "todo_generate",
            "audit_ids": [],
        },
    }
    base.update(overrides)
    return base


@async_fixture
async def _seed_customer(biz_engine) -> int:
    """建一个客户 + 2 条消息 + 1 快照（读接口组装用）。"""
    async with AsyncSession(biz_engine) as session, session.begin():
        customer = Customer(nickname="Mia", source_shop="成品", remark="")
        session.add(customer)
        await session.flush()
        session.add_all(
            [
                Message(
                    customer_id=customer.id,
                    source_text="Hi! I love your flowers",
                    translated_text="你好！我很喜欢你的花",
                    direction="buyer",
                    language="en",
                ),
                Message(
                    customer_id=customer.id,
                    source_text="Can you make it smaller?",
                    translated_text="能做小一点吗？",
                    direction="buyer",
                    language="en",
                ),
            ]
        )
        session.add(
            Snapshot(
                customer_id=customer.id,
                current_need="定制小花束",
                need_history=["首次联系：询问定制"],
                sentiment="积极",
                todos=[],
                summary="买家想定制",
            )
        )
        return customer.id


# ---- 鉴权 ----


def test_unauthorized_missing_token(biz_client: TestClient) -> None:
    resp = biz_client.post("/api/biz/tm/proposals", json=_proposal_payload(), headers={})
    assert resp.status_code == 401


def test_unauthorized_wrong_token(biz_client: TestClient) -> None:
    resp = biz_client.post(
        "/api/biz/tm/proposals", json=_proposal_payload(), headers=_headers("wrong")
    )
    assert resp.status_code == 401


def test_authorized_ok(biz_client: TestClient, biz_engine) -> None:
    resp = biz_client.post(
        "/api/biz/tm/proposals", json=_proposal_payload(), headers=_headers()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert resp.json()["skipped"] is False
    assert resp.json()["id"] > 0


# ---- TM 提案写接口 ----


@pytest.mark.asyncio
async def test_proposal_persisted(biz_client: TestClient, biz_engine) -> None:
    biz_client.post("/api/biz/tm/proposals", json=_proposal_payload(), headers=_headers())
    async with AsyncSession(biz_engine) as session:
        rows = (await session.execute(text("SELECT id, status, risk, evidence FROM tm.task_proposal"))).all()
    assert len(rows) == 1
    status, risk = rows[0][1], rows[0][2]
    assert status == "pending"
    assert risk == "suggest"
    assert len(rows[0][3]) == 1  # evidence 落库


@pytest.mark.asyncio
async def test_proposal_idempotent(biz_client: TestClient, biz_engine) -> None:
    biz_client.post("/api/biz/tm/proposals", json=_proposal_payload(), headers=_headers())
    resp = biz_client.post(
        "/api/biz/tm/proposals", json=_proposal_payload(), headers=_headers()
    )
    assert resp.json()["skipped"] is True
    async with AsyncSession(biz_engine) as session:
        count = (
            await session.execute(text("SELECT count(*) FROM tm.task_proposal"))
        ).scalar_one()
    assert count == 1


def test_proposal_empty_evidence_rejected(biz_client: TestClient) -> None:
    payload = _proposal_payload(evidence=[])
    resp = biz_client.post("/api/biz/tm/proposals", json=payload, headers=_headers())
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_proposal_duplicate_skip(biz_client: TestClient, biz_engine) -> None:
    """同 ref_id + action_id 已有待审提案 -> 本次 skipped（决策 17 防重）。"""
    biz_client.post("/api/biz/tm/proposals", json=_proposal_payload(), headers=_headers())
    resp = biz_client.post(
        "/api/biz/tm/proposals",
        json=_proposal_payload(
            source={
                "chain_id": "crm_chat_chain",
                "engine_task_id": "e-000099",
                "worker_id": "todo_generate",
                "audit_ids": [],
            }
        ),
        headers=_headers(),
    )
    assert resp.json()["skipped"] is True
    async with AsyncSession(biz_engine) as session:
        count = (
            await session.execute(text("SELECT count(*) FROM tm.task_proposal"))
        ).scalar_one()
    assert count == 1


# ---- CRM 候选写接口 ----


def test_candidate_customer_missing(biz_client: TestClient) -> None:
    resp = biz_client.post(
        "/api/biz/crm/candidates", json=_candidate_payload(customer_id=999), headers=_headers()
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_candidate_create_ok(biz_client: TestClient, biz_engine, _seed_customer) -> None:
    resp = biz_client.post(
        "/api/biz/crm/candidates", json=_candidate_payload(), headers=_headers()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    async with AsyncSession(biz_engine) as session:
        row = (
            await session.execute(
                text("SELECT content, status, evidence FROM crm.todo_candidate")
            )
        ).one()
    assert row[0] == "确认花材组合及婚礼日期"
    assert row[1] == "pending"
    assert len(row[2]) == 1


@pytest.mark.asyncio
async def test_candidate_idempotent(biz_client: TestClient, biz_engine, _seed_customer) -> None:
    biz_client.post("/api/biz/crm/candidates", json=_candidate_payload(), headers=_headers())
    resp = biz_client.post(
        "/api/biz/crm/candidates", json=_candidate_payload(), headers=_headers()
    )
    assert resp.json()["skipped"] is True
    async with AsyncSession(biz_engine) as session:
        count = (
            await session.execute(text("SELECT count(*) FROM crm.todo_candidate"))
        ).scalar_one()
    assert count == 1


# ---- CRM 上下文读接口 ----


@pytest.mark.asyncio
async def test_crm_context_assembly(biz_client: TestClient, biz_engine, _seed_customer) -> None:
    cid = _seed_customer
    # 预置：该客户 1 条未完成任务 + 1 条 pending 候选（existing_open_todos 应含两者）
    async with AsyncSession(biz_engine) as session, session.begin():
        task = Task(
            title="确认花材组合及婚礼日期",
            detail="",
            domain="crm",
            role="运营",
            due=date(2026, 8, 31),
            source_type="ai",
            created_by="运营",
            source={"customer_id": cid, "chain_id": "crm_chat_chain"},
        )
        session.add(task)
        session.add(
            TodoCandidate(
                customer_id=cid,
                content="提供报价和交期",
                reason="买家问报价",
                evidence=[{"kind": "message", "ref_id": "1", "quote": "how much"}],
                status="pending",
            )
        )
    resp = biz_client.get(f"/api/biz/crm/context/{cid}", headers=_headers())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["customer"]["nickname"] == "Mia"
    assert len(data["messages"]) == 2
    assert data["messages"][0]["source_text"] == "Hi! I love your flowers"
    assert data["snapshot"]["current_need"] == "定制小花束"
    assert "确认花材组合及婚礼日期" in data["existing_open_todos"]
    assert "提供报价和交期" in data["existing_open_todos"]


def test_crm_context_404(biz_client: TestClient) -> None:
    resp = biz_client.get("/api/biz/crm/context/999", headers=_headers())
    assert resp.status_code == 404


# ---- TM 任务上下文读接口 ----


@pytest.mark.asyncio
async def test_task_context_assembly(biz_client: TestClient, biz_engine) -> None:
    async with AsyncSession(biz_engine) as session, session.begin():
        task = Task(
            title="处理退货",
            detail="",
            domain="crm",
            role="运营",
            due=date(2026, 8, 31),
            source_type="ai",
            created_by="运营",
            source={"customer_id": 1},
            tags=["售后"],
            ai_suggestion={"action": "transfer", "target_domain": "erp", "note": "建议流转"},
        )
        session.add(task)
        await session.flush()
        task_id = task.id
    resp = biz_client.get(f"/api/biz/tm/task-context/{task_id}", headers=_headers())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["task_id"] == task_id
    assert data["title"] == "处理退货"
    assert data["status"] == "open"
    assert data["tags"] == ["售后"]
    assert isinstance(data["recent_events"], list)


def test_task_context_404(biz_client: TestClient) -> None:
    resp = biz_client.get("/api/biz/tm/task-context/999", headers=_headers())
    assert resp.status_code == 404


# =====================================================================
# GET /api/biz/crm/overdue-customers
# =====================================================================


def test_overdue_customers_unauthorized(biz_client: TestClient) -> None:
    """未带 token -> 401。"""
    resp = biz_client.get("/api/biz/crm/overdue-customers", headers={})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_overdue_customers_empty(biz_client: TestClient, biz_engine) -> None:
    """没有客户 -> 返回空列表。"""
    resp = biz_client.get("/api/biz/crm/overdue-customers", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_overdue_customers_filter(biz_client: TestClient, biz_engine) -> None:
    """在 follow_up_days 内的客户不返回，超出的返回且 days_since 正确。

    默认 follow_up_days = 5（SettingsStore 读 sys.settings 无此 key 时回退默认）。
    """
    now = datetime.now(timezone.utc)
    # 客户 A：3 天前更新 → 在 5 天内 → 不返回
    recent_date = now - timedelta(days=3)
    # 客户 B：10 天前更新 → 超出 5 天 → 返回
    old_date = now - timedelta(days=10)
    async with AsyncSession(biz_engine) as session, session.begin():
        c1 = Customer(nickname="RecentBuyer", source_shop="shop1", remark="")
        session.add(c1)
        await session.flush()
        c2 = Customer(nickname="OldBuyer", source_shop="shop2", remark="")
        session.add(c2)
        await session.flush()
        # 更新 updated_at 为指定时间
        await session.execute(
            text(
                "UPDATE crm.customer SET updated_at = :dt WHERE id = :id"
            ),
            {"dt": recent_date, "id": c1.id},
        )
        await session.execute(
            text(
                "UPDATE crm.customer SET updated_at = :dt WHERE id = :id"
            ),
            {"dt": old_date, "id": c2.id},
        )
    resp = biz_client.get("/api/biz/crm/overdue-customers", headers=_headers())
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["nickname"] == "OldBuyer"
    assert items[0]["days_since"] == 10


@pytest.mark.asyncio
async def test_overdue_customers_archived_excluded(
    biz_client: TestClient, biz_engine
) -> None:
    """归档客户即使超期也不返回。"""
    now = datetime.now(timezone.utc)
    old_date = now - timedelta(days=10)
    async with AsyncSession(biz_engine) as session, session.begin():
        c = Customer(nickname="ArchivedBuyer", source_shop="shop1", remark="")
        session.add(c)
        await session.flush()
        await session.execute(
            text(
                "UPDATE crm.customer SET updated_at = :dt, follow_up_status = 'archived' "
                "WHERE id = :id"
            ),
            {"dt": old_date, "id": c.id},
        )
    resp = biz_client.get("/api/biz/crm/overdue-customers", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_overdue_customers_on_hold_included(
    biz_client: TestClient, biz_engine
) -> None:
    """on_hold 客户如果超期，应被包含。"""
    now = datetime.now(timezone.utc)
    old_date = now - timedelta(days=7)
    async with AsyncSession(biz_engine) as session, session.begin():
        c = Customer(nickname="OnHoldBuyer", source_shop="shop1", remark="")
        session.add(c)
        await session.flush()
        await session.execute(
            text(
                "UPDATE crm.customer SET updated_at = :dt, follow_up_status = 'on_hold' "
                "WHERE id = :id"
            ),
            {"dt": old_date, "id": c.id},
        )
    resp = biz_client.get("/api/biz/crm/overdue-customers", headers=_headers())
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["nickname"] == "OnHoldBuyer"


@pytest.mark.asyncio
async def test_overdue_customers_sorted_by_days(
    biz_client: TestClient, biz_engine
) -> None:
    """多个超期客户按 days_since 降序排列。"""
    now = datetime.now(timezone.utc)
    dates = {
        "Alpha": now - timedelta(days=8),
        "Bravo": now - timedelta(days=20),
        "Charlie": now - timedelta(days=15),
    }
    async with AsyncSession(biz_engine) as session, session.begin():
        for name, dt in dates.items():
            c = Customer(nickname=name, source_shop="shop1", remark="")
            session.add(c)
            await session.flush()
            await session.execute(
                text("UPDATE crm.customer SET updated_at = :dt WHERE id = :id"),
                {"dt": dt, "id": c.id},
            )
    resp = biz_client.get("/api/biz/crm/overdue-customers", headers=_headers())
    assert resp.status_code == 200
    items = resp.json()
    assert [it["nickname"] for it in items] == ["Bravo", "Charlie", "Alpha"]
    assert items[0]["days_since"] > items[1]["days_since"] > items[2]["days_since"]


@pytest.mark.asyncio
async def test_overdue_customers_latest_summary(
    biz_client: TestClient, biz_engine
) -> None:
    """返回的 latest_summary 来自 crm.customer.latest_summary。"""
    now = datetime.now(timezone.utc)
    old_date = now - timedelta(days=10)
    async with AsyncSession(biz_engine) as session, session.begin():
        c = Customer(
            nickname="SummaryBuyer",
            source_shop="shop1",
            remark="",
            latest_summary="买家想定制婚礼花束",
        )
        session.add(c)
        await session.flush()
        await session.execute(
            text("UPDATE crm.customer SET updated_at = :dt WHERE id = :id"),
            {"dt": old_date, "id": c.id},
        )
    resp = biz_client.get("/api/biz/crm/overdue-customers", headers=_headers())
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["latest_summary"] == "买家想定制婚礼花束"


# =====================================================================
# POST /api/biz/tm/schedule-tasks
# =====================================================================


def _schedule_task_payload(**overrides) -> dict:
    """构建 POST /api/biz/tm/schedule-tasks 的请求体。"""
    base = {
        "title": "跟进客户：Mia（已 10 天未跟进）",
        "detail": "买家想定制",
        "domain": "crm",
        "source": {
            "chain_id": "crm_reminder_chain",
            "engine_task_id": "e-000001",
            "worker_id": "crm_follow_up_reminder",
            "customer_id": 1,
            "reminder_date": "2026-09-01",
            "audit_ids": [],
        },
        "role": "运营",
        "due": "2026-09-01",
        "evidence": [{"kind": "message", "ref_id": "1", "quote": "Mia"}],
    }
    base.update(overrides)
    return base


def test_schedule_task_unauthorized(biz_client: TestClient) -> None:
    """未带 token -> 401。"""
    resp = biz_client.post(
        "/api/biz/tm/schedule-tasks",
        json=_schedule_task_payload(),
        headers={},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_schedule_task_ok(biz_client: TestClient, biz_engine) -> None:
    """合法 payload -> 200，tm.task 落库且字段正确。"""
    resp = biz_client.post(
        "/api/biz/tm/schedule-tasks",
        json=_schedule_task_payload(),
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["id"] > 0
    # 验证 tm.task 落库
    async with AsyncSession(biz_engine) as session:
        row = await session.execute(
            text(
                "SELECT title, domain, status, source_type, source, created_by "
                "FROM tm.task WHERE id = :id"
            ),
            {"id": body["id"]},
        )
        rec = row.one()
        assert rec[0] == "跟进客户：Mia（已 10 天未跟进）"
        assert rec[1] == "crm"
        assert rec[2] == "open"
        assert rec[3] == "schedule"
        assert rec[4]["customer_id"] == 1
        assert rec[4]["reminder_date"] == "2026-09-01"
        assert rec[5] == "运营"
    # 验证 task_event 也创建了
    async with AsyncSession(biz_engine) as session:
        ev_rows = await session.execute(
            text("SELECT event_type FROM tm.task_event WHERE task_id = :id"),
            {"id": body["id"]},
        )
        assert ev_rows.scalar_one() == "created"


@pytest.mark.asyncio
async def test_schedule_task_duplicate_409(
    biz_client: TestClient, biz_engine
) -> None:
    """同一 customer_id + reminder_date 第二次提交 -> 409。"""
    biz_client.post(
        "/api/biz/tm/schedule-tasks",
        json=_schedule_task_payload(),
        headers=_headers(),
    )
    resp = biz_client.post(
        "/api/biz/tm/schedule-tasks",
        json=_schedule_task_payload(),
        headers=_headers(),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_schedule_task_next_day_allowed(
    biz_client: TestClient, biz_engine
) -> None:
    """不同 reminder_date -> 允许（不 409）。"""
    biz_client.post(
        "/api/biz/tm/schedule-tasks",
        json=_schedule_task_payload(),
        headers=_headers(),
    )
    resp = biz_client.post(
        "/api/biz/tm/schedule-tasks",
        json=_schedule_task_payload(
            source={
                "chain_id": "crm_reminder_chain",
                "engine_task_id": "e-000001",
                "worker_id": "crm_follow_up_reminder",
                "customer_id": 1,
                "reminder_date": "2026-09-02",
                "audit_ids": [],
            },
            due="2026-09-02",
        ),
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_schedule_task_missing_customer_id(biz_client: TestClient) -> None:
    """source.customer_id=0 -> 422（不能为空）。"""
    resp = biz_client.post(
        "/api/biz/tm/schedule-tasks",
        json=_schedule_task_payload(
            source={
                "chain_id": "crm_reminder_chain",
                "engine_task_id": "e-000001",
                "worker_id": "crm_follow_up_reminder",
                "customer_id": 0,
                "reminder_date": "2026-09-01",
                "audit_ids": [],
            }
        ),
        headers=_headers(),
    )
    assert resp.status_code == 422


def test_schedule_task_missing_reminder_date(biz_client: TestClient) -> None:
    """source.reminder_date='' -> 422（不能为空）。"""
    resp = biz_client.post(
        "/api/biz/tm/schedule-tasks",
        json=_schedule_task_payload(
            source={
                "chain_id": "crm_reminder_chain",
                "engine_task_id": "e-000001",
                "worker_id": "crm_follow_up_reminder",
                "customer_id": 1,
                "reminder_date": "",
                "audit_ids": [],
            }
        ),
        headers=_headers(),
    )
    assert resp.status_code == 422


def test_schedule_task_empty_evidence(biz_client: TestClient) -> None:
    """evidence=[] -> 422（无依据不出建议）。"""
    resp = biz_client.post(
        "/api/biz/tm/schedule-tasks",
        json=_schedule_task_payload(evidence=[]),
        headers=_headers(),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_schedule_task_void_doesnt_block(
    biz_client: TestClient, biz_engine
) -> None:
    """已有 status='void' 的任务不影响新任务创建（防重只排除非 void）。"""
    # 先手动插一条 void 任务，模拟同一 customer_id + reminder_date
    async with AsyncSession(biz_engine) as session, session.begin():
        task = Task(
            title="旧提醒",
            detail="",
            domain="crm",
            role="运营",
            due=date(2026, 9, 1),
            source_type="schedule",
            source={
                "chain_id": "crm_reminder_chain",
                "engine_task_id": "e-999999",
                "worker_id": "crm_follow_up_reminder",
                "customer_id": 1,
                "reminder_date": "2026-09-01",
                "audit_ids": [],
            },
            tags=[],
            created_by="运营",
            status="void",
            result_note="已手动处理",
        )
        session.add(task)
    # 新任务同 customer_id + reminder_date -> 应允许（void 不阻塞）
    resp = biz_client.post(
        "/api/biz/tm/schedule-tasks",
        json=_schedule_task_payload(),
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
