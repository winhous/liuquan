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
from datetime import date, datetime, timezone

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from models.contract.task import EvidenceRef, SourceTrace, TaskProposal
from models.crm import Customer, Message, Snapshot, TodoCandidate
from models.tm import Task, TaskEvent, TaskProposal as TaskProposalORM
from models.workers import (
    ChatContextData,
    CustomerBrief,
    EventBrief,
    MessageBrief,
    ScheduleTaskWrite,
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
                return {"ok": True, "id": existing, "skipped": True, "skip_reason": "idempotent"}
            # 同事件防重（决策 17）：同 ref_id + action_id 已有待审提案/未完成任务
            for ref_id in {ev.ref_id for ev in payload.evidence}:
                if await _proposal_pending(session, payload.action_id, ref_id):
                    return {"ok": True, "id": None, "skipped": True, "skip_reason": "duplicate"}
                if await _task_unfinished(session, payload.action_id, ref_id):
                    return {"ok": True, "id": None, "skipped": True, "skip_reason": "duplicate"}
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

    # ---- 读接口：引擎参数（详设 §7.3/§8：GET /api/biz/settings/engine-params）----

    @router.get("/settings/engine-params", dependencies=[Depends(_check_token)])
    async def get_engine_params() -> dict:
        """读取引擎参数（web 侧读 settings 表，缺省回退默认）。

        v0.5 §7.1 扩展：+ llm_model / vision_model（读 settings 表 ai.llm_model /
        ai.vision_model，缺省回退 models.yaml 默认值 deepseek-chat / qwen-vl-max）。

        引擎启动时经 BizApiClient.get 调用本接口读取参数注入 runner。
        """
        from web.settings_store import SettingsStore

        eng = _resolve_engine()
        store = SettingsStore(eng)
        try:
            max_attempts = int(
                await store.get("engine.max_attempts", 2)
            )
            timeout_s = float(
                await store.get("engine.timeout_s", 30.0)
            )
            backoff_cap = int(
                await store.get("engine.backoff_cap", 30)
            )
        except (ValueError, TypeError):
            max_attempts, timeout_s, backoff_cap = 2, 30.0, 30

        # v0.5 §7.1：模型选择（读 settings 表，缺省回退 models.yaml 默认值）
        llm_model = str(
            await store.get("ai.llm_model", "deepseek-chat")
        )
        vision_model = str(
            await store.get("ai.vision_model", "qwen-vl-max")
        )

        return {
            "max_attempts": max_attempts,
            "timeout_s": timeout_s,
            "backoff_cap": backoff_cap,
            "llm_model": llm_model,
            "vision_model": vision_model,
        }

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

    # ---- 读接口：超期客户清单（详设 §8：GET /api/biz/crm/overdue-customers）----

    @router.get("/crm/overdue-customers", dependencies=[Depends(_check_token)])
    async def overdue_customers() -> list[dict]:
        """提醒链数据供给：超期客户清单（决策 37-3：阈值同参 crm.follow_up_days）。

        过滤口径（决策 37-3）：
        - days = now - max(last_contacted_at, updated_at) > crm.follow_up_days
        - follow_up_status != 'archived'（on_hold 含）
        - 按 days_since 降序
        latest_summary 取 crm.customer.latest_summary（页面层维护的冗余字段）。
        """
        from web.settings_store import SettingsStore

        eng = _resolve_engine()
        store = SettingsStore(eng)
        try:
            follow_up_days = int(await store.get("crm.follow_up_days", 5))
        except (ValueError, TypeError):
            follow_up_days = 5

        maker = async_sessionmaker(eng, expire_on_commit=False)
        async with maker() as session:
            # 查非归档客户
            rows = (
                await session.execute(
                    select(Customer).where(
                        Customer.follow_up_status != "archived"
                    )
                )
            ).scalars().all()
            now = datetime.now(timezone.utc)
            result = []
            for c in rows:
                latest = c.last_contacted_at or c.updated_at
                if latest is None:
                    continue
                days_since = (now.date() - latest.date()).days
                if days_since > follow_up_days:
                    result.append(
                        {
                            "customer_id": c.id,
                            "nickname": c.nickname,
                            "days_since": days_since,
                            "latest_summary": c.latest_summary or "",
                        }
                    )
            # 按 days_since 降序
            result.sort(key=lambda x: x["days_since"], reverse=True)
            return result

    # ---- 写接口：定时任务直接落 tm.task（详设 §8：POST /api/biz/tm/schedule-tasks）----

    @router.post("/tm/schedule-tasks", dependencies=[Depends(_check_token)])
    async def schedule_task(payload: ScheduleTaskWrite) -> dict:
        """提醒任务直接落 tm.task（source_type=schedule，决策 37-1）。

        防重（详设 §8/§10.4）：存在 tm.task 满足 domain='crm' AND
        source->>'customer_id'=X AND source->>'reminder_date'=Y AND
        status <> 'void' -> 409。
        """
        # 校验 source.customer_id + reminder_date 必填
        if not payload.source.customer_id:
            raise HTTPException(status_code=422, detail="source.customer_id 不能为空")
        if not payload.source.reminder_date:
            raise HTTPException(status_code=422, detail="source.reminder_date 不能为空")
        # evidence 非空（决策 16①）
        if not payload.evidence:
            raise HTTPException(status_code=422, detail="evidence 为空：无依据不出建议")

        eng = _resolve_engine()
        maker = async_sessionmaker(eng, expire_on_commit=False)
        async with maker() as session, session.begin():
            # 防重：同客户同 reminder_date 已有非 void 任务 -> 409
            dup = await session.execute(
                text(
                    "SELECT id FROM tm.task "
                    "WHERE domain = 'crm' "
                    "AND source->>'customer_id' = :cid "
                    "AND source->>'reminder_date' = :rd "
                    "AND status <> 'void' LIMIT 1"
                ),
                {
                    "cid": str(payload.source.customer_id),
                    "rd": payload.source.reminder_date,
                },
            )
            if dup.scalar_one_or_none() is not None:
                raise HTTPException(
                    status_code=409,
                    detail="当天已生成提醒任务",
                )
            # 解析 due
            try:
                due_date = date.fromisoformat(payload.due)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail="due 格式错误，需 YYYY-MM-DD")
            # 落库 tm.task
            task = Task(
                title=payload.title,
                detail=payload.detail,
                domain=payload.domain,
                role=payload.role,
                due=due_date,
                source_type="schedule",
                source={
                    "chain_id": payload.source.chain_id,
                    "engine_task_id": payload.source.engine_task_id,
                    "worker_id": payload.source.worker_id,
                    "customer_id": payload.source.customer_id,
                    "reminder_date": payload.source.reminder_date,
                    "audit_ids": payload.source.audit_ids or [],
                },
                tags=[],
                created_by="运营",
            )
            session.add(task)
            await session.flush()
            # 创建事件
            event = TaskEvent(
                task_id=task.id,
                event_type="created",
                actor="运营",
                note="定时提醒任务自动就位",
            )
            session.add(event)
            await session.flush()
            return {"ok": True, "id": task.id}

    # ---- 读接口：SEO 关键词历史指标（详设-v0.5 §10：GET /api/biz/seo/metrics/{keyword}）----

    @router.get("/seo/metrics/{keyword}", dependencies=[Depends(_check_token)])
    async def seo_metric_history(keyword: str) -> list[dict]:
        """SEO 关键词历史指标（白名单来源，listing_healthcheck 比较用）。

        按 keyword 查询 seo.keyword_metric 表，返回历史指标列表（按 metric_date 降序）。
        """
        from models.seo import KeywordMetric

        eng = _resolve_engine()
        maker = async_sessionmaker(eng, expire_on_commit=False)
        async with maker() as session:
            rows = (
                await session.execute(
                    select(KeywordMetric)
                    .where(KeywordMetric.keyword == keyword)
                    .order_by(KeywordMetric.metric_date.desc(), KeywordMetric.id.desc())
                    .limit(30)
                )
            ).scalars().all()
            return [
                {
                    "id": r.id,
                    "keyword": r.keyword,
                    "metric_date": r.metric_date.isoformat() if r.metric_date else None,
                    "product_num": r.product_num,
                    "avg_price_top": float(r.avg_price_top) if r.avg_price_top is not None else None,
                    "top_competitors": r.top_competitors or [],
                    "metrics": r.metrics,
                    "quota": r.quota,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

    # ---- 写接口：SEO 关键词指标落库（详设-v0.5 §10：POST /api/biz/seo/metrics）----

    @router.post("/seo/metrics", dependencies=[Depends(_check_token)])
    async def seo_metric_write(payload: dict) -> dict:
        """落新指标（unique(keyword, metric_date) 幂等 409 防重）。

        listing_healthcheck 工序经本接口写入新指标数据。
        """
        from models.seo import KeywordMetric

        # 校验必填字段
        keyword = payload.get("keyword")
        metric_date_str = payload.get("metric_date")
        if not keyword or not metric_date_str:
            raise HTTPException(status_code=422, detail="keyword 和 metric_date 不能为空")

        # 解析日期
        try:
            metric_date = date.fromisoformat(metric_date_str)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="metric_date 格式错误，需 YYYY-MM-DD")

        eng = _resolve_engine()
        maker = async_sessionmaker(eng, expire_on_commit=False)
        async with maker() as session, session.begin():
            # 幂等：同 keyword + 同 metric_date 已存在 -> 409
            dup = await session.execute(
                select(KeywordMetric.id).where(
                    KeywordMetric.keyword == keyword,
                    KeywordMetric.metric_date == metric_date,
                ).limit(1)
            )
            if dup.scalar_one_or_none() is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"指标已存在：keyword={keyword}, metric_date={metric_date_str}",
                )

            # 落库
            row = KeywordMetric(
                keyword=keyword,
                metric_date=metric_date,
                product_num=payload.get("product_num"),
                avg_price_top=payload.get("avg_price_top"),
                top_competitors=payload.get("top_competitors", []),
                metrics=payload.get("metrics"),
                quota=payload.get("quota"),
            )
            session.add(row)
            await session.flush()
            return {"ok": True, "id": row.id}

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

    # ---- 扒图接口（v0.5 批 4，详设-v0.5 §10）----

    @router.get("/scrape/images", dependencies=[Depends(_check_token)])
    async def list_scrape_images(
        batch_id: str | None = None,
        source: str | None = None,
        ids: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """GET /api/biz/scrape/images：白名单来源查询图片列表。"""
        from web import scrape_store

        _SOURCES = {"xhs", "xianyu", "crm"}

        if ids:
            id_list = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()]
            images = []
            for mid in id_list:
                img = await scrape_store.get_image_by_id(mid)
                if img:
                    images.append(img)
            return images

        if source and source not in _SOURCES:
            raise HTTPException(status_code=422, detail=f"source 必须是 {_SOURCES} 之一")

        return await scrape_store.get_images(
            batch_id=batch_id, source=source, limit=limit
        )

    @router.post("/scrape/images", dependencies=[Depends(_check_token)])
    async def create_scrape_image(payload: dict) -> dict:
        """POST /api/biz/scrape/images：落产物（batch_id 幂等 409）。"""
        from web import scrape_store

        batch_id = payload.get("batch_id", "")
        url = payload.get("url", "")
        if not batch_id or not url:
            raise HTTPException(status_code=422, detail="batch_id 和 url 必填")

        # 幂等检查
        existing = await scrape_store.check_batch_idempotent(batch_id)
        # 检查同 batch_id + url 是否已存在
        images = await scrape_store.get_images(batch_id=batch_id, limit=1000)
        for img in images:
            if img.get("url") == url:
                raise HTTPException(status_code=409, detail="同 batch_id + url 已存在（幂等防重）")

        source = payload.get("source", "crm")
        img = await scrape_store.create_image_file(
            batch_id=batch_id,
            source=source,
            url=url,
            local_path=payload.get("local_path"),
            day_dir=payload.get("day_dir"),
            desc=payload.get("desc"),
            tags=payload.get("tags", []),
            author_id=payload.get("author_id"),
            width=payload.get("width"),
            height=payload.get("height"),
            watermark=payload.get("watermark", False),
            status=payload.get("status", "pending"),
        )
        return img

    @router.patch("/scrape/images/{image_id}", dependencies=[Depends(_check_token)])
    async def update_scrape_image(image_id: int, payload: dict) -> dict:
        """PATCH /api/biz/scrape/images/{id}：更新宽高/水印/状态。"""
        from web import scrape_store

        img = await scrape_store.get_image_by_id(str(image_id))
        if not img:
            raise HTTPException(status_code=404, detail="图片不存在")

        updated = await scrape_store.update_image(
            str(image_id),
            width=payload.get("width"),
            height=payload.get("height"),
            watermark=payload.get("watermark"),
            status=payload.get("status"),
            local_path=payload.get("local_path"),
            desc=payload.get("desc"),
            tags=payload.get("tags"),
            author_id=payload.get("author_id"),
        )
        return updated or {}

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
