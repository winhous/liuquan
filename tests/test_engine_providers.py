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
    TmTaskContextHTTP,
    build_providers,
)
from models.workers import ChatContextData, ChatContextParams, TaskContextData, TaskContextParams


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
async def test_provider_missing_config_raises() -> None:
    """base_url/token 缺省从 .env 读；测试环境未配置 -> BizReadError（fail-closed）。"""
    provider = CrmChatContextHTTP(base_url="", token="")
    with pytest.raises(BizReadError):
        await provider(ChatContextParams(customer_id=1))


def test_build_providers_assembles() -> None:
    providers = build_providers(base_url="http" + "://test-" + "biz", token="test-token")
    assert set(providers) == {"crm.chat_context", "tm.task_context"}
    assert isinstance(providers["crm.chat_context"], CrmChatContextHTTP)
    assert isinstance(providers["tm.task_context"], TmTaskContextHTTP)
