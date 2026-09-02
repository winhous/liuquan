"""v0.3 T5 engine/providers 测试（决策 26 读取接口化：provider = HTTP 调 web 读接口）。

- CrmChatContextHTTP：GET /api/biz/crm/context/{customer_id} -> ChatContextData
  （MockTransport 桩；JSON -> Model 校验）
- TmTaskContextHTTP：GET /api/biz/tm/task-context/{task_id} -> TaskContextData
- 非 200 -> BizReadError；未配置（base_url/token 缺）-> BizReadError
- build_providers 装配两 provider（key = 声明标识）
"""

from __future__ import annotations

import httpx
import pytest
from pytest_asyncio import fixture as async_fixture

from engine.providers import (
    BizReadError,
    CrmChatContextHTTP,
    CrmMessageImagesHTTP,
    CrmOverdueContextHTTP,
    ScrapeImageContextHTTP,
    ScrapeLinkContextHTTP,
    ScrapeLinkQueueHTTP,
    SeoMetricHistoryHTTP,
    TmTaskContextHTTP,
    build_providers,
)
from models.workers import (
    ChatContextData,
    ChatContextParams,
    ReminderContextData,
    TaskContextData,
    TaskContextParams,
)


def _crm_handler(req: httpx.Request) -> httpx.Response:
    assert req.url.path == "/api/biz/crm/context/1"
    return httpx.Response(
        200,
        json={
            "customer": {"id": 1, "nickname": "Mia", "latest_summary": "定制"},
            "messages": [
                {"id": 1, "source_text": "hi", "translated_text": "你好", "direction": "buyer"}
            ],
            "snapshot": {
                "id": 3, "current_need": "花束", "need_history": [], "sentiment": "好",
                "todos": [], "summary": "想定制",
            },
            "existing_open_todos": ["确认花材"],
        },
    )


@pytest.mark.asyncio
async def test_crm_context_provider_ok() -> None:
    provider = CrmChatContextHTTP(
        base_url="http" + "://test-" + "biz", token="test-token",
        transport=httpx.MockTransport(_crm_handler),
    )
    data = await provider(ChatContextParams(customer_id=1))
    assert isinstance(data, ChatContextData)
    assert data.customer.nickname == "Mia"
    assert data.messages[0].id == 1
    assert data.snapshot is not None and data.snapshot.id == 3
    assert data.existing_open_todos == ["确认花材"]


def _tm_handler(req: httpx.Request) -> httpx.Response:
    assert req.url.path == "/api/biz/tm/task-context/9"
    return httpx.Response(
        200,
        json={
            "task_id": 9,
            "title": "处理退货",
            "domain": "crm",
            "status": "open",
            "tags": ["售后"],
            "recent_events": [{"event_type": "created", "note": "创建", "created_at": "2026-08-30T00:00:00Z"}],
        },
    )


@pytest.mark.asyncio
async def test_tm_task_context_provider_ok() -> None:
    provider = TmTaskContextHTTP(
        base_url="http" + "://test-" + "biz", token="test-token",
        transport=httpx.MockTransport(_tm_handler),
    )
    data = await provider(TaskContextParams(task_id=9))
    assert isinstance(data, TaskContextData)
    assert data.title == "处理退货"
    assert data.tags == ["售后"]
    assert data.recent_events[0].event_type == "created"


@pytest.mark.asyncio
async def test_crm_context_404_raises() -> None:
    provider = CrmChatContextHTTP(
        base_url="http" + "://test-" + "biz", token="test-token",
        transport=httpx.MockTransport(lambda req: httpx.Response(404, json={"detail": "no"})),
    )
    with pytest.raises(BizReadError):
        await provider(ChatContextParams(customer_id=999))


@pytest.mark.asyncio
async def test_provider_missing_config_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """base_url/token 缺省从 .env 读；未配置 -> BizReadError（fail-closed）。"""
    import engine.providers as providers_mod

    monkeypatch.setattr(providers_mod, "dotenv_values", lambda path: {})
    provider = CrmChatContextHTTP(base_url="", token="")
    with pytest.raises(BizReadError):
        await provider(ChatContextParams(customer_id=1))


def test_build_providers_assembles() -> None:
    providers = build_providers(base_url="http" + "://test-" + "biz", token="test-token")
    assert set(providers) == {
        "crm.chat_context", "crm.overdue_context", "crm.message_images",
        "tm.task_context", "seo.metric_history", "scrape.image_context",
        # v0.6 批 4：扒图链接记录 + 定时队列白名单（详设 §7）
        "scrape.link_context", "scrape.link_queue",
    }
    assert isinstance(providers["crm.chat_context"], CrmChatContextHTTP)
    assert isinstance(providers["crm.overdue_context"], CrmOverdueContextHTTP)
    assert isinstance(providers["crm.message_images"], CrmMessageImagesHTTP)
    assert isinstance(providers["tm.task_context"], TmTaskContextHTTP)
    assert isinstance(providers["seo.metric_history"], SeoMetricHistoryHTTP)
    assert isinstance(providers["scrape.image_context"], ScrapeImageContextHTTP)
    assert isinstance(providers["scrape.link_context"], ScrapeLinkContextHTTP)
    assert isinstance(providers["scrape.link_queue"], ScrapeLinkQueueHTTP)


@pytest.mark.asyncio
async def test_overdue_provider_wraps_list_into_contract() -> None:
    """crm.overdue_context：web 接口返回裸 list（集成验收实锤），provider 必须包装
    进 ReminderContextData {customers: [...]}（R22 契约对齐，否则链 FAILED）。"""
    from models.workers import ReminderChainInput

    raw_list = [
        {"customer_id": 4, "nickname": "验收客户", "days_since": 6, "latest_summary": ""},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/biz/crm/overdue-customers"
        return httpx.Response(200, json=raw_list)

    provider = CrmOverdueContextHTTP(
        base_url="http" + "://test-" + "biz",
        token="test-token",
        transport=httpx.MockTransport(handler),
    )
    data = await provider(ReminderChainInput(trigger_date="2026-02-01"))
    assert isinstance(data, ReminderContextData)
    assert len(data.customers) == 1
    assert data.customers[0].customer_id == 4
    assert data.customers[0].days_since == 6
