"""link_record_create 工序 ACT（详设-v0.6 §5.2，新工序）。

纯代码工序（reason: none，无 LLM 调用，零 token 成本）：
建链接记录（normalized_url 幂等：已存在返回现有行，不重复建、不重复下载）。

- 立即扒（from_queue=false，urls 非空）：逐个经 ctx.biz_client POST
  /api/biz/scrape/links（web 批 2 已实现：created/existing 标记）→ 建 pending 记录
- 定时（from_queue=true，urls 空）：经 provider scrape.link_queue（context 声明）
  读定时队列 → 同上建记录
- 队列为空 → link_ids 空（链快速完成，供下一步 batch_image_download 空转跳过）

输入：LinkRecordCreateInput{urls[], batch_id, from_queue}
输出：LinkCreateResult{link_ids[], urls[], from_queue, batch_id, created_count, skipped_count}

降级：biz_client 未注入（CLI/测试 None）→ 返回空 link_ids + logger 警告
（照现有 hasattr/缺 key 模式，链快速完成不阻断）。
"""

from __future__ import annotations

import logging

from engine.core.context import EngineContext
from models.workers import LinkCreateResult, LinkRecordCreateInput

logger = logging.getLogger(__name__)


def _queue_urls(ctx: EngineContext) -> list[str]:
    """经 provider scrape_link_queue 读定时队列（context_data 为 LinkQueueData 或 dict）。"""
    data = (ctx.context_data or {}).get("scrape_link_queue")
    if data is None:
        return []
    urls = getattr(data, "urls", None)
    if isinstance(urls, list):
        return [str(u) for u in urls]
    if isinstance(data, dict):
        urls = data.get("urls") or []
        return [str(u) for u in urls]
    return []


async def run(inputs: LinkRecordCreateInput, ctx: EngineContext) -> LinkCreateResult:
    """建链接记录（幂等）；from_queue 时经 provider 读队列；队列空快速完成。"""
    urls = [str(u).strip() for u in (inputs.urls or []) if str(u).strip()]
    from_queue = bool(inputs.from_queue)

    if from_queue and not urls:
        urls = _queue_urls(ctx)
        if not urls:
            logger.info(
                "link_record_create: 定时队列为空（batch_id=%s），链快速完成",
                inputs.batch_id,
            )
            return LinkCreateResult(
                link_ids=[],
                urls=[],
                from_queue=from_queue,
                batch_id=inputs.batch_id,
            )

    if not urls:
        return LinkCreateResult(
            link_ids=[],
            urls=[],
            from_queue=from_queue,
            batch_id=inputs.batch_id,
        )

    biz_client = ctx.biz_client
    if biz_client is None:
        logger.warning(
            "link_record_create: biz_client 未注入（CLI/测试），跳过建记录（urls=%d）",
            len(urls),
        )
        return LinkCreateResult(
            link_ids=[],
            urls=[],
            from_queue=from_queue,
            batch_id=inputs.batch_id,
        )

    # 决策 26：写经写接口客户端（引擎零业务库连接串；normalized_url 幂等在 web 接口层）
    try:
        resp = await biz_client.post(
            "/scrape/links",
            {"urls": urls, "batch_id": inputs.batch_id},
        )
    except Exception as exc:  # noqa: BLE001  # BizApiError 等网络/配置异常
        logger.warning("link_record_create: POST /scrape/links 失败：%s", exc)
        return LinkCreateResult(
            link_ids=[],
            urls=urls,
            from_queue=from_queue,
            batch_id=inputs.batch_id,
        )
    if resp.status_code != 200:
        logger.warning(
            "link_record_create: POST /scrape/links HTTP %s", resp.status_code
        )
        return LinkCreateResult(
            link_ids=[],
            urls=urls,
            from_queue=from_queue,
            batch_id=inputs.batch_id,
        )
    body = resp.json()
    links = body.get("links") or []
    link_ids = [int(l["id"]) for l in links if l.get("id") is not None]
    created = int(body.get("created_count") or 0)
    existing = int(body.get("existing_count") or 0)
    return LinkCreateResult(
        link_ids=link_ids,
        urls=[str(l.get("url", "")) for l in links] or urls,
        from_queue=from_queue,
        batch_id=inputs.batch_id,
        created_count=created,
        skipped_count=existing,
    )
