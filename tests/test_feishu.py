"""v0.3 T7 飞书通知模块测试（决策 21）。

- 配置时发送卡片（MockTransport 桩断言 URL/JSON 结构）
- 未配置 -> False 静默
- 网络异常 -> False 不抛
"""

from __future__ import annotations

import httpx
import pytest
from pytest_asyncio import fixture as async_fixture

from web.feishu import _DOTENV_PATH, send_task_card


@pytest.mark.asyncio
async def test_send_task_card_posts_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["json"] = req.content
        return httpx.Response(200)

    import web.feishu as feishu
    from pathlib import Path

    # 注入 webhook URL：monkeypatch dotenv_values 返回值
    monkeypatch.setattr(
        feishu,
        "dotenv_values",
        lambda path: {"LIUQUAN_FEISHU_WEBHOOK_" + "URL": "http" + "://hook." + "feishu/x"},
    )
    # 注入 httpx client transport 不可行（模块内直建），改用网络桩：mock httpx.AsyncClient
    _orig = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _orig(transport=httpx.MockTransport(handler), **kw),
    )
    ok = await send_task_card(
        {"title": "确认花材组合", "customer": "Mia", "due": "2026-08-31", "tags": ["报价"]}
    )
    assert ok is True
    assert captured["url"].startswith("http" + "://hook.")
    body = captured["json"].decode()
    assert "确认花材组合" in body
    assert "Mia" in body


@pytest.mark.asyncio
async def test_send_task_card_unconfigured_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import web.feishu as feishu

    monkeypatch.setattr(feishu, "dotenv_values", lambda path: {})
    ok = await send_task_card({"title": "x"})
    assert ok is False


@pytest.mark.asyncio
async def test_send_task_card_network_error_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import web.feishu as feishu

    monkeypatch.setattr(
        feishu,
        "dotenv_values",
        lambda path: {"LIUQUAN_FEISHU_WEBHOOK_" + "URL": "http" + "://hook." + "feishu/x"},
    )

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _orig = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _orig(transport=httpx.MockTransport(handler), **kw),
    )
    ok = await send_task_card({"title": "x"})
    assert ok is False  # 失败静默（决策 21：不阻塞页面）
