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
from datetime import date, datetime, time, timezone

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

        # v0.6 §5.5：扒图存储目录 + 定时默认时间（读 settings 表，缺省回退默认；
        # time 值序列化为 "HH:MM" 字符串——引擎启动装配 connector 落盘根 + 种子 cron）
        scrape_storage_dir = str(
            await store.get("scrape.storage_dir", "/opt/liuquan/scrape/")
        )
        _raw_schedule_time = await store.get("scrape.schedule_time", "07:00")
        if isinstance(_raw_schedule_time, time):
            scrape_schedule_time = _raw_schedule_time.strftime("%H:%M")
        else:
            scrape_schedule_time = str(_raw_schedule_time)

        return {
            "max_attempts": max_attempts,
            "timeout_s": timeout_s,
            "backoff_cap": backoff_cap,
            "llm_model": llm_model,
            "vision_model": vision_model,
            "scrape.storage_dir": scrape_storage_dir,
            "scrape.schedule_time": scrape_schedule_time,
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
        link_record_id: int | None = None,  # v0.6：按链接筛选（白名单来源）
        limit: int = 100,
    ) -> list[dict]:
        """GET /api/biz/scrape/images：白名单来源查询图片列表。

        v0.6 §7：+link_record_id 筛选（provider scrape.image_context /
        image_inspect 按链接取图用）。
        """
        from web import scrape_store

        _SOURCES = {"xhs", "xianyu", "http"}

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
            batch_id=batch_id, source=source, link_record_id=link_record_id, limit=limit
        )

    @router.post("/scrape/images", dependencies=[Depends(_check_token)])
    async def create_scrape_image(payload: dict) -> dict:
        """POST /api/biz/scrape/images：落产物（幂等 409 防重）。

        v0.6 §5.2/§7：+link_record_id（挂链接）+ source_mark（默认 scraped）；
        幂等键 = link_record_id + url（uq_scrape_image_link_url；批 4 语义——
        同链接同 URL 只写一次，batch_image_download 对 409 幂等忽略）；
        link_record_id 为空时回退 v0.5 的 batch_id + url 幂等（兼容存量调用）。
        """
        from web import scrape_store

        batch_id = payload.get("batch_id", "")
        url = payload.get("url", "")
        if not batch_id or not url:
            raise HTTPException(status_code=422, detail="batch_id 和 url 必填")

        link_record_id = payload.get("link_record_id")

        # 幂等检查：link_record_id + url（v0.6 图片幂等键）或 batch_id + url（兼容存量）
        if link_record_id is not None:
            images = await scrape_store.get_images(
                link_record_id=int(link_record_id), limit=1000
            )
            for img in images:
                if img.get("url") == url:
                    raise HTTPException(
                        status_code=409, detail="同 link_record_id + url 已存在（幂等防重）"
                    )
        else:
            existing = await scrape_store.check_batch_idempotent(batch_id)
            images = await scrape_store.get_images(batch_id=batch_id, limit=1000)
            for img in images:
                if img.get("url") == url:
                    raise HTTPException(status_code=409, detail="同 batch_id + url 已存在（幂等防重）")

        source = payload.get("source", "http")
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
            link_record_id=int(link_record_id) if link_record_id is not None else None,
            source_mark=payload.get("source_mark", "scraped"),
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

    # ---- 扒图链接记录接口（v0.6 批 2，详设-v0.6 §7：link_record_create 写接口）----

    def _infer_link_source(url: str) -> str:
        """来源识别（默认 http）：域名含 xiaohongshu/xhslink → xhs；goofish → xianyu。"""
        low = url.lower()
        if "xiaohongshu" in low or "xhslink" in low:
            return "xhs"
        if "goofish" in low:
            return "xianyu"
        return "http"

    @router.post("/scrape/links", dependencies=[Depends(_check_token)])
    async def create_scrape_links(payload: dict) -> dict:
        """POST /api/biz/scrape/links：批量建链接记录（normalized_url 幂等，决策 26）。

        payload: {urls: [str], batch_id: str, source?: str}
        每条 url 规范化（去 xsec_token 等易变 query，详设-v0.6 §4.1）后幂等：
        已存在返回现有行（created/existing 标记），不重复建、不重复下载。
        """
        from web import scrape_store

        raw_urls = payload.get("urls") or []
        if isinstance(raw_urls, str):
            raw_urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]
        elif isinstance(raw_urls, list):
            raw_urls = [str(u).strip() for u in raw_urls if str(u).strip()]
        if not raw_urls:
            raise HTTPException(status_code=422, detail="urls 不能为空")
        batch_id = str(payload.get("batch_id", "")).strip()
        if not batch_id:
            raise HTTPException(status_code=422, detail="batch_id 必填")
        source_hint = str(payload.get("source", "")).strip() or None

        links = []
        for u in raw_urls:
            source = source_hint or _infer_link_source(u)
            row = await scrape_store.create_link(u, source, batch_id)
            links.append(
                {
                    "url": u,
                    "id": row["id"],
                    "source": row["source"],
                    "normalized_url": row["normalized_url"],
                    "created": row["created"],
                    "existing": row["existing"],
                }
            )
        return {
            "ok": True,
            "links": links,
            "created_count": sum(1 for l in links if l["created"]),
            "existing_count": sum(1 for l in links if l["existing"]),
        }

    @router.get("/scrape/links", dependencies=[Depends(_check_token)])
    async def list_scrape_links(
        source: str | None = None,
        status: str | None = None,
        ids: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """GET /api/biz/scrape/links：链接记录列表（白名单来源，详设-v0.6 §7）。

        provider scrape.link_context（ids 批量）与素材库读接口用；返回 {links: [...]}。
        """
        from web import scrape_store

        if ids:
            id_list = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()]
            links = []
            for lid in id_list:
                detail = await scrape_store.get_link_by_id(lid)
                if detail:
                    links.append(detail["link"])
            return {"links": links}

        links = await scrape_store.get_links(
            source=source, status=status, limit=limit, offset=offset
        )
        return {"links": links}

    @router.get("/scrape/links/{link_id}", dependencies=[Depends(_check_token)])
    async def get_scrape_link(link_id: int) -> dict:
        """GET /api/biz/scrape/links/{id}：单条链接 + 图片列表（白名单来源）。

        batch_image_download 读 url/source/status（决策 26 读也走接口）。
        """
        from web import scrape_store

        detail = await scrape_store.get_link_by_id(link_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="链接记录不存在")
        return detail

    @router.patch("/scrape/links/{link_id}", dependencies=[Depends(_check_token)])
    async def update_scrape_link(link_id: int, payload: dict) -> dict:
        """PATCH /api/biz/scrape/links/{id}：更新状态/图数/元数据/error_note/degraded_note
        + netdisk 三列（批 7：netdisk_status/netdisk_url/netdisk_uploaded_at 回填）。

        batch_image_download 落链接终态（决策 26：写也走接口）。
        批 7（详设 §15.2 技术定）：payload.netdisk_url 非空 → 同步重写该链接
        本地文件夹 meta.txt 的「网盘分享链接」行（分享链接回填后本地留痕；
        文件改写收敛在本写接口一处，引擎上传 helper 只回填 link_record 三列）。
        """
        from web import scrape_store

        netdisk_kwargs: dict = {}
        if "netdisk_status" in payload:
            netdisk_kwargs["netdisk_status"] = payload["netdisk_status"]
        if "netdisk_url" in payload:
            netdisk_kwargs["netdisk_url"] = payload["netdisk_url"]
        if "netdisk_uploaded_at" in payload:
            netdisk_kwargs["netdisk_uploaded_at"] = payload["netdisk_uploaded_at"]

        updated = await scrape_store.update_link(
            link_id,
            status=payload.get("status"),
            image_count=payload.get("image_count"),
            desc=payload.get("desc"),
            tags=payload.get("tags"),
            author_id=payload.get("author_id"),
            storage_dir=payload.get("storage_dir"),
            error_note=payload.get("error_note"),
            degraded_note=payload.get("degraded_note"),
            **netdisk_kwargs,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="链接记录不存在")

        # 批 7：上传成功（netdisk_url 非空）→ 同步本地文件夹 meta.txt 网盘行
        if netdisk_kwargs.get("netdisk_url"):
            from web.settings_store import SettingsStore

            store = SettingsStore(_resolve_engine())
            storage_root = await store.get(
                "scrape.storage_dir", "/opt/liuquan/scrape/"
            )
            try:
                await scrape_store.rewrite_link_meta_netdisk(
                    updated, str(storage_root), str(netdisk_kwargs["netdisk_url"])
                )
            except Exception:  # noqa: BLE001 - 文件同步尽力而为，不阻断接口
                logger.warning(
                    "PATCH /scrape/links/%s: meta.txt 网盘行同步失败（忽略）", link_id
                )
        return updated

    @router.post("/scrape/queue/clear", dependencies=[Depends(_check_token)])
    async def clear_scrape_queue() -> dict:
        """POST /api/biz/scrape/queue/clear：清空定时队列（scrape.link_queue → []）。

        消费者 scrape.download_done（from_queue=true 链成功完成后调用，
        详设-v0.6 §5.3/§7）。
        """
        from web import scrape_store
        from web.settings_store import SettingsStore

        store = SettingsStore(_resolve_engine())
        await scrape_store.set_link_queue([], settings=store)
        return {"ok": True, "queue": []}

    @router.get("/settings/link-queue", dependencies=[Depends(_check_token)])
    async def read_link_queue() -> dict:
        """GET /api/biz/settings/link-queue：读定时队列（白名单来源，详设-v0.6 §7）。

        provider scrape.link_queue（link_record_create from_queue 分支）用。
        """
        from web import scrape_store
        from web.settings_store import SettingsStore

        store = SettingsStore(_resolve_engine())
        queue = await scrape_store.get_link_queue(settings=store)
        return {"urls": queue}

    # ==== CRM 对话图片接口（v0.5 批 5，详设-v0.5 §10）====
    from models.crm import MessageImage

    @router.get(
        "/crm/message-images",
        dependencies=[Depends(_check_token)],
    )
    async def crm_message_images(
        message_id: int | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """CRM 对话图片列表（白名单来源）。

        - message_id：筛选指定消息的图片
        - status：筛选指定状态的图片
        """
        async with async_sessionmaker(_resolve_engine(), expire_on_commit=False)() as s:
            stmt = select(MessageImage)
            if message_id is not None:
                stmt = stmt.where(MessageImage.message_id == message_id)
            if status is not None:
                stmt = stmt.where(MessageImage.status == status)
            stmt = stmt.order_by(MessageImage.created_at.desc())
            rows = (await s.execute(stmt)).scalars().all()
            return [
                {
                    "id": r.id,
                    "message_id": r.message_id,
                    "url": r.url,
                    "local_path": r.local_path,
                    "status": r.status,
                    "width": r.width,
                    "height": r.height,
                    "ocr_text": r.ocr_text,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

    @router.post(
        "/crm/message-images",
        dependencies=[Depends(_check_token)],
        status_code=201,
    )
    async def crm_message_images_create(payload: dict) -> dict:
        """创建 CRM 对话图片记录（pending 状态）。

        幂等：同 message_id + url 已存在返回 409。
        """
        message_id = payload.get("message_id")
        url = payload.get("url")
        if not message_id or not url:
            raise HTTPException(status_code=400, detail="message_id 和 url 必填")

        async with async_sessionmaker(_resolve_engine(), expire_on_commit=False)() as s:
            # 幂等检查：同 message_id + url 已存在
            existing = await s.execute(
                select(MessageImage).where(
                    MessageImage.message_id == message_id,
                    MessageImage.url == url,
                )
            )
            if existing.scalar_one_or_none() is not None:
                raise HTTPException(status_code=409, detail="同 message_id + url 已存在")

            img = MessageImage(message_id=message_id, url=url, status="pending")
            s.add(img)
            await s.commit()
            await s.refresh(img)
            return {
                "id": img.id,
                "message_id": img.message_id,
                "url": img.url,
                "status": img.status,
                "created_at": img.created_at.isoformat() if img.created_at else None,
            }

    @router.patch(
        "/crm/message-images/{image_id}",
        dependencies=[Depends(_check_token)],
    )
    async def crm_message_images_update(
        image_id: int, payload: dict
    ) -> dict:
        """更新 CRM 对话图片（status/local_path/width/height/ocr_text）。"""
        async with async_sessionmaker(_resolve_engine(), expire_on_commit=False)() as s:
            row = (
                await s.execute(
                    select(MessageImage).where(MessageImage.id == image_id)
                )
            ).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="图片不存在")

            # 只更新传入的字段
            if "status" in payload:
                row.status = payload["status"]
            if "local_path" in payload:
                row.local_path = payload["local_path"]
            if "width" in payload:
                row.width = payload["width"]
            if "height" in payload:
                row.height = payload["height"]
            if "ocr_text" in payload:
                row.ocr_text = payload["ocr_text"]

            await s.commit()
            await s.refresh(row)
            return {
                "id": row.id,
                "message_id": row.message_id,
                "url": row.url,
                "local_path": row.local_path,
                "status": row.status,
                "width": row.width,
                "height": row.height,
                "ocr_text": row.ocr_text,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }

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
