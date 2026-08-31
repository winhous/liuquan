"""seo_report 消费者（详设-v0.5 §6.4；批 3 新增）。

seo_keyword_chain 完成 → 消费者 seo.report（action_id=seo.report）：
KeywordData → POST /api/biz/seo/metrics 落库（幂等 unique(keyword,metric_date)，
409 忽略/标记 skipped）。

职责：
1. 契约归一（dict -> KeywordData，R2）；
2. 遍历 keywords 逐词 POST /api/biz/seo/metrics 落 keyword_metric 表；
3. 409 = 幂等跳过（记日志不影响其他词）；
4. 网络异常/500 -> 抛错（链 failed，宁失败不假成功）。

注入式设计（R12）：biz_client 可注入；缺省惰性建。
"""

from __future__ import annotations

import logging
from collections.abc import Collection

from pydantic import ValidationError

from engine.actions.biz_client import BizApiClient
from engine.actions.tm_proposal import ConsumeOutcome
from models.workers import KeywordData

logger = logging.getLogger(__name__)


async def consume_seo_report(
    deliverable: dict,
    *,
    whitelist: Collection[str] | None = None,
    biz_client: BizApiClient | None = None,
    **_kwargs,
) -> ConsumeOutcome:
    """SEO 关键词报告消费者：逐词 POST /api/biz/seo/metrics 落 keyword_metric 表。"""
    # ---- 0. 契约归一 ----
    try:
        result = KeywordData.model_validate(deliverable)
    except ValidationError as exc:
        return ConsumeOutcome(
            "rejected", reason=f"关键词数据未过 KeywordData 契约校验：{exc}"
        )

    if not result.keywords:
        return ConsumeOutcome("inserted", proposal_id=None)  # 空关键词 = 合法

    # ---- 1. 逐词 HTTP 写接口落库 ----
    client = biz_client if biz_client is not None else BizApiClient()
    skipped_count = 0
    inserted_count = 0
    from datetime import date

    today = date.today().isoformat()

    for keyword, entry in result.keywords.items():
        if not isinstance(entry, dict):
            continue
        payload = {
            "keyword": keyword,
            "metric_date": today,
            "product_num": entry.get("product_num"),
            "avg_price_top": entry.get("avg_price_top"),
            "top_competitors": entry.get("top_competitors", []),
            "metrics": entry.get("metrics"),
            "quota": result.quota,
        }
        try:
            resp = await client.post("/seo/metrics", payload)
        except Exception as exc:
            raise RuntimeError(
                f"SEO 指标写接口调用失败（关键词 {keyword}）：{exc}"
            ) from exc

        if resp.status_code == 409:
            logger.info(
                "seo_report: 关键词 %s 当天已有指标记录，跳过",
                keyword,
            )
            skipped_count += 1
        elif resp.status_code != 200:
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))
            except Exception:
                detail = resp.text[:200]
            raise RuntimeError(
                f"SEO 指标写接口 HTTP {resp.status_code}（关键词 {keyword}）：{detail}"
            )
        else:
            inserted_count += 1

    total = len(result.keywords)
    if inserted_count == total:
        return ConsumeOutcome("inserted", proposal_id=None)
    if inserted_count == 0:
        return ConsumeOutcome(
            "skipped_idempotent",
            reason=f"全部 {total} 条关键词指标被幂等跳过",
        )
    return ConsumeOutcome("inserted", proposal_id=None)


__all__ = ["consume_seo_report"]
