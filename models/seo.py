"""业务库 ORM（database: liuquan, schema: seo）—— keyword_metric 表。

纯数据层（详设-v0.5 §4.1：seo.keyword_metric 表，体检历史 + 变化检测数据源）。

复用 models/tm.py 的 TmBase（业务库 declarative base，R22）。
seo 表注册进 TmBase.metadata 后，migrations/business/env.py 的 target_metadata
自动涵盖两 schema（R22 同源，env.py 已 import 本模块）。

对齐详设 §4.1：
- keyword_metric：SEO 关键词历史指标（keyword × metric_date 唯一）
  - product_num：匹配商品总数（竞争度代理）
  - avg_price_top：top 竞品均价
  - top_competitors：top 竞品画像（JSONB 数组）
  - metrics：CDP 逐词指标（JSONB，可空）
  - quota：eHunt 配额回显（JSONB，可空）
  - created_at：创建时间
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.tm import TmBase


class KeywordMetric(TmBase):
    """seo.keyword_metric：SEO 关键词历史指标表（详设-v0.5 §4.1）。

    keyword × metric_date 唯一（同词同天只写一次，幂等 409 防重）。
    体检历史 + 变化检测数据源（listing_healthcheck 比较用）。
    """

    __tablename__ = "keyword_metric"
    __table_args__ = (
        UniqueConstraint("keyword", "metric_date", name="uq_keyword_metric_date"),
        Index("idx_seo_keyword_metric_kw", "keyword", "metric_date"),
        {"schema": "seo"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    keyword: Mapped[str] = mapped_column(Text, nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    product_num: Mapped[int | None] = mapped_column(BigInteger)  # 匹配商品总数（竞争度代理）
    avg_price_top: Mapped[float | None] = mapped_column(Numeric(10, 2))  # top 竞品均价
    top_competitors: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )  # top 竞品画像（JSONB 数组）
    metrics: Mapped[dict | None] = mapped_column(JSONB)  # CDP 逐词指标（可空）
    quota: Mapped[dict | None] = mapped_column(JSONB)  # eHunt 配额回显（可空）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
