"""seo_store DAO 单测（嵌入式 PG，零网络，规范 R12）。

覆盖：
- get_keyword_metrics 关键词历史指标查询
- list_keywords_with_metrics 列出有指标数据的关键词
- 空数据场景
"""

from __future__ import annotations

import pytest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from models.seo import KeywordMetric

REPO_ROOT = Path(__file__).resolve().parents[1]


@async_fixture
async def biz_engine(tm_pg_cluster):
    """业务库 AsyncEngine（NullPool：TestClient portal 循环与 pytest 循环不串）。"""
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean_seo_tables(biz_engine):
    """每测试后清 seo.keyword_metric 表，互不污染。"""
    yield
    async with AsyncSession(biz_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE seo.keyword_metric "
                "RESTART IDENTITY CASCADE"
            )
        )


async def _insert_metric(session: AsyncSession, keyword: str, metric_date: date, product_num: int = 100):
    """插入测试指标数据。"""
    row = KeywordMetric(
        keyword=keyword,
        metric_date=metric_date,
        product_num=product_num,
        avg_price_top=9.99,
        top_competitors=[],
        metrics={"frequency": 100},
        quota={"used_today": 1, "remaining_today": 199},
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_get_keyword_metrics_empty(biz_engine) -> None:
    """get_keyword_metrics 无数据返回空列表。"""
    from web.seo_store import SEOStore

    store = SEOStore(biz_engine)
    result = await store.get_keyword_metrics("nonexistent")
    assert result == []


@pytest.mark.asyncio
async def test_get_keyword_metrics_success(biz_engine) -> None:
    """get_keyword_metrics 正常查询。"""
    from web.seo_store import SEOStore

    # 插入测试数据
    async with AsyncSession(biz_engine) as session, session.begin():
        await _insert_metric(session, "test necklace", date(2026, 9, 1), 1000)
        await _insert_metric(session, "test necklace", date(2026, 9, 2), 1100)
        await _insert_metric(session, "other keyword", date(2026, 9, 1), 500)

    store = SEOStore(biz_engine)
    result = await store.get_keyword_metrics("test necklace")

    assert len(result) == 2
    # 按 metric_date 降序
    assert result[0]["metric_date"] == "2026-09-02"
    assert result[0]["product_num"] == 1100
    assert result[1]["metric_date"] == "2026-09-01"
    assert result[1]["product_num"] == 1000


@pytest.mark.asyncio
async def test_list_keywords_with_metrics_empty(biz_engine) -> None:
    """list_keywords_with_metrics 无数据返回空列表。"""
    from web.seo_store import SEOStore

    store = SEOStore(biz_engine)
    result = await store.list_keywords_with_metrics()
    assert result == []


@pytest.mark.asyncio
async def test_list_keywords_with_metrics_success(biz_engine) -> None:
    """list_keywords_with_metrics 正常列出。"""
    from web.seo_store import SEOStore

    # 插入测试数据
    async with AsyncSession(biz_engine) as session, session.begin():
        await _insert_metric(session, "test necklace", date(2026, 9, 1), 1000)
        await _insert_metric(session, "test necklace", date(2026, 9, 2), 1100)
        await _insert_metric(session, "other keyword", date(2026, 9, 1), 500)

    store = SEOStore(biz_engine)
    result = await store.list_keywords_with_metrics()

    assert len(result) == 2
    # 按 latest_date 降序
    keywords = [r["keyword"] for r in result]
    assert "test necklace" in keywords
    assert "other keyword" in keywords


@pytest.mark.asyncio
async def test_list_keywords_with_metrics_count(biz_engine) -> None:
    """list_keywords_with_metrics 指标计数正确。"""
    from web.seo_store import SEOStore

    # 插入测试数据
    async with AsyncSession(biz_engine) as session, session.begin():
        await _insert_metric(session, "test necklace", date(2026, 9, 1), 1000)
        await _insert_metric(session, "test necklace", date(2026, 9, 2), 1100)
        await _insert_metric(session, "test necklace", date(2026, 9, 3), 1200)

    store = SEOStore(biz_engine)
    result = await store.list_keywords_with_metrics()

    assert len(result) == 1
    assert result[0]["keyword"] == "test necklace"
    assert result[0]["metric_count"] == 3
