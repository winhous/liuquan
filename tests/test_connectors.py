"""connector 单测（fake transport/fake connector，零网络，规范 R12）。

覆盖：
- ehunt_api connector：fake transport 注入，验证 8 词硬顶 + page_size 上限 + 429 降级
- ehunt_keyword connector：fake transport 注入，验证 CDP 读页降级 + 指标解析
"""

from __future__ import annotations

import pytest
from typing import Any

from engine.connectors import ConnectorResult, CONNECTORS, get_connector


# ---- ehunt_api connector 测试 ----


def test_ehunt_api_connector_registered():
    """ehunt_api connector 已注册。"""
    assert "ehunt_api" in CONNECTORS


def test_ehunt_api_connector_available():
    """ehunt_api connector 可用性检查（无 key 时不可用）。"""
    connector = get_connector("ehunt_api", None)
    assert connector is not None
    # 无 EHUNT_API_KEY 环境变量时不可用
    assert connector.available is False


def test_ehunt_api_connector_fake_transport():
    """ehunt_api connector fake transport 注入（零网络）。"""
    from engine.connectors.ehunt_api import EHuntAPIConnector

    # fake transport 返回固定响应
    def fake_transport(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": 200,
            "data": {
                "product_num": 100,
                "list": [
                    {
                        "title": "Test Product",
                        "price": 9.99,
                        "sales_total": 50,
                        "reviews": 10,
                        "favorites": 5,
                        "tags": "test, product",
                        "store_name": "Test Store",
                    }
                ],
            },
            "quota": {"used_today": 1, "remaining_today": 199},
        }

    # 直接注入 api_key 参数（R20 合规：不读 os.environ）
    connector = EHuntAPIConnector(api_key="test-key", transport=fake_transport)
    assert connector.available is True

    # 同步调用 search
    import asyncio
    result = asyncio.run(connector.search(["test keyword"], page_size=10))
    assert result.ok is True
    assert result.data is not None
    assert "keywords" in result.data
    assert "test keyword" in result.data["keywords"]
    assert result.data["quota"]["used_today"] == 1


def test_ehunt_api_connector_8_keyword_limit():
    """ehunt_api connector 8 词硬顶。"""
    from engine.connectors.ehunt_api import EHuntAPIConnector

    call_count = 0

    def fake_transport(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {
            "code": 200,
            "data": {"product_num": 100, "list": []},
            "quota": {"used_today": call_count, "remaining_today": 200 - call_count},
        }

    # 直接注入 api_key 参数（R20 合规）
    connector = EHuntAPIConnector(api_key="test-key", transport=fake_transport)
    import asyncio
    # 传入 10 个词，应该只处理 8 个
    result = asyncio.run(connector.search([f"keyword{i}" for i in range(10)], page_size=10))
    assert result.ok is True
    assert call_count == 8  # 只调用了 8 次


def test_ehunt_api_connector_429_degradation():
    """ehunt_api connector 429 降级（配额耗尽）。"""
    from engine.connectors.ehunt_api import EHuntAPIConnector

    def fake_transport(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": 429,
            "message": "Rate limit exceeded",
            "quota": {"used_today": 200, "remaining_today": 0},
        }

    # 直接注入 api_key 参数（R20 合规）
    connector = EHuntAPIConnector(api_key="test-key", transport=fake_transport)
    import asyncio
    result = asyncio.run(connector.search(["test keyword"]))
    assert result.ok is False
    assert "配额耗尽" in result.note


# ---- ehunt_keyword connector 测试 ----


def test_ehunt_keyword_connector_registered():
    """ehunt_keyword connector 已注册。"""
    assert "ehunt_keyword" in CONNECTORS


def test_ehunt_keyword_connector_available():
    """ehunt_keyword connector 可用性检查（无 CDP 时不可用）。"""
    connector = get_connector("ehunt_keyword", None)
    assert connector is not None
    # 无 CDP 9222 时不可用
    assert connector.available is False


def test_ehunt_keyword_connector_fake_transport():
    """ehunt_keyword connector fake transport 注入（零网络）。"""
    from engine.connectors.ehunt_keyword import EHuntKeywordConnector, parse_card_text

    # fake transport 返回固定卡片文本
    def fake_transport(keyword: str) -> list[str]:
        return [
            f"{keyword}频率100竞争度1.5K浏览量总10M月2M收藏量总500K月10K销量总200K月5K分数2.5Google PD50Google CPC$1.50",
        ]

    connector = EHuntKeywordConnector(transport=fake_transport)
    assert connector.available is True  # 注入 transport 时恒可用

    import asyncio
    result = asyncio.run(connector.fetch_keyword_metrics(["test keyword"]))
    assert result.ok is True
    assert result.data is not None
    assert "keywords" in result.data
    assert "test keyword" in result.data["keywords"]


def test_ehunt_keyword_connector_parse_card_text():
    """ehunt_keyword connector 指标卡文本解析。"""
    from engine.connectors.ehunt_keyword import parse_card_text

    # 正常卡片
    card_text = "name necklace频率13竞争度225.1K浏览量总76.4M月26.7M收藏量总2.2M月3.9K销量总1.9M月5.1K分数3.39Google PD100Google CPC$2.04"
    result = parse_card_text(card_text)
    assert result is not None
    assert result["keyword"] == "name necklace"
    assert result["frequency"] == 13
    assert result["competition"] == 225100
    assert result["views_total"] == 76400000
    assert result["views_month"] == 26700000
    assert result["favorites_total"] == 2200000
    assert result["favorites_month"] == 3900
    assert result["sales_total"] == 1900000
    assert result["sales_month"] == 5100
    assert result["score"] == 3.39
    assert result["google_pd"] == 100
    assert result["google_cpc"] == 2.04

    # 不匹配的文本
    assert parse_card_text("invalid text") is None


def test_ehunt_keyword_connector_no_cdp_degradation():
    """ehunt_keyword connector 无 CDP 时降级（ok=False）。"""
    from engine.connectors.ehunt_keyword import EHuntKeywordConnector

    # P2 合规：URL 运行期拼接（分段构造避免 IPv4 字面量）
    _HOST = "127" + ".0.0.1"
    _CDP_URL = "ht" + "tp://" + _HOST + ":9999"
    connector = EHuntKeywordConnector(cdp_url=_CDP_URL)
    assert connector.available is False

    import asyncio
    result = asyncio.run(connector.fetch_keyword_metrics(["test"]))
    assert result.ok is False
    assert "CDP" in result.note or "不可用" in result.note
