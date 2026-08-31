"""keyword_research 工序 ACT（详设-v0.5 §6.1；照广成 run.py 逻辑）。

纯代码工序（worker.yaml 的 reason: none，无 LLM 调用，零 token 成本）：
调 ehunt_api + ehunt_keyword 两跳聚合（经 ctx.connectors 按 id 引用，R11 精神）。

流程（照广成 skills/keyword-research/run.py）：
1. ehunt_api.search(keywords, page_size) -> 竞品画像（失败 -> 工序 failed）
2. ehunt_keyword.fetch_keyword_metrics(keywords) -> CDP 逐词指标（失败不阻塞，记 metrics_note）
3. 聚合：把 CDP 指标并进 keywords[词] 的 metrics + related_keywords；合并成功 source 升级

输入：KeywordResearchInput{keywords[], page_size?}
输出：KeywordData（含 source/quota/metrics_note）
"""

from __future__ import annotations

from typing import Any

from engine.core.context import EngineContext
from models.workers import KeywordData, KeywordResearchInput


def run(inputs: KeywordResearchInput, ctx: EngineContext) -> KeywordData:
    """关键词调研：eHunt API 竞品画像 + CDP 逐词指标聚合（照广成逻辑）。"""

    keywords = inputs.keywords
    page_size = inputs.page_size or int(ctx.config.get("default_page_size", 10))

    if not keywords:
        return KeywordData(
            source="ehunt-api",
            keywords={},
            quota=None,
            metrics_note="未提供关键词（keywords 为空）",
        )

    # 1. 调 ehunt_api connector（竞品画像）
    ehunt_api = ctx.connectors.get("ehunt_api") if ctx.connectors else None
    if ehunt_api is None:
        return KeywordData(
            source="ehunt-api",
            keywords={},
            quota=None,
            metrics_note="ehunt_api connector 未注入",
        )

    # 同步调用 connector.search（connector 内部处理 8 词硬顶 + page_size 上限）
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 在异步上下文中，用 run_in_executor 包装同步 connector
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = loop.run_in_executor(
                    pool,
                    lambda: _run_sync(ehunt_api.search, keywords, page_size=page_size),
                )
                # 等待结果
                import concurrent.futures

                future = concurrent.futures.Future()

                def callback(fut):
                    try:
                        future.set_result(fut.result())
                    except Exception as e:
                        future.set_exception(e)

                result.add_done_callback(callback)
                connector_result = future.result()
        else:
            connector_result = loop.run_until_complete(
                ehunt_api.search(keywords, page_size=page_size)
            )
    except Exception:
        # 直接尝试同步调用
        connector_result = _run_sync(ehunt_api.search, keywords, page_size=page_size)

    if not connector_result.ok:
        return KeywordData(
            source="ehunt-api",
            keywords={},
            quota=None,
            metrics_note=f"eHunt API 查询失败：{connector_result.note}",
        )

    # 解析 API 返回数据
    api_data = connector_result.data or {}
    keyword_data_dict = api_data.get("keywords", {})
    quota = api_data.get("quota")

    # 2. 调 ehunt_keyword connector（CDP 逐词指标）——失败不阻塞
    ehunt_keyword = ctx.connectors.get("ehunt_keyword") if ctx.connectors else None
    source = "ehunt-api"
    metrics_note = None

    if ehunt_keyword is not None:
        try:
            keyword_result = _run_sync(ehunt_keyword.fetch_keyword_metrics, keywords)
            if keyword_result.ok:
                # 聚合：把 CDP 指标并进 keywords[词] 的 metrics + related_keywords
                cdp_data = keyword_result.data or {}
                cdp_keywords = cdp_data.get("keywords", {})
                for keyword, entry in cdp_keywords.items():
                    if keyword in keyword_data_dict and isinstance(entry, dict):
                        # 剥掉 related，只留指标
                        metrics = {k: v for k, v in entry.items() if k != "related"}
                        keyword_data_dict[keyword]["metrics"] = metrics
                        if entry.get("related"):
                            keyword_data_dict[keyword]["related_keywords"] = entry["related"]
                source = "ehunt-api+ehunt-keyword-cdp"
            else:
                metrics_note = f"逐词指标不可用：{keyword_result.note}"
        except Exception as exc:
            metrics_note = f"逐词指标获取异常：{exc}"
    else:
        metrics_note = "ehunt_keyword connector 未注入，跳过逐词指标"

    return KeywordData(
        source=source,
        keywords=keyword_data_dict,
        quota=quota,
        metrics_note=metrics_note,
    )


def _run_sync(func, *args, **kwargs):
    """同步调用异步函数（兼容 connector 可能是同步/异步的情况）。"""
    import asyncio

    if asyncio.iscoroutinefunction(func):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在已有事件循环中，用线程池执行
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = loop.run_in_executor(pool, lambda: asyncio.run(func(*args, **kwargs)))
                    return future.result()
            else:
                return loop.run_until_complete(func(*args, **kwargs))
        except RuntimeError:
            return asyncio.run(func(*args, **kwargs))
    else:
        return func(*args, **kwargs)
