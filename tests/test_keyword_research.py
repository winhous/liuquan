"""keyword_research 工序单测（fake connector，零网络，规范 R12）。

覆盖：
- 工序纯代码（reason: none，无 LLM）
- fake connector 注入验证两跳聚合
- 8 词硬顶验证
- 降级场景（connector 缺失/API 失败/CDP 不可用）
"""

from __future__ import annotations

import pytest
from typing import Any
from unittest.mock import MagicMock

from engine.core.context import EngineContext
from engine.connectors import ConnectorResult
from models.workers import KeywordResearchInput, KeywordData


def _make_ctx(
    keywords: list[str] = None,
    connectors: dict = None,
) -> EngineContext:
    """构造测试用 EngineContext。"""
    inputs = KeywordResearchInput(keywords=keywords or ["test"])
    return EngineContext(
        worker_id="keyword_research",
        domain="seo",
        inputs=inputs,
        config={},
        context_data={},
        connectors=connectors,
    )


class FakeEHuntAPIConnector:
    """fake ehunt_api connector（零网络）。"""

    def __init__(self, ok: bool = True, data: dict = None, note: str = ""):
        self._ok = ok
        self._data = data or {}
        self._note = note

    @property
    def available(self) -> bool:
        return self._ok

    async def search(self, keywords: list[str], *, page_size: int = 10) -> ConnectorResult:
        if not self._ok:
            return ConnectorResult(ok=False, note=self._note)
        return ConnectorResult(ok=True, data=self._data)


class FakeEHuntKeywordConnector:
    """fake ehunt_keyword connector（零网络）。"""

    def __init__(self, ok: bool = True, data: dict = None, note: str = ""):
        self._ok = ok
        self._data = data or {}
        self._note = note

    @property
    def available(self) -> bool:
        return self._ok

    async def fetch_keyword_metrics(self, keywords: list[str]) -> ConnectorResult:
        if not self._ok:
            return ConnectorResult(ok=False, note=self._note)
        return ConnectorResult(ok=True, data=self._data)


def test_keyword_research_basic():
    """keyword_research 基本流程（两跳聚合）。"""
    from engine.registry.workers.seo.keyword_research.run import run

    # mock ehunt_api 返回
    api_data = {
        "keywords": {
            "test": {
                "keyword": "test",
                "product_num": 100,
                "avg_price_top": 9.99,
                "top_competitors": [],
            }
        },
        "quota": {"used_today": 1, "remaining_today": 199},
    }
    fake_api = FakeEHuntAPIConnector(ok=True, data=api_data)

    # mock ehunt_keyword 返回
    cdp_data = {
        "keywords": {
            "test": {
                "keyword": "test",
                "frequency": 100,
                "competition": 1.5,
                "views_month": 2000000,
                "related": [],
            }
        }
    }
    fake_keyword = FakeEHuntKeywordConnector(ok=True, data=cdp_data)

    connectors = {"ehunt_api": fake_api, "ehunt_keyword": fake_keyword}
    ctx = _make_ctx(keywords=["test"], connectors=connectors)
    inputs = ctx.inputs

    result = run(inputs, ctx)
    assert isinstance(result, KeywordData)
    assert result.source == "ehunt-api+ehunt-keyword-cdp"
    assert "test" in result.keywords
    assert result.keywords["test"]["product_num"] == 100
    assert result.keywords["test"]["metrics"]["frequency"] == 100
    assert result.quota["used_today"] == 1


def test_keyword_research_empty_keywords():
    """keyword_research 空关键词（Pydantic 校验拒绝空列表）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        KeywordResearchInput(keywords=[])


def test_keyword_research_no_connector():
    """keyword_research connector 缺失。"""
    from engine.registry.workers.seo.keyword_research.run import run

    inputs = KeywordResearchInput(keywords=["test"])
    ctx = EngineContext(
        worker_id="keyword_research",
        domain="seo",
        inputs=inputs,
        config={},
        context_data={},
        connectors=None,  # 无 connectors
    )

    result = run(inputs, ctx)
    assert isinstance(result, KeywordData)
    assert result.metrics_note is not None
    assert "未注入" in result.metrics_note


def test_keyword_research_api_failure():
    """keyword_research API 失败。"""
    from engine.registry.workers.seo.keyword_research.run import run

    fake_api = FakeEHuntAPIConnector(ok=False, note="API 请求失败")
    connectors = {"ehunt_api": fake_api}
    ctx = _make_ctx(keywords=["test"], connectors=connectors)

    result = run(ctx.inputs, ctx)
    assert isinstance(result, KeywordData)
    assert result.metrics_note is not None
    assert "失败" in result.metrics_note


def test_keyword_research_cdp_failure():
    """keyword_research CDP 失败（不阻塞主流程）。"""
    from engine.registry.workers.seo.keyword_research.run import run

    # API 成功
    api_data = {
        "keywords": {
            "test": {
                "keyword": "test",
                "product_num": 100,
                "avg_price_top": 9.99,
                "top_competitors": [],
            }
        },
        "quota": {"used_today": 1, "remaining_today": 199},
    }
    fake_api = FakeEHuntAPIConnector(ok=True, data=api_data)

    # CDP 失败
    fake_keyword = FakeEHuntKeywordConnector(ok=False, note="CDP 不可用")

    connectors = {"ehunt_api": fake_api, "ehunt_keyword": fake_keyword}
    ctx = _make_ctx(keywords=["test"], connectors=connectors)

    result = run(ctx.inputs, ctx)
    assert isinstance(result, KeywordData)
    assert result.source == "ehunt-api"  # 只有 API 数据
    assert result.metrics_note is not None
    assert "不可用" in result.metrics_note
    assert "test" in result.keywords  # API 数据仍在


def test_keyword_research_8_keyword_limit():
    """keyword_research 8 词硬顶（connector 层处理）。

    注：KeywordResearchInput 的 max_length=8 在 Pydantic 层拒绝 >8 的列表；
    8 词硬顶由 connector 层二次截断（防调用方绕过 model 校验）。
    """
    from engine.registry.workers.seo.keyword_research.run import run

    call_count = 0

    class CountingFakeAPI:
        def __init__(self):
            self.available = True

        async def search(self, keywords: list[str], *, page_size: int = 10) -> ConnectorResult:
            nonlocal call_count
            call_count += 1
            return ConnectorResult(
                ok=True,
                data={
                    "keywords": {k: {"product_num": 100} for k in keywords},
                    "quota": {"used_today": call_count, "remaining_today": 200 - call_count},
                },
            )

    connectors = {"ehunt_api": CountingFakeAPI()}
    # 传入恰好 8 个词（model 允许的上限）
    ctx = _make_ctx(keywords=[f"kw{i}" for i in range(8)], connectors=connectors)

    result = run(ctx.inputs, ctx)
    assert isinstance(result, KeywordData)
    assert call_count == 1  # 只调用一次 search，connector 内部处理 8 词硬顶
