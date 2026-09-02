"""scrape_download_done 消费者（详设-v0.6 §5.3 + §15.2/§15.6 批 7，链完成回调）。

scrape_download_chain 完成 → 消费者 scrape.download_done：
deliverable = ScrapeBatchResult（链末输出） + kwargs 注入 task（含 task.input）：
- task.input.upload_netdisk=true（批 7）→ 对 status=done 且 netdisk_status≠
  uploaded 的链接逐条 upload_link_folder（engine/netdisk.py 同一执行点：
  quark connector 上传 + PATCH netdisk 三列回填；失败 PATCH failed +
  error_note 页面可见，不阻断下载链本身）
- task.input.from_queue=true → POST /api/biz/scrape/queue/clear（清定时队列，
  详设 §5.4：定时跑全部链接建记录+处理成功完成后清空队列）
- from_queue=false → 只记审计 note（立即扒不清队列）

职责：
1. 契约归一（dict -> ScrapeBatchResult，R2）；
2. upload_netdisk 判断（优先 kwargs.task.input.upload_netdisk——批 7 web/调度器
   注入；deliverable 无此字段）；from_queue 判断（优先 kwargs.task.input.
   from_queue——调度器注入；兜底 deliverable.from_queue）；
3. 上传与队列清空都经 biz_client 写接口（决策 26：引擎零业务库连接串）；
4. 不 import web 任何代码（P3-2）。

注入式设计（R12）：biz_client / task / connectors / storage_dir 全部可注入，
测试零网络（fake quark connector 住 tests/；真实夸克调用延后 manual 项）。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from engine.actions.biz_client import BizApiClient
from engine.actions.tm_proposal import ConsumeOutcome
from engine.netdisk import upload_link_folder
from models.workers import ScrapeBatchResult

logger = logging.getLogger(__name__)


async def consume_scrape_download_done(
    deliverable: dict,
    *,
    task: Any | None = None,
    biz_client: BizApiClient | None = None,
    connectors: dict[str, Any] | None = None,  # 批 7：ctx.connectors（含 quark）
    storage_dir: str | None = None,  # 批 7：扒图存储根（定位链接文件夹）
    **_kwargs,
) -> ConsumeOutcome:
    """下载链完成消费者：upload_netdisk 上传 + from_queue=true 清定时队列。"""
    try:
        result = ScrapeBatchResult.model_validate(deliverable)
    except ValidationError as exc:
        return ConsumeOutcome(
            "rejected", reason=f"下载结果未过 ScrapeBatchResult 契约校验：{exc}"
        )

    # task.input 优先（调度器/web 注入；deliverable 兜底透传）
    task_input: dict[str, Any] = {}
    if task is not None:
        raw = getattr(task, "input", None) or {}
        if isinstance(raw, dict):
            task_input = raw
    from_queue = bool(result.from_queue)
    if isinstance(task_input, dict) and task_input.get("from_queue") is not None:
        from_queue = bool(task_input.get("from_queue", from_queue))
    upload_netdisk = bool(
        task_input.get("upload_netdisk", False)
    )  # 批 7：显式参数，缺省 False

    reason_parts: list[str] = [
        f"下载链完成（batch_id={result.batch_id}，链接 {len(result.links)} 条，"
        f"图片 {len(result.image_ids)} 张）"
    ]

    # ---- 批 7：upload_netdisk=true → 逐条上传夸克（done 且未上传）----
    if upload_netdisk:
        reason_parts.append(await _upload_done_links(
            result, biz_client, connectors, storage_dir
        ))

    # ---- from_queue=true → 清定时队列（原行为）----
    if from_queue:
        if biz_client is None:
            reason_parts.append("定时扒链完成但 biz_client 未注入（无法清定时队列，下次定时跑重试）")
        else:
            try:
                resp = await biz_client.post("/scrape/queue/clear", {})
                if resp.status_code == 200:
                    reason_parts.append("定时队列已清空")
                else:
                    reason_parts.append(
                        f"定时扒链完成但清队列 HTTP {resp.status_code}（下次定时跑重试）"
                    )
            except Exception as exc:  # noqa: BLE001  # BizApiError 等网络/配置异常
                reason_parts.append(
                    f"定时扒链完成但清队列失败（{exc}，下次定时跑重试）"
                )
    else:
        reason_parts.append("不清队列")

    return ConsumeOutcome("accepted", reason="，".join(reason_parts))


async def _upload_done_links(
    result: ScrapeBatchResult,
    biz_client: BizApiClient | None,
    connectors: dict[str, Any] | None,
    storage_dir: str | None,
) -> str:
    """upload_netdisk=true：对 done 且未上传链接逐条上传，返回摘要串。"""
    if biz_client is None:
        return "网盘上传跳过（biz_client 未注入）"
    quark_connector = (connectors or {}).get("quark")
    if quark_connector is None:
        return "网盘上传跳过（quark connector 未注入，请在 设置 → 扒图设置 完成登录）"
    if not storage_dir:
        return "网盘上传跳过（storage_dir 未注入，无法定位链接文件夹）"

    done_links = [
        l for l in result.links
        if l.get("status") == "done" and l.get("link_id") is not None
    ]
    uploaded_n = 0
    failed_n = 0
    skipped_n = 0
    for link in done_links:
        try:
            summary = await upload_link_folder(
                biz_client,
                int(link["link_id"]),
                storage_dir,
                quark_connector,
            )
        except Exception as exc:  # noqa: BLE001 - 单条上传异常不阻断其余
            failed_n += 1
            logger.warning("scrape.download_done: 链接 %s 上传异常：%s", link.get("link_id"), exc)
            continue
        action = summary.get("action")
        if action == "uploaded":
            uploaded_n += 1
        elif action == "skipped":
            skipped_n += 1
        else:
            failed_n += 1
            logger.warning(
                "scrape.download_done: 链接 %s 上传失败：%s",
                link.get("link_id"),
                summary.get("note", ""),
            )
    return (
        f"网盘上传：成功 {uploaded_n} 条"
        + (f"，失败 {failed_n} 条" if failed_n else "")
        + (f"，跳过 {skipped_n} 条" if skipped_n else "")
    )
