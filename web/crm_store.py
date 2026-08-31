"""web/crm_store.py：CRM 域 DAO（v0.3 T6，详设 §7/§8）。

web 侧独立 DAO：只依赖 models/crm.py + models/tm.py 共享 ORM + SQLAlchemy
async（R22 唯一通用语言），零 engine import（lint P3-2 执法，双向零代码耦合
R24）。连接串经 dotenv_values 读 .env 的 LIUQUAN_TM_DB_URL（P2 合法来源）。

职责（详设 §7.1/§8）：
- 客户列表（状态筛选 tab / 全文搜索 / 分页）/ 新建（重名两层拦截）/ 删除（级联）
- 客户详情：消息时间线 + 最新快照 + 任务卡（tm.task domain=crm 且
  source.customer_id=X 的过滤视图，决策 24）
- 粘贴落库：从引擎链结果（译文/快照）落 crm.message / crm.snapshot（候选由
  引擎 crm_candidate 消费者经 /api/biz 写接口落库，决策 26）
- 回复台：回复生成结果不落库（可重试）；「已发送」归档 seller 消息 + 状态转 replied
- 候选确认事务（决策 19/25/27）：candidate confirmed + tm.task 创建 +
  created/approved/suggested 事件 + confirmed_task_id 回填（+ 飞书由路由层调）
- 候选忽略 / 翻译工具归入客户
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from models.crm import Customer, Message, Snapshot, TodoCandidate
from models.tm import Task, TaskEvent
from web.tm_store import create_tm_engine

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOTENV_PATH = _REPO_ROOT / ".env"
_TM_DB_URL_ENV = "LIUQUAN_TM_DB_URL"

# 跟进状态/阶段/来源标签（展示用；与 EOMS 语义一致，决策 24 任务只按 domain）
STATUS_META = {
    "waiting_reply": "待回复",
    "replied": "已回复",
    "closed_deal": "已成交",
    "on_hold": "搁置",
    "archived": "归档",
}
STAGE_META = {"pre_sale": "售前", "in_sale": "售中", "after_sale": "售后"}
DIRECTION_META = {"buyer": "买家", "seller": "我"}

# 逾期判定默认阈值（详设 §7.3：读 settings crm.follow_up_days，表无回退默认）
_DEFAULT_FOLLOW_UP_DAYS = 5


class CrmWebError(Exception):
    """业务拒绝（路由捕获 -> 页面 err 提示）。"""


class CRMStore:
    """CRM 域数据访问（构造注入 engine + settings_store；生产经 from_env）。"""

    def __init__(
        self,
        engine: AsyncEngine,
        settings_store: Any | None = None,
    ) -> None:
        self._engine = engine
        self._settings_store = settings_store

    @classmethod
    def from_env(cls) -> "CRMStore":
        # 延迟 import 避免循环：SettingsStore 同包
        from web.settings_store import SettingsStore

        url = (dotenv_values(_DOTENV_PATH) or {}).get(_TM_DB_URL_ENV)
        engine = create_tm_engine(url)
        settings_store = SettingsStore(engine)
        return cls(engine, settings_store=settings_store)

    async def dispose(self) -> None:
        await self._engine.dispose()

    # ---- 客户列表 ----

    async def _get_follow_up_days(self) -> int:
        """从 settings_store 读逾期阈值；未注入或读取失败回退默认。"""
        if self._settings_store is None:
            return _DEFAULT_FOLLOW_UP_DAYS
        try:
            val = await self._settings_store.get("crm.follow_up_days", _DEFAULT_FOLLOW_UP_DAYS)
            return int(val) if val is not None else _DEFAULT_FOLLOW_UP_DAYS
        except Exception:
            return _DEFAULT_FOLLOW_UP_DAYS

    async def list_customers(
        self,
        *,
        q: str = "",
        status: str = "",
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[dict], int]:
        """客户列表（状态筛选 tab + 全文搜索 + 分页）。

        status: ""=进行中(非归档) / 五态 / all=全部。搜索：昵称/备注/消息
        原文译文/快照 summary+current_need。
        """
        follow_up_days = await self._get_follow_up_days()
        # page_size 默认值从 settings 读（表无回退 20）
        effective_page_size = 20
        if page_size is not None:
            effective_page_size = page_size
        else:
            try:
                if self._settings_store is not None:
                    val = await self._settings_store.get("crm.page_size", 20)
                    effective_page_size = int(val) if val is not None else 20
            except Exception:
                effective_page_size = 20
        async with AsyncSession(self._engine) as session:
            query = select(Customer)
            if status == "all":
                pass
            elif status == "":
                query = query.where(Customer.follow_up_status != "archived")
            elif status in STATUS_META:
                query = query.where(Customer.follow_up_status == status)
            keyword = q.strip()
            if keyword:
                like = f"%{keyword}%"
                msg_ids = select(Message.customer_id).where(
                    or_(
                        Message.source_text.ilike(like),
                        Message.translated_text.ilike(like),
                    )
                )
                latest_snap = (
                    select(func.max(Snapshot.id))
                    .group_by(Snapshot.customer_id)
                    .subquery()
                )
                snap_hits = select(Snapshot.customer_id).where(
                    Snapshot.id.in_(select(latest_snap)),
                    or_(
                        Snapshot.summary.ilike(like),
                        Snapshot.current_need.ilike(like),
                    ),
                )
                query = query.where(
                    or_(
                        Customer.nickname.ilike(like),
                        Customer.remark.ilike(like),
                        Customer.id.in_(msg_ids),
                        Customer.id.in_(snap_hits),
                    )
                )
            total = len((await session.execute(query)).scalars().all())
            rows = (
                (
                    await session.execute(
                        query.order_by(Customer.updated_at.desc(), Customer.id.desc())
                        .offset((page - 1) * effective_page_size)
                        .limit(effective_page_size)
                    )
                )
                .scalars()
                .all()
            )
        return [self._customer_view(c, follow_up_days) for c in rows], total

    @staticmethod
    def _customer_view(c: Customer, follow_up_days: int = _DEFAULT_FOLLOW_UP_DAYS) -> dict:
        days = None
        latest = c.last_contacted_at or c.updated_at
        if latest is not None:
            days = (datetime.now(timezone.utc).date() - latest.date()).days
        overdue = (
            days is not None
            and days > follow_up_days
            and c.follow_up_status != "archived"
        )
        return {
            "id": c.id,
            "nickname": c.nickname,
            "source_shop": c.source_shop or "",
            "remark": c.remark or "",
            "status": c.follow_up_status,
            "status_label": STATUS_META.get(c.follow_up_status, c.follow_up_status),
            "stage_label": STAGE_META.get(c.trade_stage, c.trade_stage),
            "days_since": days,
            "overdue": overdue,
            "latest_summary": c.latest_summary or "",
        }

    # ---- 新建 / 重名 / 删除 ----

    async def find_same_name(self, nickname: str) -> list[dict]:
        name = nickname.strip()
        if not name:
            return []
        follow_up_days = await self._get_follow_up_days()
        async with AsyncSession(self._engine) as session:
            rows = (
                (
                    await session.execute(
                        select(Customer).where(
                            func.lower(Customer.nickname) == name.lower()
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [self._customer_view(c, follow_up_days) for c in rows]

    async def create_customer(
        self, *, nickname: str, source_shop: str = "", remark: str = "", force: bool = False
    ) -> int:
        nickname = nickname.strip()
        if not nickname:
            raise CrmWebError("昵称必填")
        if not force and await self.find_same_name(nickname):
            raise CrmWebError("已存在同名客户（忽略大小写），请确认是否重复建档")
        async with AsyncSession(self._engine) as session, session.begin():
            row = Customer(
                nickname=nickname,
                source_shop=source_shop.strip(),
                remark=remark.strip(),
                follow_up_status="waiting_reply",
            )
            session.add(row)
            await session.flush()
            return row.id

    async def delete_customer(self, customer_id: int) -> None:
        """级联删除（决策 22）：crm 四表 + 关联 tm.task（含事件，FK CASCADE）。"""
        async with AsyncSession(self._engine) as session, session.begin():
            customer = await session.get(Customer, customer_id)
            if customer is None:
                raise CrmWebError("客户不存在")
            await session.execute(
                text(
                    "DELETE FROM tm.task WHERE domain = 'crm' "
                    "AND source->>'customer_id' = :cid"
                ),
                {"cid": str(customer_id)},
            )
            await session.execute(
                text("DELETE FROM crm.todo_candidate WHERE customer_id = :cid"),
                {"cid": customer_id},
            )
            await session.execute(
                text("DELETE FROM crm.snapshot WHERE customer_id = :cid"),
                {"cid": customer_id},
            )
            await session.execute(
                text("DELETE FROM crm.message WHERE customer_id = :cid"),
                {"cid": customer_id},
            )
            await session.delete(customer)

    # ---- 详情 ----

    async def detail(self, customer_id: int) -> dict | None:
        """客户详情：档案头 + 消息时间线 + 最新快照 + 任务卡 + 待确认候选。"""
        follow_up_days = await self._get_follow_up_days()
        async with AsyncSession(self._engine) as session:
            customer = await session.get(Customer, customer_id)
            if customer is None:
                return None
            messages = (
                (
                    await session.execute(
                        select(Message)
                        .where(Message.customer_id == customer_id)
                        .order_by(Message.created_at.asc(), Message.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            snap = (
                (
                    await session.execute(
                        select(Snapshot)
                        .where(Snapshot.customer_id == customer_id)
                        .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
                        .limit(1)
                    )
                )
                .scalar_one_or_none()
            )
            tasks = (
                (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.domain == "crm",
                            Task.source["customer_id"].astext == str(customer_id),
                        )
                        .order_by(Task.created_at.desc(), Task.id.desc())
                    )
                )
                .scalars()
                .all()
            )
            candidates = (
                (
                    await session.execute(
                        select(TodoCandidate)
                        .where(
                            TodoCandidate.customer_id == customer_id,
                            TodoCandidate.status == "pending",
                        )
                        .order_by(TodoCandidate.created_at.asc(), TodoCandidate.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            # v0.5 批 5：查询对话图片
            from models.crm import MessageImage
            message_images = (
                (
                    await session.execute(
                        select(MessageImage)
                        .where(MessageImage.message_id.in_(
                            select(Message.id).where(Message.customer_id == customer_id)
                        ))
                        .order_by(MessageImage.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
        return {
            "customer": self._customer_view(customer, follow_up_days),
            "messages": [
                {
                    "id": m.id,
                    "direction": m.direction,
                    "direction_label": DIRECTION_META.get(m.direction, m.direction),
                    "source_text": m.source_text,
                    "translated_text": m.translated_text,
                    "language": m.language,
                    "created_at": m.created_at,
                }
                for m in messages
            ],
            "snapshot": self._snapshot_view(snap) if snap else None,
            "tasks": [self._task_view(t) for t in tasks],
            "candidates": [
                {
                    "id": c.id,
                    "content": c.content,
                    "reason": c.reason,
                    "suggested_tags": list(c.suggested_tags or []),
                    "evidence": list(c.evidence or []),
                }
                for c in candidates
            ],
            "message_images": [
                {
                    "id": img.id,
                    "message_id": img.message_id,
                    "url": img.url,
                    "local_path": img.local_path,
                    "status": img.status,
                    "width": img.width,
                    "height": img.height,
                    "ocr_text": img.ocr_text,
                    "created_at": img.created_at,
                }
                for img in message_images
            ],
        }

    @staticmethod
    def _snapshot_view(s: Snapshot) -> dict:
        return {
            "current_need": s.current_need,
            "need_history": list(s.need_history or []),
            "sentiment": s.sentiment,
            "todos": list(s.todos or []),
            "summary": s.summary,
            "created_at": s.created_at,
        }

    @staticmethod
    def _task_view(t: Task) -> dict:
        return {
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "role": t.role,
            "due": t.due,
            "tags": list(t.tags or []),
            "ai_suggestion": t.ai_suggestion,
        }

    # ---- 粘贴落库（从引擎链结果） ----

    async def apply_chain_result(
        self, customer_id: int, chain_output: dict
    ) -> list[int]:
        """从引擎链结果落库：译文 -> message（append-only）、快照 -> snapshot。

        chain_output：GET /api/engine/tasks/{id} 的链末步 output
        （ChatTranscriptResult + CustomerSnapshotResult 形态）。
        """
        new_ids: list[int] = []
        async with AsyncSession(self._engine) as session, session.begin():
            for item in chain_output.get("translations") or []:
                row = Message(
                    customer_id=customer_id,
                    source_text=str(item.get("source_text", "")),
                    translated_text=str(item.get("translated_text", "")),
                    direction=str(item.get("direction", "buyer")),
                    language=str(item.get("language", "")),
                    message_time=None,  # 决策 23：粘贴场景不标时间
                )
                session.add(row)
                await session.flush()
                new_ids.append(row.id)
            snapshot = chain_output.get("snapshot") or {}
            session.add(
                Snapshot(
                    customer_id=customer_id,
                    current_need=str(snapshot.get("current_need", "")),
                    need_history=list(snapshot.get("need_history") or []),
                    sentiment=str(snapshot.get("sentiment", "")),
                    todos=list(snapshot.get("todos") or []),
                    summary=str(snapshot.get("summary", "")),
                )
            )
            customer = await session.get(Customer, customer_id)
            if customer is not None:
                customer.latest_summary = str(snapshot.get("summary", ""))
                customer.last_contacted_at = datetime.now(timezone.utc)
        return new_ids

    # ---- 回复台 ----

    async def reply_send(
        self, customer_id: int, reply_en: str, reply_zh: str = ""
    ) -> None:
        """「已发送，归档」：落 seller 消息 + 状态转 replied + 刷新跟进时间。"""
        reply_en = reply_en.strip()
        if not reply_en:
            raise CrmWebError("回复内容为空，无法归档")
        async with AsyncSession(self._engine) as session, session.begin():
            customer = await session.get(Customer, customer_id)
            if customer is None:
                raise CrmWebError("客户不存在")
            session.add(
                Message(
                    customer_id=customer_id,
                    source_text=reply_en,
                    translated_text=reply_zh,
                    direction="seller",
                    language="en",
                )
            )
            customer.follow_up_status = "replied"
            customer.last_contacted_at = datetime.now(timezone.utc)

    # ---- 候选确认事务（决策 19/25/27）----

    async def confirm_candidates(
        self, customer_id: int, candidate_ids: list[int], *, actor: str = "运营"
    ) -> list[int]:
        """确认候选 -> 单事务建 tm.task（domain=crm, source_type=ai）。

        1) 校验：候选属于该客户且 status=pending；
        2) candidate confirmed + confirmed_task_id 回填；
        3) 建 tm.task（title=content, detail=reason, tags=suggested_tags,
           ai_suggestion=suggested_next, source 带 customer_id/candidate_id）；
        4) task_event created + approved（+ suggested 若有建议）；
        5) 返回新任务 id 列表（路由层调飞书）。
        """
        created_task_ids: list[int] = []
        async with AsyncSession(self._engine) as session, session.begin():
            customer = await session.get(Customer, customer_id)
            if customer is None:
                raise CrmWebError("客户不存在")
            for cid in candidate_ids:
                candidate = await session.get(TodoCandidate, cid)
                if candidate is None or candidate.customer_id != customer_id:
                    raise CrmWebError(f"候选 {cid} 不存在或不属于该客户")
                if candidate.status != "pending":
                    raise CrmWebError(f"候选 {cid} 已处理（重复确认拒）")
                if not candidate.evidence:
                    raise CrmWebError(f"候选 {cid} 无依据，不可确认（决策 16）")
                now = datetime.now(timezone.utc)
                task = Task(
                    title=candidate.content[:80],
                    detail=candidate.reason or "",
                    domain="crm",
                    role="运营",  # crm 域默认角色（决策 11：AI 提案按域带默认角色可改）
                    due=date.today(),
                    source_type="ai",
                    source={
                        "customer_id": customer_id,
                        "candidate_id": candidate.id,
                        "engine_task_id": candidate.engine_task_id,
                        "chain_id": "crm_chat_chain",
                        "evidence": candidate.evidence,
                    },
                    tags=list(candidate.suggested_tags or []),
                    ai_suggestion=(
                        candidate.suggested_next
                        if hasattr(candidate, "suggested_next")
                        else None
                    ),
                    created_by=actor,
                )
                session.add(task)
                await session.flush()
                session.add_all(
                    [
                        TaskEvent(task_id=task.id, event_type="created", actor=actor),
                        TaskEvent(task_id=task.id, event_type="approved", actor=actor),
                    ]
                )
                if task.ai_suggestion:
                    session.add(
                        TaskEvent(
                            task_id=task.id,
                            event_type="suggested",
                            note="AI 建议下一步",
                            detail=task.ai_suggestion,
                        )
                    )
                candidate.status = "confirmed"
                candidate.confirmed_at = now
                candidate.confirmed_task_id = task.id
                created_task_ids.append(task.id)
        return created_task_ids

    async def dismiss_candidates(
        self, customer_id: int, candidate_ids: list[int]
    ) -> None:
        async with AsyncSession(self._engine) as session, session.begin():
            for cid in candidate_ids:
                candidate = await session.get(TodoCandidate, cid)
                if candidate is None or candidate.customer_id != customer_id:
                    continue
                if candidate.status == "pending":
                    candidate.status = "dismissed"
                    candidate.dismissed_at = datetime.now(timezone.utc)

    # ---- 翻译工具归入客户 ----

    async def archive_translation(
        self,
        *,
        customer_id: int,
        source_text: str,
        translated_text: str,
        direction: str = "buyer",
        language: str = "",
    ) -> None:
        """翻译工具「归入某客户」（决策 23）：译文作为消息归档进客户时间线。"""
        if not source_text.strip():
            raise CrmWebError("原文为空，无法归档")
        async with AsyncSession(self._engine) as session, session.begin():
            customer = await session.get(Customer, customer_id)
            if customer is None:
                raise CrmWebError("客户不存在")
            session.add(
                Message(
                    customer_id=customer_id,
                    source_text=source_text,
                    translated_text=translated_text,
                    direction=direction if direction in ("buyer", "seller") else "buyer",
                    language=language,
                )
            )
            customer.last_contacted_at = datetime.now(timezone.utc)


__all__ = ["CRMStore", "CrmWebError"]
