"""listing_healthcheck 工序单测（fake connector + fake provider，零网络，规范 R12）。

覆盖：
- 工序纯代码（reason: none，无 LLM）
- 变化检测：首跑无基线/下降/上升/稳定/阈值边界
- 配置阈值生效
- connector 缺失降级
- provider 历史数据对比
"""

from __future__ import annotations

import pytest
from typing import Any
from unittest.mock import MagicMock

from engine.core.context import EngineContext
from engine.connectors import ConnectorResult
from models.workers import HealthcheckInput, HealthcheckResult, HealthcheckItem


class FakeEHuntAPIConnector:
    """fake ehunt_api connector（零网络）。"""

    def __init__(self, ok: bool = True, keywords: dict = None, quota: dict = None, note: str = ""):
        self._ok = ok
        self._keywords = keywords or {}
        self._quota = quota or {}
        self._note = note

    async def search(self, keywords: list[str], *, page_size: int = 10) -> ConnectorResult:
        if not self._ok:
            return ConnectorResult(ok=False, note=self._note)
        # 返回匹配的关键词数据
        matched = {k: v for k, v in self._keywords.items() if k in keywords}
        return ConnectorResult(
            ok=True,
            data={"keywords": matched, "quota": self._quota},
        )


def _make_ctx(
    keywords: list[str] | None = None,
    connector=None,
    context_data: dict = None,
    config: dict = None,
) -> EngineContext:
    """构造测试用 EngineContext。"""
    inputs = HealthcheckInput(keywords=keywords if keywords is not None else ["test_keyword"])
    connectors = {}
    if connector:
        connectors["ehunt_api"] = connector
    return EngineContext(
        worker_id="listing_healthcheck",
        domain="seo",
        inputs=inputs,
        config=config or {"delta_pct": 20},
        context_data=context_data or {},
        connectors=connectors if connectors else None,
    )


class TestListingHealthcheck:
    """listing_healthcheck 工序测试。"""

    def test_connector_missing(self):
        """connector 缺失时降级返回 note。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        ctx = _make_ctx(keywords=["kw1"])
        result = run(ctx.inputs, ctx)
        assert isinstance(result, HealthcheckResult)
        assert result.note is not None
        assert "connector 未注入" in result.note

    def test_empty_keywords(self):
        """空关键词列表返回 note。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        connector = FakeEHuntAPIConnector(ok=True)
        ctx = _make_ctx(keywords=[], connector=connector)
        result = run(ctx.inputs, ctx)
        assert isinstance(result, HealthcheckResult)
        assert result.note is not None
        assert "未提供关键词" in result.note

    def test_first_run_no_baseline(self):
        """首跑无基线：所有关键词归 stable。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        connector = FakeEHuntAPIConnector(
            ok=True,
            keywords={"kw1": {"product_num": 100, "avg_price_top": 25.0}},
            quota={"used_today": 1, "remaining_today": 199},
        )
        ctx = _make_ctx(keywords=["kw1"], connector=connector)
        result = run(ctx.inputs, ctx)
        assert isinstance(result, HealthcheckResult)
        assert len(result.changed) == 0
        assert len(result.improved) == 0
        assert len(result.stable) == 1
        assert result.stable[0].keyword == "kw1"
        assert result.stable[0].prev_product_num is None  # 无基线
        assert result.quota == {"used_today": 1, "remaining_today": 199}

    def test_degraded(self):
        """product_num 下降超阈值 -> changed。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        connector = FakeEHuntAPIConnector(
            ok=True,
            keywords={"kw1": {"product_num": 75, "avg_price_top": 25.0}},
        )
        # provider 返回历史数据（product_num=100）
        history = MagicMock()
        history.keywords = {"kw1": {"product_num": 100}}
        ctx = _make_ctx(
            keywords=["kw1"],
            connector=connector,
            context_data={"seo_metric_history": history},
        )
        result = run(ctx.inputs, ctx)
        assert len(result.changed) == 1
        assert result.changed[0].keyword == "kw1"
        assert result.changed[0].direction == "degraded"
        assert result.changed[0].prev_product_num == 100
        assert result.changed[0].product_num == 75
        assert result.changed[0].delta_pct == -25.0

    def test_improved(self):
        """product_num 上升超阈值 -> improved。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        connector = FakeEHuntAPIConnector(
            ok=True,
            keywords={"kw1": {"product_num": 150, "avg_price_top": 25.0}},
        )
        history = MagicMock()
        history.keywords = {"kw1": {"product_num": 100}}
        ctx = _make_ctx(
            keywords=["kw1"],
            connector=connector,
            context_data={"seo_metric_history": history},
        )
        result = run(ctx.inputs, ctx)
        assert len(result.improved) == 1
        assert result.improved[0].keyword == "kw1"
        assert result.improved[0].direction == "improved"
        assert result.improved[0].delta_pct == 50.0

    def test_stable_within_threshold(self):
        """product_num 变化在阈值内 -> stable。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        connector = FakeEHuntAPIConnector(
            ok=True,
            keywords={"kw1": {"product_num": 105, "avg_price_top": 25.0}},
        )
        history = MagicMock()
        history.keywords = {"kw1": {"product_num": 100}}
        ctx = _make_ctx(
            keywords=["kw1"],
            connector=connector,
            context_data={"seo_metric_history": history},
        )
        result = run(ctx.inputs, ctx)
        assert len(result.stable) == 1
        assert result.stable[0].direction == "stable"
        assert result.stable[0].delta_pct == 5.0

    def test_threshold_boundary(self):
        """恰好等于阈值 -> stable（不超阈值）。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        connector = FakeEHuntAPIConnector(
            ok=True,
            keywords={"kw1": {"product_num": 120, "avg_price_top": 25.0}},
        )
        history = MagicMock()
        history.keywords = {"kw1": {"product_num": 100}}
        ctx = _make_ctx(
            keywords=["kw1"],
            connector=connector,
            context_data={"seo_metric_history": history},
        )
        result = run(ctx.inputs, ctx)
        # 120-100=20/100=20%，恰好等于阈值 -> stable
        assert len(result.stable) == 1
        assert len(result.changed) == 0

    def test_threshold_just_over(self):
        """刚超阈值 -> changed。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        connector = FakeEHuntAPIConnector(
            ok=True,
            keywords={"kw1": {"product_num": 79, "avg_price_top": 25.0}},
        )
        history = MagicMock()
        history.keywords = {"kw1": {"product_num": 100}}
        ctx = _make_ctx(
            keywords=["kw1"],
            connector=connector,
            context_data={"seo_metric_history": history},
        )
        result = run(ctx.inputs, ctx)
        # 79-100=-21/100=-21%，超阈值 -> changed
        assert len(result.changed) == 1

    def test_custom_config_threshold(self):
        """自定义配置阈值生效。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        connector = FakeEHuntAPIConnector(
            ok=True,
            keywords={"kw1": {"product_num": 85, "avg_price_top": 25.0}},
        )
        history = MagicMock()
        history.keywords = {"kw1": {"product_num": 100}}
        ctx = _make_ctx(
            keywords=["kw1"],
            connector=connector,
            context_data={"seo_metric_history": history},
            config={"delta_pct": 10},  # 阈值降为 10%
        )
        result = run(ctx.inputs, ctx)
        # 85-100=-15/100=-15%，超 10% 阈值 -> changed
        assert len(result.changed) == 1

    def test_multiple_keywords_mixed(self):
        """多个关键词混合变化（下降+上升+稳定）。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        connector = FakeEHuntAPIConnector(
            ok=True,
            keywords={
                "kw_down": {"product_num": 50},
                "kw_up": {"product_num": 200},
                "kw_stable": {"product_num": 105},
            },
        )
        history = MagicMock()
        history.keywords = {
            "kw_down": {"product_num": 100},
            "kw_up": {"product_num": 100},
            "kw_stable": {"product_num": 100},
        }
        ctx = _make_ctx(
            keywords=["kw_down", "kw_up", "kw_stable"],
            connector=connector,
            context_data={"seo_metric_history": history},
        )
        result = run(ctx.inputs, ctx)
        assert len(result.changed) == 1
        assert result.changed[0].keyword == "kw_down"
        assert len(result.improved) == 1
        assert result.improved[0].keyword == "kw_up"
        assert len(result.stable) == 1
        assert result.stable[0].keyword == "kw_stable"

    def test_api_failure(self):
        """eHunt API 调用失败返回 note。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        connector = FakeEHuntAPIConnector(
            ok=False,
            note="配额耗尽",
        )
        ctx = _make_ctx(keywords=["kw1"], connector=connector)
        result = run(ctx.inputs, ctx)
        assert result.note is not None
        assert "配额耗尽" in result.note

    def test_metrics_snapshot(self):
        """指标快照包含在结果中。"""
        from engine.registry.workers.seo.listing_healthcheck.run import run

        connector = FakeEHuntAPIConnector(
            ok=True,
            keywords={"kw1": {"product_num": 100, "avg_price_top": 25.0}},
            quota={"used_today": 1, "remaining_today": 199},
        )
        ctx = _make_ctx(keywords=["kw1"], connector=connector)
        result = run(ctx.inputs, ctx)
        assert "kw1" in result.metrics
        assert result.metrics["kw1"]["product_num"] == 100
        assert result.metrics["kw1"]["avg_price_top"] == 25.0


# ==== HealthcheckItem Model 测试 ====


class TestHealthcheckItemModel:
    """HealthcheckItem Model 字段验证。"""

    def test_basic_fields(self):
        item = HealthcheckItem(keyword="test", product_num=100, direction="stable")
        assert item.keyword == "test"
        assert item.product_num == 100
        assert item.direction == "stable"
        assert item.prev_product_num is None
        assert item.delta_pct is None

    def test_full_fields(self):
        item = HealthcheckItem(
            keyword="test",
            product_num=80,
            prev_product_num=100,
            delta_pct=-20.0,
            direction="degraded",
        )
        assert item.delta_pct == -20.0
        assert item.direction == "degraded"
