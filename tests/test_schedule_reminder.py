"""v0.4 schedule_reminder 消费者（engine/actions/schedule_reminder.py）测试。

链末步产出 ReminderResult（reminders 数组）-> 消费者（action_id=tm.schedule）：
1. 契约归一（dict -> ReminderResult）；
2. 校验三件套：evidence 非空 / ref_id 命中白名单 / source 完整；
3. HTTP 写接口落库：POST /api/biz/tm/schedule-tasks；
4. 结果映射：inserted / skipped_idempotent / rejected / RuntimeError。

FakeBizClient 模拟 BizApiClient.post()，零网络零真服务（R12 注入式）。
"""

from __future__ import annotations

import httpx
import pytest

from engine.actions.schedule_reminder import consume_schedule_reminder
from engine.actions.tm_proposal import ConsumeOutcome


# ---------------------------------------------------------------------------
# FakeBizClient：模拟 BizApiClient.post()，按序返回预设响应
# ---------------------------------------------------------------------------


class FakeBizClient:
    def __init__(self, responses=None):
        self.responses = responses or []
        self.calls = []
        self._idx = 0

    async def post(self, path, payload):
        self.calls.append((path, payload))
        if self._idx < len(self.responses):
            resp = self.responses[self._idx]
            self._idx += 1
            return resp
        return httpx.Response(200, json={"ok": True, "id": 1})


# ---------------------------------------------------------------------------
# 标准 deliverable / source / whitelist
# ---------------------------------------------------------------------------

_STANDARD_DELIVERABLE: dict = {
    "reminders": [
        {
            "customer_id": 1,
            "title": "跟进客户：Mia（已 10 天未跟进）",
            "detail": "买家想定制\n客户：Mia（ID: 1）",
            "days_since": 10,
            "evidence": [
                {"kind": "message", "ref_id": "1", "quote": "Mia"}
            ],
        }
    ]
}

_STANDARD_SOURCE: dict = {
    "chain_id": "crm_reminder_chain",
    "engine_task_id": "e-000001",
    "worker_id": "crm_follow_up_reminder",
    "customer_id": 1,
    "reminder_date": "2026-09-01",
    "audit_ids": [],
}

_STANDARD_WHITELIST = {"1"}


def _ok_response() -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "id": 42})


def _conflict_response() -> httpx.Response:
    return httpx.Response(409, json={"ok": False, "detail": "已存在"})


def _error_response(status: int = 500) -> httpx.Response:
    return httpx.Response(status, json={"detail": "服务器内部错误"})


# ---------------------------------------------------------------------------
# 1. test_consumer_empty_reminders — 空 reminders 合法 no-op -> inserted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_empty_reminders() -> None:
    """空 reminders 数组 = 合法产出，不调 HTTP，直接 inserted。"""
    outcome = await consume_schedule_reminder(
        {"reminders": []},
        whitelist=_STANDARD_WHITELIST,
        biz_client=FakeBizClient(),
        source=_STANDARD_SOURCE,
    )
    assert outcome.status == "inserted"
    assert outcome.proposal_id is None


# ---------------------------------------------------------------------------
# 2. test_consumer_rejected_no_evidence — evidence 为空 -> rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_rejected_no_evidence() -> None:
    """提醒项 evidence 为空（无依据不出建议）-> rejected。"""
    deliverable: dict = {
        "reminders": [
            {
                "customer_id": 1,
                "title": "跟进客户：Mia",
                "detail": "买家想定制",
                "days_since": 10,
                "evidence": [],
            }
        ]
    }
    outcome = await consume_schedule_reminder(
        deliverable,
        whitelist=_STANDARD_WHITELIST,
        biz_client=FakeBizClient(),
        source=_STANDARD_SOURCE,
    )
    assert outcome.status == "rejected"
    assert "evidence 为空" in (outcome.reason or "")


# ---------------------------------------------------------------------------
# 3. test_consumer_rejected_whitelist_outside — ref_id 不在白名单 -> rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_rejected_whitelist_outside() -> None:
    """evidence ref_id 不在白名单 -> rejected（幻觉证据，决策 16③）。"""
    outcome = await consume_schedule_reminder(
        _STANDARD_DELIVERABLE,
        whitelist={"999"},  # 白名单不含 "1"
        biz_client=FakeBizClient(),
        source=_STANDARD_SOURCE,
    )
    assert outcome.status == "rejected"
    assert "幻觉证据" in (outcome.reason or "")


# ---------------------------------------------------------------------------
# 4. test_consumer_rejected_whitelist_none — whitelist 未注入 -> rejected (fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_rejected_whitelist_none() -> None:
    """whitelist 未注入 -> fail-closed 拒落。"""
    outcome = await consume_schedule_reminder(
        _STANDARD_DELIVERABLE,
        whitelist=None,
        biz_client=FakeBizClient(),
        source=_STANDARD_SOURCE,
    )
    assert outcome.status == "rejected"
    assert "whitelist 未注入" in (outcome.reason or "")


# ---------------------------------------------------------------------------
# 5. test_consumer_rejected_source_incomplete — source 缺 customer_id -> rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_rejected_source_incomplete() -> None:
    """source 缺 customer_id -> rejected。"""
    incomplete_source: dict = {
        "chain_id": "crm_reminder_chain",
        "engine_task_id": "e-000001",
        "worker_id": "crm_follow_up_reminder",
        "reminder_date": "2026-09-01",
        "audit_ids": [],
        # customer_id 故意缺失
    }
    outcome = await consume_schedule_reminder(
        _STANDARD_DELIVERABLE,
        whitelist=_STANDARD_WHITELIST,
        biz_client=FakeBizClient(),
        source=incomplete_source,
    )
    assert outcome.status == "rejected"
    assert "customer_id" in (outcome.reason or "")


# ---------------------------------------------------------------------------
# 6. test_consumer_inserted_ok — 所有 item 落库成功 -> inserted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_inserted_ok() -> None:
    """全部 item 落库成功 -> ConsumeOutcome("inserted")。"""
    fake = FakeBizClient(responses=[_ok_response()])
    outcome = await consume_schedule_reminder(
        _STANDARD_DELIVERABLE,
        whitelist=_STANDARD_WHITELIST,
        biz_client=fake,
        source=_STANDARD_SOURCE,
    )
    assert outcome.status == "inserted"
    assert outcome.proposal_id is None
    assert len(fake.calls) == 1
    path, payload = fake.calls[0]
    assert path == "/tm/schedule-tasks"
    assert payload["title"] == "跟进客户：Mia（已 10 天未跟进）"
    assert payload["domain"] == "crm"
    assert payload["source"]["customer_id"] == 1


# ---------------------------------------------------------------------------
# 7. test_consumer_409_skip — biz_client 返回 409 -> skipped_idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_409_skip() -> None:
    """biz_client 返回 409（防重跳过）-> skipped_idempotent。"""
    fake = FakeBizClient(responses=[_conflict_response()])
    outcome = await consume_schedule_reminder(
        _STANDARD_DELIVERABLE,
        whitelist=_STANDARD_WHITELIST,
        biz_client=fake,
        source=_STANDARD_SOURCE,
    )
    assert outcome.status == "skipped_idempotent"
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# 8. test_consumer_network_error_raises — biz_client 抛异常 -> RuntimeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_network_error_raises() -> None:
    """biz_client.post() 抛异常 -> RuntimeError（链 failed，宁失败不假成功）。"""

    class ExplodingClient:
        calls: list = []

        async def post(self, path, payload):  # noqa: ANN001
            self.calls.append((path, payload))
            raise ConnectionError("网络不通")

    client = ExplodingClient()
    with pytest.raises(RuntimeError, match="定时任务写接口调用失败"):
        await consume_schedule_reminder(
            _STANDARD_DELIVERABLE,
            whitelist=_STANDARD_WHITELIST,
            biz_client=client,
            source=_STANDARD_SOURCE,
        )


# ---------------------------------------------------------------------------
# 9. test_consumer_http_500_raises — biz_client 返回 500 -> RuntimeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_http_500_raises() -> None:
    """biz_client 返回 500 -> RuntimeError（链 failed，宁失败不假成功）。"""
    fake = FakeBizClient(responses=[_error_response(500)])
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await consume_schedule_reminder(
            _STANDARD_DELIVERABLE,
            whitelist=_STANDARD_WHITELIST,
            biz_client=fake,
            source=_STANDARD_SOURCE,
        )


# ---------------------------------------------------------------------------
# 10. test_consumer_contract_validation_error — deliverable 无效 -> rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_contract_validation_error() -> None:
    """deliverable 不符合 ReminderResult 契约 -> rejected。"""
    # 缺少 reminders 字段
    outcome = await consume_schedule_reminder(
        {"not_reminders": []},
        whitelist=_STANDARD_WHITELIST,
        biz_client=FakeBizClient(),
        source=_STANDARD_SOURCE,
    )
    assert outcome.status == "rejected"
    assert "ReminderResult" in (outcome.reason or "")
