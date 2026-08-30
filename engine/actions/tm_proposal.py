"""TM 转交器（详设-v0.2 §5 + 详设-v0.3 §5.4：Action 消费者，链完成回调把
TaskProposal 落业务库）。

引擎不直接写业务库——链完成后由注册的 Action 消费者（纯代码）把
TaskProposal 落 tm.task_proposal（status=pending），AI 全程不碰库
（AI 100% 可控，承最高原则；R5 AI 不碰账本）。本模块是 v0.2 消费者
（engine/actions/__init__.py 注册表登记 action_id=tm.proposal）。

**v0.3 改造（决策 26 业务写入接口化）**：落库由「直连业务库（create_tm_engine
+ SQL）」改为「HTTP 调 web /api/biz/tm/proposals 写接口」——引擎进程零业务库
连接串；幂等/防重查询上移 web 写接口层（引擎无法查业务库）；**禁幻觉三件套
保留在本消费者**（evidence 非空 / audit_ids 可查 / ref_id 白名单封闭——决策
16 校验职责在引擎侧，接口只校验数据正确性）。

职责：
1. **业务校验三件套（机器强制，违反任一拒落 + 调用方标链 FAILED）**：
   ① evidence 非空（无依据不出建议，详设 v0.1 §6.4）；
   ② audit_ids 可查——非空时每个 id 能在引擎库 engine_audit 查到（注入的
      audit_lookup）；为空 = 纯代码工序产出（工序声明 reason=none 豁免）；
   ③ 数据引用封闭性（决策 16）——evidence 每个 ref_id 必须命中注入的
      whitelist（本链喂给 AI 的数据集合 = provider 返回 id 集合，v0.3 正式化）；
      白名单外 = 拒落 + 报告「幻觉证据: ref_id=...」。
2. **risk 代码规则标注（R9）**：查 Action 声明（registry.actions[action_id].risk），
   随请求体传给写接口（接口校验 risk 值域）。
3. **落库**：HTTP POST /api/biz/tm/proposals（TaskProposalWrite = 契约 + risk）；
   响应 {ok, id, skipped} -> inserted / skipped_idempotent；422/网络异常 ->
   rejected（宁失败不假成功）。

注入式设计（规范 R12：audit_lookup / whitelist / biz_client / registry 全部
可注入，测试零网络零真服务；R21 契约依赖）：
- biz_client：BizApiClient（HTTP 写接口客户端，决策 26）；缺省惰性建
  （.env 的 LIUQUAN_BIZ_API_URL / LIUQUAN_BIZ_API_TOKEN）。
- registry：工序/Action 声明注册表；缺省惰性 load_registry。
- audit_lookup / whitelist：同 v0.2 语义（缺省 None = fail-closed 拒落）。

不 import web 任何代码（P3-2：web/ 与引擎双向零代码耦合，R24）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from engine.actions.biz_client import BizApiClient
from engine.registry import Registry, load_registry
from models.contract.task import TaskProposal
from models.contract.validation import validate_audit_ids

# 仓库根 .env 路径（engine/actions/tm_proposal.py 上溯两级；不依赖 cwd）
_REPO_ROOT = Path(__file__).resolve().parents[2]

# 提案拒落原因文案（详设 §5 / §3.2：幻觉证据错误格式）
_REASON_HALLUCINATED = "幻觉证据: ref_id="

# 审计豁免：纯代码工序 reason 值（audit_ids 为空仅当工序声明 reason: none 豁免）
_REASON_NONE = "none"


@dataclass(frozen=True)
class ConsumeOutcome:
    """一次转交的结果（调用方据此记日志/标链终态；status 四态）。

    - inserted：校验全过 + 写接口已落 tm.task_proposal(pending)
    - skipped_idempotent：写接口判幂等（source.engine_task_id + worker_id
      已存在）或同事件防重跳过（链仍 DONE，不重复建任务）
    - rejected：三件套任一未过 / 写接口校验拒绝 / 网络异常（调用方标链 FAILED）
    """

    status: str
    proposal_id: int | None = None  # inserted 时 = tm.task_proposal.id
    reason: str | None = None  # rejected / skipped 的说明


async def consume_task_proposal(
    proposal: TaskProposal,
    *,
    registry: Registry | None = None,
    audit_lookup: Callable[[list[str]], bool] | None = None,
    whitelist: Collection[str] | None = None,
    biz_client: BizApiClient | None = None,
) -> ConsumeOutcome:
    """TM 转交器：校验（禁幻觉三件套）+ risk 标注 + HTTP 写接口落库。

    全部依赖可注入（R12）：biz_client 缺省惰性建（.env 的
    LIUQUAN_BIZ_API_URL / TOKEN）；registry 缺省惰性加载；audit_lookup /
    whitelist 缺省 None——缺注入时 fail-closed（拒落），宁失败不假成功。
    """
    # ---- 0. 契约类型校验（R2 输出即类型；dict 形态一并归一）----
    try:
        validated = TaskProposal.model_validate(proposal)
    except ValidationError as exc:
        return ConsumeOutcome(
            "rejected", reason=f"提案未过 TaskProposal 契约校验：{exc}"
        )

    # ---- 注册表（工序 reason 豁免 / Action 声明 risk 需要；缺省惰性加载）----
    if registry is None:
        registry = load_registry(_REPO_ROOT)

    # ---- 1. 业务校验三件套（机器强制，违反任一拒落）----
    reject = _check_triple(validated, registry, audit_lookup, whitelist)
    if reject is not None:
        return ConsumeOutcome("rejected", reason=reject)

    # ---- risk 代码规则标注（R9：查 Action 声明，非 AI 自评；缺声明拒落）----
    action = registry.actions.get(validated.action_id)
    if action is None:
        return ConsumeOutcome(
            "rejected",
            reason=(
                f"Action 声明缺失：action_id={validated.action_id!r} 未登记"
                "（risk 必须由代码规则标注，非 AI 自评，拒落）"
            ),
        )

    # ---- 2. HTTP 写接口落库（决策 26：引擎零业务库连接串；幂等/防重
    #        上移 web /api/biz/tm/proposals 接口层）----
    client = biz_client if biz_client is not None else BizApiClient()
    payload = {**validated.model_dump(mode="json"), "risk": action.risk}
    try:
        resp = await client.post("/tm/proposals", payload)
    except Exception as exc:  # noqa: BLE001  # BizApiError 等网络/配置异常
        return ConsumeOutcome(
            "rejected", reason=f"业务写接口调用失败：{exc}"
        )
    if resp.status_code != 200:
        detail = ""
        try:
            detail = str(resp.json().get("detail", ""))
        except Exception:  # noqa: BLE001
            detail = resp.text[:200]
        return ConsumeOutcome(
            "rejected",
            reason=f"业务写接口 HTTP {resp.status_code}：{detail}",
        )
    body = resp.json()
    if body.get("skipped"):
        # 写接口区分幂等/防重（决策 17）：skip_reason 由 web 接口层标注
        if body.get("skip_reason") == "duplicate":
            return ConsumeOutcome(
                "skipped_duplicate",
                reason=(
                    "同事件防重：ref_id + action_id 已有未完成任务/待审提案，"
                    "本次不落库（决策 17，链仍 DONE）"
                ),
            )
        return ConsumeOutcome(
            "skipped_idempotent",
            reason=(
                "写接口判幂等跳过（同链同工序重复交付，决策 17——链仍 DONE）"
            ),
        )
    return ConsumeOutcome("inserted", proposal_id=body.get("id"))


# ---- 三件套校验（返回拒落原因；None = 全过）----


def _check_triple(
    proposal: TaskProposal,
    registry: Registry,
    audit_lookup: Callable[[list[str]], bool] | None,
    whitelist: Collection[str] | None,
) -> str | None:
    """禁幻觉三件套：evidence 非空 / audit_ids 可查 / ref_id 白名单封闭。"""
    # ① evidence 非空（无依据不出建议，详设 v0.1 §6.4）
    if not proposal.evidence:
        return "evidence 为空：无依据不出建议（详设 v0.1 §6.4，拒落）"

    # ② audit_ids 可查（追溯保证）；为空 = 纯代码工序产出，按工序声明豁免
    ids = proposal.source.audit_ids
    if ids:
        if audit_lookup is None:
            return "audit_ids 非空但 audit_lookup 未注入（无法校验可查性，拒落）"
        if not validate_audit_ids(proposal, audit_lookup):
            return "audit_ids 不可查：引擎库查不到对应 LLM 调用（追溯保证，拒落）"
    else:
        worker = registry.workers.get(proposal.source.worker_id)
        if worker is None:
            return (
                f"audit_ids 为空且工序 {proposal.source.worker_id!r} 未登记"
                "（无法确认纯代码工序豁免，拒落）"
            )
        if worker.reason != _REASON_NONE:
            return (
                f"audit_ids 为空但工序 {proposal.source.worker_id} 声明"
                f" reason={worker.reason}（非纯代码，无审计追溯，拒落）"
            )
        # reason: none -> 豁免：无 LLM 调用本身可追溯（engine_task_id 可查
        # 引擎任务与步骤），任务书技术决策

    # ③ 数据引用封闭性（决策 16）：ref_id 必须命中链数据白名单
    if whitelist is None:
        return "whitelist 未注入（数据引用封闭性无法校验，fail-closed 拒落）"
    allowed = set(whitelist)
    outside = sorted({ev.ref_id for ev in proposal.evidence if ev.ref_id not in allowed})
    if outside:
        detail = "、".join(f"{_REASON_HALLUCINATED}{rid}" for rid in outside)
        return (
            f"{detail}：evidence 引用了链未喂给 AI 的数据对象"
            "（数据引用封闭性，决策 16，拒落）"
        )
    return None


# ---- 消费者注册表类型（engine/actions/__init__.py 引用）----

Consumer = Callable[..., Awaitable[ConsumeOutcome]]

__all__ = ["ConsumeOutcome", "Consumer", "consume_task_proposal"]
