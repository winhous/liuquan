"""seo_healthcheck_proposal 消费者（详设-v0.5 §6.4；批 3 新增）。

seo_healthcheck_chain 完成 → 消费者 seo.healthcheck（action_id=seo.healthcheck）：
HealthcheckResult → ①POST /api/biz/seo/metrics 落当前指标 ②changed 非空时模板生成
TaskProposal → POST /api/biz/tm/proposals。

职责：
1. 契约归一（dict -> HealthcheckResult，R2）；
2. 逐关键词 POST /api/biz/seo/metrics 落当前指标（幂等 409 忽略）；
3. changed 非空时，每变化关键词生成一条 TaskProposal：
   title「SEO 体检：关键词 X 排名下滑 Y%」，detail 附 prev/current + 建议优化方向，
   evidence ref_id=keyword；
4. changed 为空不落提案（skipped 语义）；
5. 复用 tm_proposal 转交器核心逻辑（禁幻觉三件套 + risk 标注 + HTTP 写接口）；
6. 不 import web 任何代码（P3-2）。

注入式设计（R12）：registry / audit_lookup / whitelist / biz_client 全部可注入。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from datetime import date

from pydantic import ValidationError

from engine.actions.biz_client import BizApiClient
from engine.actions.tm_proposal import ConsumeOutcome, consume_task_proposal
from models.contract.task import TaskProposal
from models.workers import HealthcheckResult

logger = logging.getLogger(__name__)


async def consume_seo_healthcheck(
    deliverable: dict,
    *,
    registry: object | None = None,
    audit_lookup: Callable[[list[str]], bool] | None = None,
    whitelist: Collection[str] | None = None,
    biz_client: BizApiClient | None = None,
    **_kwargs,
) -> ConsumeOutcome:
    """SEO 体检消费者：落指标 + changed 非空时模板转提案。"""
    # ---- 0. 契约归一 ----
    try:
        result = HealthcheckResult.model_validate(deliverable)
    except ValidationError as exc:
        return ConsumeOutcome(
            "rejected", reason=f"体检结果未过 HealthcheckResult 契约校验：{exc}"
        )

    client = biz_client if biz_client is not None else BizApiClient()
    today = date.today().isoformat()

    # ---- 1. POST /api/biz/seo/metrics 落当前指标 ----
    for kw, entry in result.metrics.items():
        if not isinstance(entry, dict):
            continue
        payload = {
            "keyword": kw,
            "metric_date": today,
            "product_num": entry.get("product_num"),
            "avg_price_top": entry.get("avg_price_top"),
            "top_competitors": [],
            "metrics": None,
            "quota": result.quota,
        }
        try:
            resp = await client.post("/seo/metrics", payload)
            if resp.status_code == 409:
                logger.info("seo_healthcheck: 关键词 %s 当天已有指标记录，跳过", kw)
            elif resp.status_code != 200:
                detail = ""
                try:
                    detail = str(resp.json().get("detail", ""))
                except Exception:
                    detail = resp.text[:200]
                logger.warning(
                    "seo_healthcheck: 关键词 %s 指标写入失败 HTTP %d: %s",
                    kw, resp.status_code, detail,
                )
        except Exception as exc:
            logger.warning("seo_healthcheck: 关键词 %s 指标写入异常: %s", kw, exc)

    # ---- 2. changed 非空时模板生成提案 ----
    if not result.changed:
        return ConsumeOutcome("inserted", proposal_id=None)

    inserted_count = 0
    skipped_count = 0

    for item in result.changed:
        keyword = item.keyword if hasattr(item, "keyword") else (item.get("keyword", "") if isinstance(item, dict) else "")
        delta_pct = item.delta_pct if hasattr(item, "delta_pct") else (item.get("delta_pct", 0) if isinstance(item, dict) else 0)
        prev_num = item.prev_product_num if hasattr(item, "prev_product_num") else (item.get("prev_product_num") if isinstance(item, dict) else None)
        curr_num = item.product_num if hasattr(item, "product_num") else (item.get("product_num") if isinstance(item, dict) else None)

        title = f"SEO 体检：关键词 {keyword} 排名下滑 {abs(delta_pct or 0):.0f}%"
        detail = (
            f"关键词「{keyword}」竞争度变化："
            f"商品数从 {prev_num} 降至 {curr_num}（变化 {delta_pct:.1f}%）。"
            f"建议优化对应 listing 标题、标签方向以提升排名。"
        )
        proposal = TaskProposal(
            action_id="seo.healthcheck",
            title=title,
            detail=detail,
            domain="seo",
            suggested_role="运营",
            suggested_due_days=3,
            evidence=[{"kind": "listing", "ref_id": keyword, "quote": keyword}],
            source={
                "chain_id": "",
                "engine_task_id": "",
                "worker_id": "listing_healthcheck",
                "audit_ids": [],
            },
        )
        outcome = await consume_task_proposal(
            proposal,
            registry=registry,
            audit_lookup=audit_lookup,
            whitelist=whitelist,
            biz_client=biz_client,
        )
        if outcome.status == "inserted":
            inserted_count += 1
        elif outcome.status.startswith("skipped"):
            skipped_count += 1
        else:
            logger.warning(
                "seo_healthcheck: 关键词 %s 提案落库失败: %s",
                keyword, outcome.reason,
            )

    if inserted_count > 0:
        return ConsumeOutcome("inserted", proposal_id=None)
    if skipped_count > 0:
        return ConsumeOutcome(
            "skipped_idempotent",
            reason=f"全部 {skipped_count} 条体检提案被跳过",
        )
    return ConsumeOutcome("inserted", proposal_id=None)


__all__ = ["consume_seo_healthcheck"]
