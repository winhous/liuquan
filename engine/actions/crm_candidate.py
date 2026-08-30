"""crm_candidate 消费者（详设-v0.3 §5.4：todo_generate 产出候选落 crm.todo_candidate）。

链末步产出 TodoCandidateResult（todos 数组）-> 本消费者（action_id=crm.candidate）：
1. 契约归一（dict 形态 -> TodoCandidateResult，R2）；
2. **禁幻觉三件套（决策 16，机器强制）**：每条候选 evidence 非空 / audit_ids
   可查（todo_generate 为 reason=llm 工序，必须有审计）/ ref_id 命中 whitelist
   （= crm_chat_context provider 返回 id 集合，白名单正式化 v0.3）；
3. HTTP 写接口落库（决策 26）：POST /api/biz/crm/candidates（逐条候选，
   含 source{chain_id, engine_task_id, worker_id, audit_ids}）——幂等
   （engine_task_id+content）在 web 接口层；
4. 结果映射：全部 ok -> inserted；任一 rejected/网络异常 -> rejected（宁失败
   不假成功：候选没落库不能算成功）。

source 注入（技术定）：TodoCandidateResult 无 source 字段（工序输出契约），
候选来源上下文（customer_id/chain_id/engine_task_id/worker_id/audit_ids）由
调用方（engine/server.py）从链上下文（task.input 的 customer_id + 引擎任务
信息）构造注入——R21 契约依赖，消费者不自行拼装。

注入式设计（R12）：registry / audit_lookup / whitelist / biz_client / source
可注入，缺注入 fail-closed。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from pathlib import Path

from pydantic import ValidationError

from engine.actions.biz_client import BizApiClient
from engine.actions.tm_proposal import ConsumeOutcome
from engine.registry import Registry, load_registry
from models.contract.validation import validate_audit_ids
from models.workers import TodoCandidateResult

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REASON_HALLUCINATED = "幻觉证据: ref_id="
_REASON_NONE = "none"


async def consume_todo_candidates(
    deliverable: dict,
    *,
    registry: Registry | None = None,
    audit_lookup: Callable[[list[str]], bool] | None = None,
    whitelist: Collection[str] | None = None,
    biz_client: BizApiClient | None = None,
    source: dict | None = None,
) -> ConsumeOutcome:
    """CRM 候选消费者：校验（禁幻觉三件套）+ HTTP 写接口落 crm.todo_candidate。"""
    # ---- 0. 契约归一（dict 形态 -> TodoCandidateResult，R2 输出即类型）----
    try:
        result = TodoCandidateResult.model_validate(deliverable)
    except ValidationError as exc:
        return ConsumeOutcome(
            "rejected", reason=f"候选未过 TodoCandidateResult 契约校验：{exc}"
        )

    if registry is None:
        registry = load_registry(_REPO_ROOT)

    # ---- 1. 禁幻觉三件套（每条候选）----
    reject = _check_candidates(result, registry, audit_lookup, whitelist, source)
    if reject is not None:
        return ConsumeOutcome("rejected", reason=reject)

    # ---- 2. HTTP 写接口逐条落库（决策 26；幂等在 web 接口层）----
    client = biz_client if biz_client is not None else BizApiClient()
    for item in result.todos:
        payload = {
            "customer_id": (source or {}).get("customer_id"),
            "content": item.content,
            "reason": item.reason,
            "suggested_tags": item.suggested_tags,
            "evidence": [ev.model_dump(mode="json") for ev in item.evidence],
            "source": {
                "chain_id": (source or {}).get("chain_id", ""),
                "engine_task_id": (source or {}).get("engine_task_id", ""),
                "worker_id": (source or {}).get("worker_id", ""),
                "audit_ids": (source or {}).get("audit_ids", []),
            },
        }
        try:
            resp = await client.post("/crm/candidates", payload)
        except Exception as exc:  # noqa: BLE001
            return ConsumeOutcome(
                "rejected", reason=f"候选写接口调用失败：{exc}"
            )
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
            return ConsumeOutcome(
                "rejected",
                reason=f"候选写接口 HTTP {resp.status_code}：{detail}",
            )
        if resp.json().get("skipped"):
            return ConsumeOutcome(
                "skipped_idempotent",
                reason="候选写接口判幂等跳过（同引擎任务同内容候选已存在，决策 17）",
            )
    return ConsumeOutcome("inserted", proposal_id=None)


def _check_candidates(
    result: TodoCandidateResult,
    registry: Registry,
    audit_lookup: Callable[[list[str]], bool] | None,
    whitelist: Collection[str] | None,
    source: dict | None,
) -> str | None:
    """禁幻觉三件套：每条候选 evidence 非空 / audit_ids 可查 / ref_id 白名单封闭。"""
    if not result.todos:
        return None  # 空候选数组是合法产出（规则：确实无可生成的返回空数组）
    # ① evidence 非空（无依据不出建议，决策 16①）
    for item in result.todos:
        if not item.evidence:
            return f"候选「{item.content}」evidence 为空：无依据不出建议（拒落）"
    # ② audit_ids 可查（todo_generate 为 reason=llm 工序，必须带审计）
    audit_ids = (source or {}).get("audit_ids") or []
    if audit_ids:
        if audit_lookup is None:
            return "audit_ids 非空但 audit_lookup 未注入（无法校验可查性，拒落）"
        if not audit_lookup(audit_ids):
            return "audit_ids 不可查：引擎库查不到对应 LLM 调用（追溯保证，拒落）"
    else:
        worker = registry.workers.get((source or {}).get("worker_id") or "")
        if worker is None or worker.reason != _REASON_NONE:
            return (
                f"候选 audit_ids 为空且工序 {((source or {}).get('worker_id'))!r} "
                "非纯代码（reason=llm 无审计追溯，拒落）"
            )
    # ③ 数据引用封闭性（决策 16③）：ref_id 命中白名单
    if whitelist is None:
        return "whitelist 未注入（数据引用封闭性无法校验，fail-closed 拒落）"
    allowed = set(whitelist)
    outside = sorted(
        {
            ev.ref_id
            for item in result.todos
            for ev in item.evidence
            if ev.ref_id not in allowed
        }
    )
    if outside:
        detail = "、".join(f"{_REASON_HALLUCINATED}{rid}" for rid in outside)
        return (
            f"{detail}：evidence 引用了链未喂给 AI 的数据对象"
            "（数据引用封闭性，决策 16，拒落）"
        )
    return None


__all__ = ["consume_todo_candidates"]
