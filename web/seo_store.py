"""web/seo_store.py：SEO 业务 DAO（web 侧独立 DAO，零 engine import，P3-2）。

照 tm_store/crm_store 模式：连接串读 .env 的 LIUQUAN_TM_DB_URL；
零 engine import，lint P3-2 执法，双向零代码耦合 R24）。

职责：
- SEO 关键词历史指标读取（供 SEO 三页展示）
- 关键词研究结果临时存储（内存，不落库，供页面展示）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from models.seo import KeywordMetric

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOTENV_PATH = _REPO_ROOT / ".env"
_BIZ_DB_ENV = "LIUQUAN_TM_DB_URL"


class SEOWebError(RuntimeError):
    """SEO 业务层错误（统一异常，web 路由层 catch）。"""


class SEOStore:
    """SEO 业务 DAO（web 侧独立，零 engine import，P3-2）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get_keyword_metrics(self, keyword: str) -> list[dict[str, Any]]:
        """获取关键词历史指标（白名单来源，供 SEO 健康检查页展示）。"""
        maker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with maker() as session:
            rows = (
                await session.execute(
                    select(KeywordMetric)
                    .where(KeywordMetric.keyword == keyword)
                    .order_by(KeywordMetric.metric_date.desc(), KeywordMetric.id.desc())
                    .limit(30)
                )
            ).scalars().all()
            return [
                {
                    "id": r.id,
                    "keyword": r.keyword,
                    "metric_date": r.metric_date.isoformat() if r.metric_date else None,
                    "product_num": r.product_num,
                    "avg_price_top": float(r.avg_price_top) if r.avg_price_top is not None else None,
                    "top_competitors": r.top_competitors or [],
                    "metrics": r.metrics,
                    "quota": r.quota,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

    async def list_keywords_with_metrics(self) -> list[dict[str, Any]]:
        """列出所有有指标数据的关键词（供 listing 体检页展示）。"""
        maker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with maker() as session:
            rows = (
                await session.execute(
                    select(
                        KeywordMetric.keyword,
                        text("COUNT(*) as metric_count"),
                        text("MAX(metric_date) as latest_date"),
                    )
                    .group_by(KeywordMetric.keyword)
                    .order_by(text("latest_date DESC"))
                    .limit(50)
                )
            ).all()
            return [
                {
                    "keyword": row[0],
                    "metric_count": row[1],
                    "latest_date": row[2].isoformat() if row[2] else None,
                }
                for row in rows
            ]


def create_seo_store_from_env() -> SEOStore:
    """从环境变量创建 SEOStore（生产用）。"""
    from web.tm_store import create_tm_engine

    engine = create_tm_engine()
    return SEOStore(engine)
