"""web 侧业务库访问层（详设-v0.2 §6：任务列表/筛选/状态操作/派生/编辑/重开/
人工建任务/提案审核/逾期统计）。

- 任务书技术决策：业务库连接与查询逻辑**独立住本模块**——只依赖
  models/tm.py（R22 共享 ORM，web 与引擎转交器共用同一 Model）+ SQLAlchemy
  async；**零 engine import**（lint P3-2：web/ 不得 import engine.*，双向
  零代码耦合，R24；引擎进程也不 import web.*）。
- 连接串读仓库根 .env 的 LIUQUAN_TM_DB_URL（R20：值只存 .env；P2 规则 4：
  dotenv_values 读 .env 文件、不触碰 os.environ——先例
  engine/actions/tm_proposal.py 的 create_tm_engine 模式，但本模块独立实现
  同款模式，不 import 引擎任何模块）。
- 状态转换代码写死（R1：状态转换全部代码写死，AI 无权决定下一步；详设
  §3.5.1 状态机）：
      open --started--> in_progress --completed(必填 result_note)--> done
        |                    |            --voided(必填 result_note)--> void
        |                    --blocked--> blocked --unblocked--> in_progress
  每次操作写 tm.task_event（§3.3 事件写入点表）+ 更新 tm.task；done/void
  写 done_at；completed/voided 事件 note=result_note（决策 17「不回复不能
  结束」——页面校验 + DB CHECK chk_result_note 双保险）。
- 派生（§3.5.2 决策 17）：建新任务 derived_from=原任务 id（**派生≠原任务
  结束**，原任务保持原状态）；新任务记 created 事件、原任务记 derived 事件
  （note=派生出的任务展示 id）。
- 提案批准（§3.2 审核流）：单事务内 提案 approved + 建 tm.task
  （source_type=ai，source 复制提案来源并追加 proposal_id）+ 提案 task_id
  回填 + created/approved 两条事件；驳回：rejected + reject_reason。
- 业务规则拒绝抛 TMWebError（路由捕获 -> 页面 err 提示）；DB CHECK
  （chk_result_note / chk_blocked_reason / 角色枚举）兜底双保险。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from models.tm import Task, TaskEvent, TaskProposal

# 任务 + 派生关联视图（决策 17 / 用户复核反馈：展示层需要父任务标题与子任务
# 数，增强派生任务的父子关联性——「由 t-xxx「父标题」派生」+「N 个子任务」）
from dataclasses import dataclass


@dataclass(frozen=True)
class _TaskWithRel:
    """list_tasks 返回包装：task + 父任务标题 + 子任务数（None/0 = 无派生关联）。"""

    task: Task
    parent_title: str | None = None
    child_count: int = 0

# 业务库连接串变量名（R20：值只存 .env；本模块经 dotenv_values 读仓库根
# .env 文件，不触碰 os.environ——P2 规则 4 的合法来源即「.env 文件」）
_TM_DB_URL_ENV = "LIUQUAN_TM_DB_URL"

# web/tm_store.py -> web/ -> 仓库根（不依赖 cwd）
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOTENV_PATH = _REPO_ROOT / ".env"

# 负责人按角色指派（决策 11；对齐 tm.task 的 role CHECK）
ROLE_VALUES = ("运营", "采购", "管理员")
# 阻塞原因枚举（决策 11：⚑ 枚举先固定这两个，业务后续要加走变更日志加 CHECK 值）
BLOCKED_REASONS = ("等物料", "等回复")


class TMWebError(Exception):
    """业务规则拒绝（路由捕获 -> 页面 err 提示；不是 500）。"""


def task_display_id(task_id: int) -> str:
    """任务展示形：t-<6位零填充>（详设 §3.1）。"""
    return f"t-{task_id:06d}"


def proposal_display_id(proposal_id: int) -> str:
    """提案展示形：p-<6位零填充>（详设 §3.2）。"""
    return f"p-{proposal_id:06d}"


def create_tm_engine(url: str | None = None) -> AsyncEngine:
    """建业务库 AsyncEngine；url 缺省从仓库根 .env 的 LIUQUAN_TM_DB_URL 读。

    连接串是敏感值（含密码）：R20 规定值只存 .env；本函数经 dotenv_values
    读 .env 文件（P2 规则 4 的合法来源，先例 engine/actions/tm_proposal.py
    的 create_tm_engine——但本模块独立实现，不 import 引擎），不触碰
    os.environ，禁止连接串硬编码进代码。
    """
    if url is None:
        url = dotenv_values(_DOTENV_PATH).get(_TM_DB_URL_ENV)
        if not url:
            raise ValueError(
                f".env 的 {_TM_DB_URL_ENV} 未设置：web 需要业务库连接串"
                "（规范 R20：值只存 .env，cp .env.example .env 后填入）"
            )
    return create_async_engine(url)


# 逾期 = status 非 done/void 且 due < 今天（决策 17 第 5 条：逾期一直提醒）
def _overdue_cond():
    return and_(Task.status.not_in(("done", "void")), Task.due < date.today())


# 状态机（§3.5.1：from 集合 -> to；代码写死，AI 无权决定）
_TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "started": (("open",), "in_progress"),
    "completed": (("in_progress",), "done"),
    "voided": (("in_progress",), "void"),
    "blocked": (("open", "in_progress"), "blocked"),
    "unblocked": (("blocked",), "in_progress"),
}
# completed/voided/blocked 事件带 note（result_note / blocked_reason）
_NOTE_ACTIONS = ("completed", "voided", "blocked")


class TMStore:
    """业务库访问门面：全部查询/写路径集中于此，路由不直接碰 SQLAlchemy。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._maker = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_env(cls) -> TMStore:
        """生产入口：连接串从 .env 的 LIUQUAN_TM_DB_URL 建（见 create_tm_engine）。"""
        return cls(create_tm_engine())

    async def dispose(self) -> None:
        await self._engine.dispose()

    # ---- 查询 ----

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        overdue_only: bool = False,
        keyword: str | None = None,
    ) -> list[Task]:
        """任务列表（筛选：状态 / 逾期 / 关键词；详设 §6.2 先做这三个）。"""
        stmt = select(Task).order_by(Task.due.asc(), Task.id.asc())
        if status is not None:
            stmt = stmt.where(Task.status == status)
        if overdue_only:
            stmt = stmt.where(_overdue_cond())
        if keyword:
            kw = f"%{keyword}%"
            stmt = stmt.where(
                or_(
                    Task.title.ilike(kw),
                    func.coalesce(Task.detail, "").ilike(kw),
                )
            )
        async with self._maker() as session:
            rows = (await session.execute(stmt)).scalars().all()
            # 派生关联（决策 17，用户复核反馈）：带出父任务标题 + 子任务数，
            # 展示层据此显示「由 t-xxx「父标题」派生」与「N 个子任务」徽章
            parent_ids = {t.derived_from for t in rows if t.derived_from}
            parent_titles: dict[int, str] = {}
            child_counts: dict[int, int] = {}
            if parent_ids:
                parents = (
                    await session.execute(
                        select(Task.id, Task.title).where(Task.id.in_(parent_ids))
                    )
                ).all()
                parent_titles = {pid: title for pid, title in parents}
                cnt_rows = (
                    await session.execute(
                        select(Task.derived_from, func.count(Task.id))
                        .where(Task.derived_from.in_(parent_ids))
                        .group_by(Task.derived_from)
                    )
                ).all()
                child_counts = {pid: int(cnt) for pid, cnt in cnt_rows}
        return [
            _TaskWithRel(
                task=t,
                parent_title=parent_titles.get(t.derived_from) if t.derived_from else None,
                child_count=child_counts.get(t.id, 0),
            )
            for t in rows
        ]

    async def count_overdue(self) -> int:
        """逾期任务数（status 非 done/void 且 due < 今天；提醒条计数）。"""
        async with self._maker() as session:
            cnt = await session.scalar(
                select(func.count()).select_from(Task).where(_overdue_cond())
            )
        return int(cnt or 0)

    async def stats(self) -> dict[str, int]:
        """统计卡：待办 / 逾期 / 待审提案 / 已完成（简单聚合，详设 §6.2）。"""
        async with self._maker() as session:
            open_cnt = await session.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.status.in_(("open", "in_progress")))
            )
            done_cnt = await session.scalar(
                select(func.count()).select_from(Task).where(Task.status == "done")
            )
            overdue_cnt = await session.scalar(
                select(func.count()).select_from(Task).where(_overdue_cond())
            )
            pending_cnt = await session.scalar(
                select(func.count())
                .select_from(TaskProposal)
                .where(TaskProposal.status == "pending")
            )
        return {
            "open": int(open_cnt or 0),
            "done": int(done_cnt or 0),
            "overdue": int(overdue_cnt or 0),
            "pending_proposals": int(pending_cnt or 0),
        }

    async def list_pending_proposals(self) -> list[TaskProposal]:
        """待审提案（status=pending，全部人工审，决策 12）。

        A22：**无依据的提案页面不可见**——转交器落库已保证 evidence 非空
        （禁幻觉三件套 ①），页面层再防御性过滤空 evidence（决策 16：审核
        面板强制展示依据，无依据不展示）。
        """
        stmt = (
            select(TaskProposal)
            .where(TaskProposal.status == "pending")
            .where(func.jsonb_array_length(TaskProposal.evidence) > 0)
            .order_by(TaskProposal.created_at.desc())
        )
        async with self._maker() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    # ---- 人工建任务（决策 16 双通道：人工通道）----

    async def create_task(
        self,
        *,
        title: str,
        detail: str | None,
        domain: str,
        role: str,
        due: date,
        actor: str,
    ) -> Task:
        """人工建任务：source_type=manual，source={"creator": 操作者角色}。"""
        title = (title or "").strip()
        if not title or len(title) > 80:
            raise TMWebError("标题必填且不超过 80 字（对齐 TaskProposal.title）")
        if role not in ROLE_VALUES:
            raise TMWebError(f"负责人必须是 {'/'.join(ROLE_VALUES)} 之一")
        if not (domain or "").strip():
            raise TMWebError("来源域（domain）必填")
        if due is None:
            raise TMWebError("截止日期（due）必填")
        async with self._maker() as session, session.begin():
            task = Task(
                title=title,
                detail=(detail or "").strip() or None,
                domain=domain.strip(),
                role=role,
                due=due,
                source_type="manual",
                source={"creator": actor},  # §3.4：人工输入，creator 即依据
                created_by=actor,
            )
            session.add(task)
            await session.flush()
            session.add(TaskEvent(task_id=task.id, event_type="created", actor=actor))
        return task

    # ---- 状态操作（§3.5.1 + §3.3 事件写入点）----

    async def transition(
        self,
        task_id: int,
        action: str,
        *,
        actor: str,
        result_note: str | None = None,
        blocked_reason: str | None = None,
    ) -> Task:
        """状态迁移：started/completed/voided/blocked/unblocked，每次写事件。

        completed/voided 必填 result_note（决策 17：不回复不能结束——页面
        校验 + DB CHECK chk_result_note 双保险）；blocked 必填
        blocked_reason（决策 11：等物料 / 等回复）。
        """
        spec = _TRANSITIONS.get(action)
        if spec is None:
            raise TMWebError(f"未知操作：{action!r}")
        allowed_from, to_status = spec
        if action in ("completed", "voided"):
            note = (result_note or "").strip()
            if not note:
                raise TMWebError(
                    "完成/作废必须填写回复（result_note）——不回复不能结束（决策 17）"
                )
            result_note = note
        if action == "blocked":
            reason = (blocked_reason or "").strip()
            if not reason:
                raise TMWebError(
                    "阻塞必须填写原因（blocked_reason：" + " / ".join(BLOCKED_REASONS) + "）"
                )
            if reason not in BLOCKED_REASONS:
                raise TMWebError(
                    "阻塞原因必须是 " + " / ".join(BLOCKED_REASONS) + " 之一"
                )
            blocked_reason = reason
        async with self._maker() as session, session.begin():
            task = await session.get(Task, task_id, with_for_update=True)
            if task is None:
                raise TMWebError(f"任务不存在：{task_display_id(task_id)}")
            if task.status not in allowed_from:
                raise TMWebError(
                    f"非法状态迁移：{task_display_id(task_id)} 当前状态"
                    f" {task.status!r}，不能执行 {action}"
                )
            from_status = task.status
            task.status = to_status
            if to_status in ("done", "void"):
                task.result_note = result_note
                task.done_at = datetime.now(timezone.utc)  # done/void 写 done_at（§3.3）
            elif to_status == "blocked":
                task.blocked_reason = blocked_reason
            elif action == "unblocked":
                task.blocked_reason = None  # chk_blocked_reason：非 blocked 必须为空
            task.updated_at = datetime.now(timezone.utc)
            session.add(
                TaskEvent(
                    task_id=task.id,
                    event_type=action,
                    from_status=from_status,
                    to_status=to_status,
                    actor=actor,
                    note=(result_note if action in ("completed", "voided") else blocked_reason)
                    if action in _NOTE_ACTIONS
                    else None,
                )
            )
        return task

    # ---- 派生（§3.5.2 决策 17：派生≠原任务结束）----

    async def derive_task(
        self,
        parent_id: int,
        *,
        title: str,
        detail: str | None,
        domain: str,
        role: str,
        due: date,
        actor: str,
    ) -> Task:
        """从原任务派生新任务：新任务 derived_from=原任务；原任务不结束。

        事件：新任务 created + 原任务 derived（note=派生出的任务 id）。
        """
        title = (title or "").strip()
        if not title or len(title) > 80:
            raise TMWebError("标题必填且不超过 80 字")
        if role not in ROLE_VALUES:
            raise TMWebError(f"负责人必须是 {'/'.join(ROLE_VALUES)} 之一")
        if due is None:
            raise TMWebError("截止日期（due）必填")
        async with self._maker() as session, session.begin():
            parent = await session.get(Task, parent_id, with_for_update=True)
            if parent is None:
                raise TMWebError(f"原任务不存在：{task_display_id(parent_id)}")
            child = Task(
                title=title,
                detail=(detail or "").strip() or None,
                domain=domain.strip() or parent.domain,
                role=role,
                due=due,
                derived_from=parent.id,  # 派生来源（§3.1：派生≠原任务结束）
                source_type="manual",
                source={"creator": actor},
                created_by=actor,
            )
            session.add(child)
            await session.flush()
            session.add(TaskEvent(task_id=child.id, event_type="created", actor=actor))
            session.add(
                TaskEvent(
                    task_id=parent.id,
                    event_type="derived",
                    actor=actor,
                    note=task_display_id(child.id),  # §3.3：note=派生出的任务 id
                )
            )
            parent.updated_at = datetime.now(timezone.utc)
        return child

    # ---- 编辑（§3.5.5 决策 17 第 4 条：updated 事件带 from/to 快照）----

    async def edit_task(
        self,
        task_id: int,
        *,
        title: str,
        role: str,
        due: date,
        domain: str,
        detail: str | None,
        actor: str,
    ) -> Task:
        """改 title/role/due/domain/detail，每次编辑记 updated 事件（快照）。"""
        title = (title or "").strip()
        if not title or len(title) > 80:
            raise TMWebError("标题必填且不超过 80 字")
        if role not in ROLE_VALUES:
            raise TMWebError(f"负责人必须是 {'/'.join(ROLE_VALUES)} 之一")
        if not (domain or "").strip():
            raise TMWebError("来源域（domain）必填")
        new_detail = (detail or "").strip() or None
        async with self._maker() as session, session.begin():
            task = await session.get(Task, task_id, with_for_update=True)
            if task is None:
                raise TMWebError(f"任务不存在：{task_display_id(task_id)}")
            changes: dict[str, dict[str, object]] = {}

            def _chg(field: str, new: object) -> None:
                old = getattr(task, field)
                if old != new:
                    changes[field] = {"from": old, "to": new}

            _chg("title", title)
            _chg("role", role)
            _chg("due", due)
            _chg("domain", domain)
            _chg("detail", new_detail)
            if not changes:
                return task  # 无实际变更不记事件
            task.title = title
            task.role = role
            task.due = due
            task.domain = domain
            task.detail = new_detail
            task.updated_at = datetime.now(timezone.utc)
            session.add(
                TaskEvent(
                    task_id=task.id,
                    event_type="updated",
                    actor=actor,
                    note=json.dumps(changes, ensure_ascii=False, default=str),
                )
            )
        return task

    # ---- 重开（§3.5.1：done/void 可重开回 open，updated 事件留痕）----

    async def reopen_task(self, task_id: int, *, actor: str) -> Task:
        """终态（done/void）重开回 open：清 result_note/done_at + updated 事件。"""
        async with self._maker() as session, session.begin():
            task = await session.get(Task, task_id, with_for_update=True)
            if task is None:
                raise TMWebError(f"任务不存在：{task_display_id(task_id)}")
            if task.status not in ("done", "void"):
                raise TMWebError(
                    f"只有已完成/已作废任务可重开（当前状态 {task.status}）"
                )
            changes = {
                "status": {"from": task.status, "to": "open"},
                "result_note": {"from": task.result_note, "to": None},
                "done_at": {"from": task.done_at, "to": None},
            }
            task.status = "open"
            task.result_note = None  # chk_result_note：非 done/void 必须为空
            task.done_at = None
            task.updated_at = datetime.now(timezone.utc)
            session.add(
                TaskEvent(
                    task_id=task.id,
                    event_type="updated",
                    actor=actor,
                    note=json.dumps(changes, ensure_ascii=False, default=str),
                )
            )
        return task

    # ---- 提案审核（§3.2 审核流：全部人工批/驳，决策 12）----

    async def approve_proposal(self, proposal_id: int, *, actor: str) -> Task:
        """批准提案（单事务）：提案 approved + 建 tm.task + 提案 task_id 回填
        + created/approved 两条事件（详设 §3.2 审核流）。"""
        async with self._maker() as session, session.begin():
            prop = await session.get(TaskProposal, proposal_id, with_for_update=True)
            if prop is None:
                raise TMWebError(f"提案不存在：{proposal_display_id(proposal_id)}")
            if prop.status != "pending":
                raise TMWebError(
                    f"提案 {proposal_display_id(proposal_id)} 已审核"
                    f"（状态 {prop.status}），不能重复批准"
                )
            source = dict(prop.source or {})
            source["proposal_id"] = proposal_display_id(prop.id)  # §3.4 批准时回填
            due = date.today() + timedelta(days=prop.suggested_due_days or 0)
            task = Task(
                title=prop.title,
                detail=prop.detail,
                domain=prop.domain,  # AI 任务继承提案 domain（决策 16）
                role=prop.suggested_role,
                due=due,  # AI 建议截止天数独立建议，批准时落库（决策 11）
                source_type="ai",
                source=source,
                created_by=actor,
            )
            session.add(task)
            await session.flush()
            prop.status = "approved"
            prop.reviewed_by = actor
            prop.reviewed_at = datetime.now(timezone.utc)
            prop.task_id = task.id  # 提案关联生成的任务（业务库内 FK，合法）
            session.add(TaskEvent(task_id=task.id, event_type="created", actor=actor))
            session.add(TaskEvent(task_id=task.id, event_type="approved", actor=actor))
        return task

    async def reject_proposal(
        self, proposal_id: int, *, actor: str, reason: str | None = None
    ) -> None:
        """驳回提案：rejected + reject_reason（不建任务）。"""
        async with self._maker() as session, session.begin():
            prop = await session.get(TaskProposal, proposal_id, with_for_update=True)
            if prop is None:
                raise TMWebError(f"提案不存在：{proposal_display_id(proposal_id)}")
            if prop.status != "pending":
                raise TMWebError(
                    f"提案 {proposal_display_id(proposal_id)} 已审核，不能重复驳回"
                )
            prop.status = "rejected"
            prop.reviewed_by = actor
            prop.reviewed_at = datetime.now(timezone.utc)
            prop.reject_reason = (reason or "").strip() or None


    # ---- 任务「下一步」区（决策 27/28：AI 建议 + 人同意/改派/忽略 + 流转留痕）----

    async def next_action(
        self,
        task_id: int,
        *,
        action: str,
        target_role: str | None = None,
        tags: list[str] | None = None,
        note: str | None = None,
        actor: str = "运营",
    ) -> Task:
        """执行「下一步」动作（v0.3 可执行：assign 改派角色 / tag 改标签 / note 记备注 /
        block 挂起 / ignore 忽略建议）；流转留痕 transferred 事件（决策 27）。

        - assign：task.role = target_role（三角色）
        - tag：task.tags = tags（覆盖）
        - note：task.detail 追加备注 + transferred 事件 note
        - block：状态转 blocked（等物料/等回复，必填 blocked_reason 取 note）
        - ignore：清 ai_suggestion（建议被忽略，不落 transferred）
        transfer 到未接入域（erp/seo）由路由层提示不可执行并记录意图（决策 28）。
        """
        async with AsyncSession(self._engine) as session, session.begin():
            task = await session.get(Task, task_id)
            if task is None:
                raise TMWebError("任务不存在")
            # SQLAlchemy ORM 非 pydantic：快照关键字段（updated 事件 from/to 同款思路）
            prev = {
                "status": task.status,
                "role": task.role,
                "tags": list(task.tags or []),
                "detail": task.detail,
            }
            if action == "assign":
                if target_role not in ("运营", "采购", "管理员"):
                    raise TMWebError(f"非法角色：{target_role!r}")
                task.role = target_role
            elif action == "tag":
                task.tags = [t for t in (tags or []) if t.strip()][:20]
            elif action == "note":
                addition = f"\n[下一步备注] {note}".strip() if note else ""
                task.detail = (task.detail or "") + addition
            elif action == "block":
                reason = (note or "等物料").strip()
                if reason not in ("等物料", "等回复"):
                    raise TMWebError("blocked_reason 只能填 等物料 / 等回复")
                task.status = "blocked"
                task.blocked_reason = reason
            elif action == "ignore":
                task.ai_suggestion = None
            else:
                raise TMWebError(f"非法下一步动作：{action!r}")
            if action != "ignore":
                # 流转留痕（决策 27）：transferred 事件，detail 带 action/target
                session.add(
                    TaskEvent(
                        task_id=task.id,
                        event_type="transferred",
                        actor=actor,
                        note=f"下一步：{action}"
                        + (f" -> {target_role}" if target_role else ""),
                        detail={
                            "action": action,
                            "target_role": target_role,
                            "tags": tags,
                            "note": note,
                            "prev": prev,
                        },
                    )
                )
                if action == "assign":
                    task.ai_suggestion = None  # 执行建议后清空
            else:
                task.ai_suggestion = None
            await session.flush()
            return task

    async def record_disagreement(
        self, task_id: int, *, ai_suggestion: dict, human_chose: str, actor: str = "运营"
    ) -> None:
        """分歧留痕（决策 27）：人选择与 AI 建议不同 -> disagreed 事件。"""
        async with AsyncSession(self._engine) as session, session.begin():
            task = await session.get(Task, task_id)
            if task is None:
                raise TMWebError("任务不存在")
            session.add(
                TaskEvent(
                    task_id=task.id,
                    event_type="disagreed",
                    actor=actor,
                    note=f"人选择：{human_chose}",
                    detail={"ai_suggestion": ai_suggestion, "human_chose": human_chose},
                )
            )

__all__ = [
    "BLOCKED_REASONS",
    "ROLE_VALUES",
    "TMStore",
    "TMWebError",
    "create_tm_engine",
    "proposal_display_id",
    "task_display_id",
]
