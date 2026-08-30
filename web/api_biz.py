"""web/api_biz.py：业务读写接口（详设-v0.3 §6，决策 26 业务写入接口化）。

- 引擎侧消费者（tm_proposal 转交器 / crm_candidate 候选消费者）与 Context
  provider（engine/providers/）经 HTTP 调用本模块——引擎进程零业务库连接串
  （凭据更少、校验/审计集中、将来 ERP 重构换库引擎零改动）。
- 接口**不区分调用方是 AI 还是人**，只做数据正确性校验（Pydantic 类型 +
  业务规则），正确即入库（用户拍板原则）。
- 鉴权：X-Biz-Token（.env 的 LIUQUAN_BIZ_API_TOKEN，P2 规则 4 合法来源）；
  未配置/缺失/不匹配 -> 401 fail-closed。
- 幂等：写接口按 source.engine_task_id + source.worker_id 幂等（同链重跑
  重复交付跳过，决策 17）；同事件防重（同 ref_id + action_id 已有待审提案/
  未完成任务跳过，决策 17 语义不变，查询上移本层——接口化后引擎无法查业务库）。
- 测试友好：create_biz_router(engine=..., token=...) 构造注入（R12）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from dotenv import dotenv_values
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from models.contract.task import EvidenceRef, SourceTrace, TaskProposal
from models.crm import Customer, Message, Snapshot, TodoCandidate
from models.tm import Task, TaskEvent, TaskProposal as TaskProposalORM
from models.workers import (
    ChatContextData,
    CustomerBrief,
    EventBrief,
    MessageBrief,
    SnapshotBrief,
    TaskContextData,
)
from web.tm_store import create_tm_engine

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOTENV_PATH = _REPO_ROOT / ".env"
_BIZ_TOKEN_ENV = "LIUQUAN_BIZ_API_" + "TOKEN"


class TaskProposalWrite(TaskProposal):
    """TM 提案写接口请求体：四契约 TaskProposal + risk（代码规则标注，引擎侧查
    Action 声明后随请求传入；接口只校验正确性，risk 值域校验见下）。"""

    risk: Literal["read", "suggest", "write"]


class TodoCandidateWrite(BaseModel):
    """CRM 候选写接口请求体（决策 19：todo_generate 产出候选落 crm.todo_candidate）。"""

    customer_id: int
    content: str = Field(min_length=1, max_length=200)
    reason: str = ""
    suggested_tags: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=20)
    source: SourceTrace


def create_biz_router(
    *,
    engine: AsyncEngine | None = None,
    token: str | None = None,
) -> APIRouter:
    """业务读写接口工厂（R12 构造注入：测试注入嵌入式 PG engine + token）。"""
    router = APIRouter(prefix="/api/biz")
    _engine_holder: dict[str, AsyncEngine | None] = {"engine": engine}
    _token_holder: dict[str, str | None] = {"token": token}

    def _resolve_engine() -> AsyncEngine:
        eng = _engine_holder["engine"]
        if eng is None:
            eng = create_tm_engine()
            _engine_holder["engine"] = eng
        return eng

    def _resolve_token() -> str | None:
        tok = _token_holder["token"]
        if tok is None:
            tok = (dotenv_values(_DOTENV_PATH) or {}).get(_BIZ_TOKEN_ENV) or None
            _token_holder["token"] = tok
        return tok

    async def _check_token(x_biz_token: str | None = Header(default=None)) -> None:
        expected = _resolve_token()
        if not expected or x_biz_token != expected:
            raise HTTPException(status_code=401, detail="biz token 缺失或不匹配")

    # ---- 写接口：TM 提案（转交器落 tm.task_proposal）----

    @router.post("/tm/proposals", dependencies=[Depends(_check_token)])
    async def create_proposal(payload: TaskProposalWrite) -> dict:
        if not payload.evidence:
            raise HTTPException(status_code=422, detail="evidence 为空：无依据不出建议")
        maker = async_sessionmaker(_resolve_engine(), expire_on_commit=False)
        async with maker() as session, session.begin():
            # 幂等：同链同工序重复交付跳过（决策 17，v0.2 转交器同语义）
            dup = await session.execute(
                text(
                    "SELECT id FROM tm.task_proposal "
                    "WHERE source->>'engine_task_id' = :eid "
                    "AND source->>'worker_id' = :wid LIMIT 1"
                ),
                {
                    "eid": payload.source.engine_task_id,
                    "wid": payload.source.worker_id,
                },
            )
            existing = dup.scalar_one_or_none()
            if existing is not None:
                return {"ok": True, "id": existing, "skipped": True}
            # 同事件防重（决策 17）：同 ref_id + action_id 已有待审提案/未完成任务
            for ref_id in {ev.ref_id for ev in payload.evidence}:
                if await _proposal_pending(session, payload.action_id, ref_id):
                    return {"ok": True, "id": None, "skipped": True}
                if await _task_unfinished(session, payload.action_id, ref_id):
                    return {"ok": True, "id": None, "skipped": True}
            row = TaskProposalORM(
                title=payload.title,
                detail=payload.detail,
                domain=payload.domain,
                action_id=payload.action_id,
                risk=payload.risk,
                suggested_role=payload.suggested_role,
                suggested_due_days=payload.suggested_due_days,
                evidence=[ev.model_dump(mode="json") for ev in payload.evidence],
                source=payload.source.model_dump(mode="json"),
                status="pending",
            )
            session.add(row)
            await session.flush()
            return {"ok": True, "id": row.id, "skipped": False}

    # ---- 写接口：CRM 候选（候选消费者落 crm.todo_candidate）----

    @router.post("/crm/candidates", dependencies=[Depends(_check_token)])
    async def create_candidate(payload: TodoCandidateWrite) -> dict:
        maker = async_sessionmaker(_resolve_engine(), expire_on_commit=False)
        async with maker() as session, session.begin():
            customer = await session.get(Customer, payload.customer_id)
            if customer is None:
                raise HTTPException(status_code=404, detail="customer 不存在")
            # 幂等：同引擎任务产出的同内容候选重复交付跳过（候选是一链多条，
            # 幂等键 = engine_task_id(数值) + content，决策 17 同链重跑不重复）
            eid_num = _engine_task_num(payload.source.engine_task_id)
            dup = await session.execute(
                text(
                    "SELECT id FROM crm.todo_candidate "
                    "WHERE engine_task_id = :eid AND content = :content LIMIT 1"
                ),
                {"eid": eid_num, "content": payload.content},
            )
            existing = dup.scalar_one_or_none()
            if existing is not None:
                return {"ok": True, "id": existing, "skipped": True}
            row = TodoCandidate(
                customer_id=payload.customer_id,
                content=payload.content,
                reason=payload.reason,
                suggested_tags=payload.suggested_tags,
                evidence=[ev.model_dump(mode="json") for ev in payload.evidence],
                status="pending",
                engine_task_id=eid_num,
            )
            session.add(row)
            await session.flush()
            return {"ok": True, "id": row.id, "skipped": False}

    # ---- 读接口：crm 上下文（引擎侧 provider 白名单与 prompt 数据来源）----

    @router.get("/crm/context/{customer_id}", dependencies=[Depends(_check_token)])
    async def crm_context(customer_id: int) -> ChatContextData:
        maker = async_sessionmaker(_resolve_engine(), expire_on_commit=False)
        async with maker() as session:
            customer = await session.get(Customer, customer_id)
            if customer is None:
                raise HTTPException(status_code=404, detail="customer 不存在")
            rows = (
                await session.execute(
                    select(Message)
                    .where(Message.customer_id == customer_id)
                    .order_by(Message.created_at.asc(), Message.id.asc())
                    .limit(30)
                )
            ).scalars().all()
            snap_row = (
                await session.execute(
                    select(Snapshot)
                    .where(Snapshot.customer_id == customer_id)
                    .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            todo_rows = (
                await session.execute(
                    select(Task.title).where(
                        Task.domain == "crm",
                        Task.status.in_(["open", "in_progress", "blocked"]),
                        Task.source["customer_id"].astext == str(customer_id),
                    )
                )
            ).scalars().all()
            cand_rows = (
                await session.execute(
                    select(TodoCandidate.content).where(
                        TodoCandidate.customer_id == customer_id,
                        TodoCandidate.status == "pending",
                    )
                )
            ).scalars().all()
            return ChatContextData(
                customer=CustomerBrief(
                    id=customer.id,
                    nickname=customer.nickname,
                    latest_summary=customer.latest_summary,
                ),
                messages=[
                    MessageBrief(
                        id=m.id,
                        source_text=m.source_text,
                        translated_text=m.translated_text,
                        direction=m.direction,
                    )
                    for m in rows
                ],
                snapshot=(
                    SnapshotBrief(
                        id=snap_row.id,
                        current_need=snap_row.current_need,
                        need_history=list(snap_row.need_history or []),
                        sentiment=snap_row.sentiment,
                        todos=list(snap_row.todos or []),
                        summary=snap_row.summary,
                    )
                    if snap_row is not None
                    else None
                ),
                existing_open_todos=[*todo_rows, *cand_rows],
            )

    # ---- 读接口：tm 任务上下文（tm_intent 链 provider 数据来源）----

    @router.get("/tm/task-context/{task_id}", dependencies=[Depends(_check_token)])
    async def task_context(task_id: int) -> TaskContextData:
        maker = async_sessionmaker(_resolve_engine(), expire_on_commit=False)
        async with maker() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task 不存在")
            events = (
                (
                    await session.execute(
                        select(TaskEvent)
                        .where(TaskEvent.task_id == task_id)
                        .order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc())
                        .limit(10)
                    )
                )
                .scalars()
                .all()
            )
            return TaskContextData(
                task_id=task.id,
                title=task.title,
                domain=task.domain,
                status=task.status,
                tags=list(task.tags or []),
                recent_events=[
                    EventBrief(
                        event_type=e.event_type,
                        note=e.note,
                        created_at=e.created_at.isoformat() if e.created_at else None,
                    )
                    for e in reversed(events)
                ],
            )

    return router


async def _proposal_pending(session, action_id: str, ref_id: str) -> bool:
    """待审提案（status=pending）同 ref_id + action_id 已存在（决策 17 防重）。"""
    row = await session.execute(
        text(
            "SELECT id FROM tm.task_proposal "
            "WHERE status = 'pending' AND action_id = :aid "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(evidence) e "
            "             WHERE e->>'ref_id' = :rid) LIMIT 1"
        ),
        {"aid": action_id, "rid": ref_id},
    )
    return row.scalar_one_or_none() is not None


async def _task_unfinished(session, action_id: str, ref_id: str) -> bool:
    """未完成任务（status 非 done/void）经批准提案带同 ref_id + action_id。"""
    row = await session.execute(
        text(
            "SELECT t.id FROM tm.task t "
            "JOIN tm.task_proposal tp ON tp.task_id = t.id "
            "WHERE t.status NOT IN ('done', 'void') AND tp.action_id = :aid "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(tp.evidence) e "
            "             WHERE e->>'ref_id' = :rid) LIMIT 1"
        ),
        {"aid": action_id, "rid": ref_id},
    )
    return row.scalar_one_or_none() is not None


def _engine_task_num(engine_task_id: str) -> int | None:
    """展示形 e-000123 -> 数值 123（crm.todo_candidate.engine_task_id BIGINT 列）。"""
    if engine_task_id.startswith("e-") and engine_task_id[2:].isdigit():
        return int(engine_task_id[2:])
    return None
