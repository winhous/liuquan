"""listing_healthcheck 工序 ACT（详设-v0.5 §6.1；批 3 修订：纯代码两步）。

纯代码工序（worker.yaml 的 reason: none，无 LLM 调用，零 token 成本）：
1. 经 ctx.connectors["ehunt_api"] 拉各关键词当前指标
2. 经 provider seo.metric_history 读历史（缺失 = 首次无基线）
3. 代码规则变化检测（config 阈值）
4. 输出 HealthcheckResult

变化检测规则（config/settings.yaml 阈值）：
- product_num 下降超阈值 -> changed（degraded）
- product_num 上升超阈值 -> improved
- 其余 -> stable
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from engine.core.context import EngineContext
from models.workers import HealthcheckInput, HealthcheckItem, HealthcheckResult

logger = logging.getLogger(__name__)


def run(inputs: HealthcheckInput, ctx: EngineContext) -> HealthcheckResult:
    """listing 体检：拉取当前指标 + 变化检测（纯代码，零 token）。"""

    # 获取关键词列表
    keywords = inputs.keywords
    if not keywords:
        return HealthcheckResult(note="未提供关键词（keywords 为空）")

    # 获取配置阈值
    delta_pct_threshold = float(ctx.config.get("delta_pct", 20))

    # 体检日期
    check_date = inputs.date or date.today().isoformat()

    # 1. 拉取各关键词当前指标（经 ehunt_api connector）
    ehunt_api = ctx.connectors.get("ehunt_api") if ctx.connectors else None
    if ehunt_api is None:
        return HealthcheckResult(note="ehunt_api connector 未注入，无法拉取当前指标")

    # 调用 connector 获取当前指标
    current_metrics: dict[str, dict[str, Any]] = {}
    quota: dict[str, Any] | None = None
    try:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = concurrent.futures.Future()
                    def _call():
                        try:
                            r = asyncio.run(ehunt_api.search(keywords, page_size=10))
                            future.set_result(r)
                        except Exception as e:
                            future.set_exception(e)
                    pool.submit(_call)
                    api_result = future.result()
            else:
                api_result = loop.run_until_complete(ehunt_api.search(keywords, page_size=10))
        except RuntimeError:
            api_result = asyncio.run(ehunt_api.search(keywords, page_size=10))
    except Exception as exc:
        logger.warning("listing_healthcheck: ehunt_api 调用失败: %s", exc)
        return HealthcheckResult(note=f"eHunt API 调用失败：{exc}")

    if not api_result.ok:
        return HealthcheckResult(note=f"eHunt API 查询失败：{api_result.note}")

    api_data = api_result.data or {}
    keyword_entries = api_data.get("keywords", {})
    quota = api_data.get("quota")

    # 组装当前指标快照
    for kw, entry in keyword_entries.items():
        if isinstance(entry, dict):
            current_metrics[kw] = {
                "product_num": entry.get("product_num"),
                "avg_price_top": entry.get("avg_price_top"),
                "quota": quota,
            }

    # 2. 读取历史指标（经 provider seo.metric_history）
    #    provider 返回 keyword -> 历史指标列表
    history_data: dict[str, Any] = {}
    context_data = ctx.context_data or {}
    metric_history = context_data.get("seo_metric_history")
    if metric_history is not None:
        # provider 返回 KeywordData 格式
        if hasattr(metric_history, "keywords"):
            history_data = metric_history.keywords or {}
        elif isinstance(metric_history, dict):
            history_data = metric_history.get("keywords", {})

    # 3. 代码规则变化检测
    changed: list[HealthcheckItem] = []
    improved: list[HealthcheckItem] = []
    stable: list[HealthcheckItem] = []

    for kw in keywords:
        current = current_metrics.get(kw, {})
        product_num = current.get("product_num")
        prev_entry = history_data.get(kw, {})
        prev_product_num = prev_entry.get("product_num") if isinstance(prev_entry, dict) else None

        item = HealthcheckItem(
            keyword=kw,
            product_num=product_num,
            prev_product_num=prev_product_num,
        )

        if product_num is None:
            # 无法比较，归为 stable
            item.direction = "stable"
            stable.append(item)
            continue

        if prev_product_num is None or prev_product_num == 0:
            # 首次无基线，无法比较
            item.direction = "stable"
            stable.append(item)
            continue

        # 计算变化百分比
        delta = product_num - prev_product_num
        pct = (delta / prev_product_num) * 100 if prev_product_num else 0
        item.delta_pct = round(pct, 1)

        if pct < -delta_pct_threshold:
            # 下降超阈值 -> changed（degraded）
            item.direction = "degraded"
            changed.append(item)
        elif pct > delta_pct_threshold:
            # 上升超阈值 -> improved
            item.direction = "improved"
            improved.append(item)
        else:
            # 稳定
            item.direction = "stable"
            stable.append(item)

    return HealthcheckResult(
        changed=changed,
        improved=improved,
        stable=stable,
        metrics=current_metrics,
        quota=quota,
        note=None if current_metrics else "未获取到任何指标数据",
    )
