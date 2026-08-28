"""TM 转交器（详设-v0.2 §5：Action 消费者，链完成回调把 TaskProposal 落业务库）。

引擎不直接写业务库——链完成后由注册的 Action 消费者（纯代码）把
TaskProposal 落 tm.task_proposal（status=pending），AI 全程不碰库
（AI 100% 可控，承最高原则；R5 AI 不碰账本）。本模块是 v0.2 唯一消费者
（engine/actions/__init__.py 注册表登记 action_id=tm.proposal）。

职责（详设 §5 + §3.2 禁幻觉三件套 + §3.2 幂等与防重 + 任务书技术决策）：
1. **业务校验三件套（机器强制，违反任一拒落 + 调用方标链 FAILED）**：
   ① evidence 非空（无依据不出建议，详设 v0.1 §6.4）——复用
      models/contract/validation.assert_suggest_has_evidence 契约函数；
   ② audit_ids 可查——非空时每个 id 能在引擎库 engine_audit 查到（注入的
      audit_lookup，参考 validate_audit_ids 模式）；**为空 = 纯代码工序产出
      （任务书技术决策）**：按 source.worker_id 查 registry 工序声明，若
      reason=none 则豁免（无 LLM 调用本身可追溯——source.engine_task_id 可
      查到引擎任务与步骤）；否则（工序 reason=llm 却无审计）拒落；
   ③ 数据引用封闭性（决策 16）——evidence 每个 ref_id 必须命中注入的
      whitelist（本链喂给 AI 的数据集合）；白名单外 = 拒落 + 报告
      「幻觉证据: ref_id=...」。
2. **幂等与防重（决策 17）**：
   - 同链重跑幂等：按 source.engine_task_id + worker_id 去重（已存在跳过）；
   - 同事件防重：同 ref_id + action_id 已有未完成任务（status 非 done/void）
     或待审提案（status=pending）-> 本次不落库（skip 日志，链仍 DONE）。
3. **落库**：校验全过 -> 写 tm.task_proposal（status=pending）。risk 由代码
   规则标注 = 查 Action 声明（registry.actions[action_id].risk），非 AI 自评
   （R9）；字段映射自 TaskProposal 契约（title/detail/domain/action_id/
   suggested_role/suggested_due_days/evidence/source）。

注入式设计（规范 R12：audit_lookup / whitelist / tm_engine / registry 全部
可注入，测试零网络零真服务/嵌入式 PG；R21 契约依赖）：
- tm_engine：业务库 AsyncEngine；缺省经 create_tm_engine() 从 .env 的
  LIUQUAN_TM_DB_URL 建（P2 规则 4：dotenv_values 读 .env 文件，不触碰
  os.environ；先例 engine/core/db.py 的 create_engine）。
- registry：工序/Action 声明注册表；缺省惰性 load_registry（查工序
  reason 豁免 audit_ids、查 Action 声明取 risk）。
- audit_lookup：引擎库 engine_audit 可查函数（真实场景由调用方注入 =
  查引擎库的可调用；测试 = 桩函数，不打真库）。
- whitelist：本链喂给 AI 的数据集合（对象 id 集合；缺省 None = 无法校验
  数据引用封闭性，fail-closed 拒落）。

不 import web 任何代码（P3-2：web/ 与引擎双向零代码耦合，R24）；本模块
属引擎包（engine/actions/），可 import engine.registry / engine.core 与
models（R22 唯一通用语言：业务表 ORM 住 models/tm.py，web 与转交器共用）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from engine.registry import Registry, load_registry
from models.contract.task import TaskProposal
from models.contract.validation import validate_audit_ids
from models.tm import TaskProposal as TmTaskProposalRow

# 业务库连接串变量名（R20：值只存 .env；本模块经 dotenv_values 读仓库根
# .env 文件，不触碰 os.environ——P2 规则 4 的合法来源即「.env 文件」）
_TM_DB_URL_ENV = "LIUQUAN_TM_DB_URL"

# 仓库根 .env 路径（engine/actions/tm_proposal.py 上溯两级；不依赖 cwd）
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOTENV_PATH = _REPO_ROOT / ".env"

# 提案拒落原因文案（详设 §5 / §3.2：幻觉证据错误格式）
_REASON_HALLUCINATED = "幻觉证据: ref_id="

# 审计豁免：纯代码工序 reason 值（任务书技术决策：audit_ids 为空仅当工序
# 声明 reason: none 才豁免；WorkerDeclaration.reason 枚举见 schema.py）
_REASON_NONE = "none"


@dataclass(frozen=True)
class ConsumeOutcome:
    """一次转交的结果（调用方据此记日志/标链终态；status 四态）。

    - inserted：校验全过 + 幂等/防重通过，已落 tm.task_proposal(pending)
    - skipped_idempotent：同链同工序提案重复交付（source.engine_task_id +
      worker_id 已存在），跳过（链仍 DONE，不重复建任务）
    - skipped_duplicate：同事件防重（同 ref_id + action_id 已有未完成任务/
      待审提案），本次不落库（skip 日志）
    - rejected：三件套任一未过（拒落，调用方应标链 FAILED）
    """

    status: str
    proposal_id: int | None = None  # inserted 时 = tm.task_proposal.id
    reason: str | None = None  # rejected / skipped 的说明


def create_tm_engine(url: str | None = None) -> AsyncEngine:
    """建业务库 AsyncEngine；url 缺省从仓库根 .env 的 LIUQUAN_TM_DB_URL 读。

    连接串是敏感值（含密码）：R20 规定值只存 .env；本函数经 dotenv_values
    读 .env 文件（P2 规则 4 的合法来源，先例 engine/core/db.py），不触碰
    os.environ——P2 对 engine/actions 的 URL/IP 字面量检查保持生效（禁止
    连接串硬编码进代码）。
    """
    if url is None:
        url = dotenv_values(_DOTENV_PATH).get(_TM_DB_URL_ENV)
        if not url:
            raise ValueError(
                f".env 的 {_TM_DB_URL_ENV} 未设置：create_tm_engine 需要业务库"
                "连接串（规范 R20：值只存 .env，cp .env.example .env 后填入）"
            )
    return create_async_engine(url)


async def consume_task_proposal(
    proposal: TaskProposal,
    *,
    tm_engine: AsyncEngine | None = None,
    registry: Registry | None = None,
    audit_lookup: Callable[[list[str]], bool] | None = None,
    whitelist: Collection[str] | None = None,
) -> ConsumeOutcome:
    """TM 转交器：校验（禁幻觉三件套）+ 幂等/防重 + 落 tm.task_proposal。

    全部依赖可注入（R12）：tm_engine 缺省建真连接（.env 的
    LIUQUAN_TM_DB_URL）；registry 缺省惰性加载；audit_lookup / whitelist
    缺省 None——缺注入时 fail-closed（拒落），宁失败不假成功。
    """
    engine = tm_engine
    owned = engine is None
    if owned:
        engine = create_tm_engine()
    try:
        return await _consume(
            engine,
            proposal,
            registry=registry,
            audit_lookup=audit_lookup,
            whitelist=whitelist,
        )
    finally:
        if owned:
            await engine.dispose()


async def _consume(
    engine: AsyncEngine,
    proposal: TaskProposal,
    *,
    registry: Registry | None,
    audit_lookup: Callable[[list[str]], bool] | None,
    whitelist: Collection[str] | None,
) -> ConsumeOutcome:
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

    # ---- 2. 幂等与防重（决策 17）----
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        if await _exists_same_delivery(session, validated):
            return ConsumeOutcome(
                "skipped_idempotent",
                reason=(
                    "同链同工序提案重复交付（source.engine_task_id + worker_id"
                    " 已存在），跳过——同链重跑幂等（决策 17）"
                ),
            )
        if await _exists_duplicate(session, validated):
            return ConsumeOutcome(
                "skipped_duplicate",
                reason=(
                    "同事件防重：ref_id + action_id 已有未完成任务/待审提案，"
                    "本次不落库（决策 17，链仍 DONE）"
                ),
            )
        row_id = await _insert_proposal(session, validated, action.risk)
    return ConsumeOutcome("inserted", proposal_id=row_id)


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


# ---- 幂等与防重查询（tm 表；JSONB 检索用 text SQL 直陈，勿引引擎库）----


async def _exists_same_delivery(session: AsyncSession, proposal: TaskProposal) -> bool:
    """同链重跑幂等：source.engine_task_id + worker_id 已有提案 -> 跳过。"""
    row = await session.execute(
        text(
            "SELECT id FROM tm.task_proposal "
            "WHERE source->>'engine_task_id' = :eid AND source->>'worker_id' = :wid "
            "LIMIT 1"
        ),
        {"eid": proposal.source.engine_task_id, "wid": proposal.source.worker_id},
    )
    return row.scalar_one_or_none() is not None


async def _exists_duplicate(session: AsyncSession, proposal: TaskProposal) -> bool:
    """同事件防重（决策 17）：同 ref_id + action_id 已有未完成任务/待审提案。

    防重键 = (ref_id, action_id)（详设 §3.2：tm.task_proposal 查询 +
    tm.task 查询双保险；唯一索引留待迁移补充，本任务先落查询层）。
    """
    for ref_id in {ev.ref_id for ev in proposal.evidence}:
        if await _pending_proposal_exists(session, proposal.action_id, ref_id):
            return True
        if await _unfinished_task_exists(session, proposal.action_id, ref_id):
            return True
    return False


async def _pending_proposal_exists(
    session: AsyncSession, action_id: str, ref_id: str
) -> bool:
    """待审提案（status=pending）同 ref_id + action_id 已存在。"""
    row = await session.execute(
        text(
            "SELECT id FROM tm.task_proposal "
            "WHERE status = 'pending' AND action_id = :aid "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(evidence) e "
            "             WHERE e->>'ref_id' = :rid) "
            "LIMIT 1"
        ),
        {"aid": action_id, "rid": ref_id},
    )
    return row.scalar_one_or_none() is not None


async def _unfinished_task_exists(
    session: AsyncSession, action_id: str, ref_id: str
) -> bool:
    """未完成任务（status 非 done/void）经批准提案带同 ref_id + action_id。"""
    row = await session.execute(
        text(
            "SELECT t.id FROM tm.task t "
            "JOIN tm.task_proposal tp ON tp.task_id = t.id "
            "WHERE t.status NOT IN ('done', 'void') AND tp.action_id = :aid "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(tp.evidence) e "
            "             WHERE e->>'ref_id' = :rid) "
            "LIMIT 1"
        ),
        {"aid": action_id, "rid": ref_id},
    )
    return row.scalar_one_or_none() is not None


# ---- 落库 ----


async def _insert_proposal(
    session: AsyncSession, proposal: TaskProposal, risk: str
) -> int:
    """写 tm.task_proposal（status=pending）；risk 已由调用方查 Action 声明标注。"""
    row = TmTaskProposalRow(
        title=proposal.title,
        detail=proposal.detail,
        domain=proposal.domain,
        action_id=proposal.action_id,
        risk=risk,  # 代码规则标注：查 Action 声明（domain+动作类型查表）
        suggested_role=proposal.suggested_role,
        suggested_due_days=proposal.suggested_due_days,
        evidence=[
            ev.model_dump(mode="json") for ev in proposal.evidence
        ],  # EvidenceRef 数组 JSONB（对齐契约 §6.4）
        source=proposal.source.model_dump(mode="json"),  # SourceTrace JSONB（§3.4）
        # status 由 DDL 默认 'pending'（审核态三态，决策 12 全部人工审）
    )
    session.add(row)
    await session.flush()
    return row.id


# ---- 消费者注册表类型（engine/actions/__init__.py 引用）----

Consumer = Callable[..., Awaitable[ConsumeOutcome]]

__all__ = ["ConsumeOutcome", "Consumer", "consume_task_proposal", "create_tm_engine"]
