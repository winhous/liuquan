"""crm_follow_up_reminder 工序 ACT（v0.4 详设 §10.1；决策 37 纯代码提醒）。

纯代码工序（worker.yaml 的 reason: none，无 LLM 调用，零 token 成本）：
从 crm_overdue_context provider 拿超期客户清单 -> 逐客户生成 ReminderItem。

title 模板（代码）：跟进客户：{nickname}（已 {days_since} 天未跟进）
detail = latest_summary（截断，长度由 config 控制）+ 客户标识
evidence = [{kind: 'customer', ref_id: str(customer_id), quote: nickname}]
（白名单内 = provider 返回，决策 16③）。

空清单 = reminders 空数组（合法，无超期客户时不生成提醒）。
"""

from engine.core.context import EngineContext
from models.workers import ReminderChainInput, ReminderItem, ReminderResult


def run(inputs: ReminderChainInput, ctx: EngineContext) -> ReminderResult:
    """逐超期客户生成提醒项（确定性；无超期客户 -> 空数组）。"""
    max_detail_len = int(ctx.config.get("max_detail_length", 200))

    # 从 context_data 拿 provider 返回的超期客户清单
    overdue_data = ctx.context_data.get("crm_overdue_context")
    if overdue_data is None:
        return ReminderResult(reminders=[])

    # overdue_data 可能是 dict（序列化后）或 ReminderContextData（直接注入）
    customers = getattr(overdue_data, "customers", None)
    if customers is None and isinstance(overdue_data, dict):
        customers = overdue_data.get("customers", [])

    reminders: list[ReminderItem] = []
    for cust in customers:
        # 兼容 dict 和 ReminderCustomer 对象
        if isinstance(cust, dict):
            cid = cust.get("customer_id", 0)
            nickname = cust.get("nickname", "")
            days_since = cust.get("days_since", 0)
            latest_summary = cust.get("latest_summary", "")
        else:
            cid = cust.customer_id
            nickname = cust.nickname
            days_since = cust.days_since
            latest_summary = cust.latest_summary

        title = f"跟进客户：{nickname}（已 {days_since} 天未跟进）"
        detail = latest_summary[:max_detail_len] if latest_summary else ""
        if detail:
            detail += f"\n客户：{nickname}（ID: {cid}）"
        else:
            detail = f"客户：{nickname}（ID: {cid}）"

        reminders.append(
            ReminderItem(
                customer_id=cid,
                title=title,
                detail=detail,
                days_since=days_since,
                evidence=[
                    {
                        "kind": "customer",
                        "ref_id": str(cid),
                        "quote": nickname,
                    }
                ],
            )
        )

    return ReminderResult(reminders=reminders)
