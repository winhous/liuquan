"""schedule_reminder 消费者（v0.4 详设 §10.3：tm.schedule）。

链末步产出 ReminderResult（reminders 数组）-> 本消费者（action_id=tm.schedule）：
1. 契约归一（dict 形态 -> ReminderResult，R2）；
2. **校验三件套**：
   ① evidence 非空（决策 16①）；
   ② ref_id 命中白名单（= crm_overdue_context provider 返回 customer_id 集合，
      决策 16③ 白名单正式化）；
   ③ source 完整（customer_id + reminder_date 必填）；
3. HTTP 写接口落库（决策 26）：POST /api/biz/tm/schedule-tasks（逐条提醒，
   source 含 chain_id/engine_task_id/worker_id/customer_id/reminder_date）；
4. 结果映射：
   - 全部 inserted -> inserted
   - 409/校验拒落 = skipped（记日志，不影响其他条，链保持 done，v0.3 candidate
     消费者同款 skipped 语义）
   - 网络异常/500 -> 抛错（链 failed，宁失败不假成功，决策 26 语义）

幂等（详设 §8）：engine_task_id + worker_id 幂等在消费者层（web 写接口层
也有防重，双保险）。

注入式（R12）：whitelist / biz_client / source 可注入（照 crm_candidate 先例）；
缺注入 fail-closed。
"""

from __future__ import annotations

import logging
from collections.abc import Collection

from pydantic import ValidationError

from engine.actions.biz_client import BizApiClient
from engine.actions.tm_proposal import ConsumeOutcome
from models.workers import ReminderResult

logger = logging.getLogger(__name__)

# 用于日志标记
_SKIP_REASONS = {"409", "skipped"}


async def consume_schedule_reminder(
    deliverable: dict,
    *,
    whitelist: Collection[str] | None = None,
    biz_client: BizApiClient | None = None,
    source: dict | None = None,
    **_kwargs,
) -> ConsumeOutcome:
    """提醒任务消费者：校验 + 逐条 POST /api/biz/tm/schedule-tasks 落 tm.task。

    source 注入格式（由 engine/server.py _reminder_source 构造）：
    {chain_id, engine_task_id, worker_id, customer_id, reminder_date, audit_ids}
    """
    # ---- 0. 契约归一 ----
    try:
        result = ReminderResult.model_validate(deliverable)
    except ValidationError as exc:
        return ConsumeOutcome(
            "rejected", reason=f"提醒未过 ReminderResult 契约校验：{exc}"
        )

    if not result.reminders:
        return ConsumeOutcome("inserted", proposal_id=None)  # 空提醒 = 合法

    # ---- 1. 校验三件套 ----
    reject = _check_reminders(result, whitelist, source)
    if reject is not None:
        return ConsumeOutcome("rejected", reason=reject)

    # ---- 2. 逐条 HTTP 写接口落库 ----
    client = biz_client if biz_client is not None else BizApiClient()
    skipped_count = 0
    inserted_count = 0

    for item in result.reminders:
        # 为每条 reminder 构造独立 source（含该条的 customer_id）
        item_source = {
            "chain_id": (source or {}).get("chain_id", ""),
            "engine_task_id": (source or {}).get("engine_task_id", ""),
            "worker_id": (source or {}).get("worker_id", ""),
            "customer_id": item.customer_id,
            "reminder_date": (source or {}).get("reminder_date", ""),
            "audit_ids": (source or {}).get("audit_ids", []),
        }
        payload = {
            "title": item.title,
            "detail": item.detail,
            "domain": "crm",
            "source": item_source,
            "role": "运营",
            "due": (source or {}).get("reminder_date", ""),
            "evidence": [
                ev.model_dump(mode="json") for ev in item.evidence
            ],
        }
        try:
            resp = await client.post("/tm/schedule-tasks", payload)
        except Exception as exc:  # noqa: BLE001
            # 网络异常 -> 抛错（链 failed，宁失败不假成功，决策 26）
            raise RuntimeError(
                f"定时任务写接口调用失败（客户 {item.customer_id}）：{exc}"
            ) from exc

        if resp.status_code == 409:
            # 防重跳过（同客户当天已生成），记日志不影响其他条
            logger.info(
                "schedule_reminder: 客户 %d 当天已生成提醒任务，跳过",
                item.customer_id,
            )
            skipped_count += 1
        elif resp.status_code != 200:
            # 500 等非预期 -> 抛错（宁失败不假成功）
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
            raise RuntimeError(
                f"定时任务写接口 HTTP {resp.status_code}（客户 {item.customer_id}）：{detail}"
            )
        else:
            inserted_count += 1

    total = len(result.reminders)
    if inserted_count == total:
        return ConsumeOutcome("inserted", proposal_id=None)
    if inserted_count == 0:
        return ConsumeOutcome(
            "skipped_idempotent",
            reason=f"全部 {total} 条提醒被防重跳过",
        )
    # 部分插入部分跳过 -> inserted（链保持 done，跳过的记日志）
    return ConsumeOutcome("inserted", proposal_id=None)


def _check_reminders(
    result: ReminderResult,
    whitelist: Collection[str] | None,
    source: dict | None,
) -> str | None:
    """校验三件套：evidence 非空 / ref_id 命中白名单 / source 完整。"""
    if not result.reminders:
        return None  # 空提醒是合法产出

    # ① evidence 非空（决策 16①）
    for item in result.reminders:
        if not item.evidence:
            return (
                f"提醒「{item.title}」evidence 为空：无依据不出建议（拒落）"
            )

    # ② ref_id 命中白名单（决策 16③）
    if whitelist is None:
        return "whitelist 未注入（数据引用封闭性无法校验，fail-closed 拒落）"
    allowed = set(whitelist)
    outside = sorted(
        {
            ev.ref_id
            for item in result.reminders
            for ev in item.evidence
            if ev.ref_id not in allowed
        }
    )
    if outside:
        detail = "、".join(f"幻觉证据: ref_id={rid}" for rid in outside)
        return (
            f"{detail}：evidence 引用了链未喂给的数据对象"
            "（数据引用封闭性，决策 16，拒落）"
        )

    # ③ source 完整（customer_id + reminder_date 必填）
    if source is not None:
        if not source.get("customer_id"):
            return "source.customer_id 缺失（提醒落库必填，拒落）"
        if not source.get("reminder_date"):
            return "source.reminder_date 缺失（提醒落库必填，拒落）"

    return None


__all__ = ["consume_schedule_reminder"]
