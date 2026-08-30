"""刘全 v0.2 T6：web 接真数据（替换原型 mock——业务库 tm 表 + 引擎三接口 HTTP）。

- 数据层：web/tm_store.py（web 侧独立 DAO：models/tm.py 共享 ORM + SQLAlchemy
  async，连接串读 .env 的 LIUQUAN_TM_DB_URL；**零 engine import**，lint
  P3-2 执法，双向零代码耦合 R24）。
- 引擎：web/engineapi/client.py 三接口 HTTP 客户端（触发演示链面板：registry
  链清单 -> POST 触发 -> 轮询 GET 查终态 -> 刷新提案栏；引擎未连接时页面
  降级展示，不影响其余功能）。
- 登录为原型形态（共享密码 + 角色 cookie，ASCII 键 admin/ops/buyer 映射中文
  标签）；未登录访问 /tasks 重定向 /login（验收 A16）。
- 权限 v0.2 不做（决策 17 第 2 条）：三角色仅展示，任何角色可看全部任务、
  可批任何提案、可编辑任何任务；查询层已留 status/domain/role 过滤参数位。
- 测试友好：create_app(tm_store=..., engine_client_factory=...) 构造注入
  （R12 注入式：测试可注入嵌入式 PG 的 store 与 MockTransport 引擎桩，
  零网络零真服务）。
- 路由全部 async（查询是 async DB 调用）；Jinja2 用新签名
  TemplateResponse(request, "x.html", {...})（v0.1 踩过旧签名 500 的坑）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from models.tm import Task, TaskProposal
from web.engineapi.client import EngineAPIError, EngineAPIClient
from web.tm_store import (
    ROLE_VALUES,
    TMStore,
    TMWebError,
    proposal_display_id,
    task_display_id,
)

BASE = Path(__file__).resolve().parent

# 角色：cookie 存 ASCII 键（latin-1 限制），显示映射中文标签（原型保留）
ROLES: list[tuple[str, str]] = [("admin", "管理员"), ("ops", "运营"), ("buyer", "采购")]
ROLE_LABEL = dict(ROLES)

# 状态五态（决策 11 修订：无优先级分级，无 P0-P3）
STATUS_LABEL = {
    "open": "待处理",
    "in_progress": "进行中",
    "done": "已完成",
    "void": "已作废",
    "blocked": "阻塞",
}

RISK_LABEL = {"read": "只读分析", "suggest": "建议", "write": "写操作"}

# 来源徽章 = domain（决策 16：任务来源标徽章，一眼可见"这任务是谁提的"）
DOMAIN_META: dict[str, dict[str, str]] = {
    "crm": {"label": "CRM", "cls": "bg-indigo"},
    "erp": {"label": "ERP", "cls": "bg-purple"},
    "seo": {"label": "SEO", "cls": "bg-teal"},
    "tm": {"label": "TM", "cls": "bg-cyan"},
    "demo": {"label": "演示", "cls": "bg-orange"},
}

# 触发演示链面板：链输入表单声明（v0.2 演示链 tm_demo_chain / demo_echo_chain；
# 引擎 registry 接口不返回 input schema，web 侧按链声明表单字段，未知链走 JSON）
CHAIN_INPUTS: dict[str, dict[str, Any]] = {
    "tm_demo_chain": {
        "fields": [
            {"name": "text", "label": "输入文本", "type": "text", "required": True},
            {"name": "ref_id", "label": "证据对象 id（ref_id）", "type": "text", "required": True},
            {"name": "kind", "label": "证据类型", "type": "select", "required": True,
             "options": ["message", "metric", "order_view", "listing", "image"]},
        ]
    },
    "demo_echo_chain": {
        "fields": [
            {"name": "text", "label": "输入文本", "type": "text", "required": True},
        ]
    },
}

# 链 id -> 中文展示名（用户复核反馈：触发面板链名称改中文）。
# 链 id 是引擎技术标识（loader L1 强制 snake_case 不可改），展示层映射中文；
# 未知链回退显示 id 本身。
CHAIN_LABELS: dict[str, str] = {
    "tm_demo_chain": "任务提案演示链",
    "demo_echo_chain": "回声冒烟链",
    "crm_translate_chain": "翻译雏形链",
}

# 状态操作 -> 完成提示语（msg 展示）
_ACTION_MSG = {
    "started": "已开始",
    "completed": "已完成",
    "voided": "已作废",
    "blocked": "已阻塞",
    "unblocked": "已解除阻塞",
}

# ---- 整体系统框架：模块清单（L1 业务应用层；children = 二级导航）----

MODULES: list[dict[str, Any]] = [
    {"id": "workspace", "name": "工作台", "icon": "ti ti-dashboard", "href": "/modules/workspace",
     "desc": "统计数据等工作台内容后续在定（决策 15：统计一定会做）"},
    {"id": "tm", "name": "任务中心", "icon": "ti ti-list-check", "href": "/tasks",
     "desc": "AI 提案转任务、人工处理回流（v0.2 首个真实模块）"},
    {"id": "crm", "name": "CRM", "icon": "ti ti-message-circle", "href": "/modules/crm",
     "desc": "客户对话翻译 / 快照 / 待办（v0.3 平移，建设中）",
     "children": [
         {"id": "crm-translate", "name": "对话翻译", "icon": "ti ti-language", "href": "/modules/crm"},
         {"id": "crm-snapshot", "name": "客户快照", "icon": "ti ti-users", "href": "/modules/crm"},
         {"id": "crm-follow", "name": "跟进待办", "icon": "ti ti-list", "href": "/modules/crm"},
     ]},
    {"id": "erp", "name": "ERP 增强", "icon": "ti ti-box", "href": "/modules/erp",
     "desc": "库存 / 补货建议 / 异常预警（v0.6，只读 NocoBase 视图）",
     "children": [
         {"id": "erp-stock", "name": "库存", "icon": "ti ti-box", "href": "/modules/erp"},
         {"id": "erp-replenish", "name": "补货建议", "icon": "ti ti-shopping-cart", "href": "/modules/erp"},
         {"id": "erp-alert", "name": "异常预警", "icon": "ti ti-alert-triangle", "href": "/modules/erp"},
     ]},
    {"id": "seo", "name": "SEO", "icon": "ti ti-chart-line", "href": "/modules/seo",
     "desc": "关键词研究 / 标题优化 / 体检（v0.5，数据源 eHunt）"},
    {"id": "scrape", "name": "扒图", "icon": "ti ti-photo", "href": "/modules/scrape",
     "desc": "选品扒图 / 图片体检（v0.5，建设中）"},
]


# ---- 视图助手 ----

def _domain_meta(domain: str) -> dict[str, str]:
    return DOMAIN_META.get(domain, {"label": domain or "其他", "cls": "bg-secondary"})


def _task_view(t: Task, *, parent_title: str | None = None, child_count: int = 0) -> dict[str, Any]:
    """任务行视图：展示形 id / 状态标签 / 逾期标记 / 派生关联（父标题+子任务数）。"""
    overdue = t.status not in ("done", "void") and t.due < date.today()
    return {
        "id": t.id,
        "display_id": task_display_id(t.id),
        "title": t.title,
        "detail": t.detail or "",
        "domain": t.domain,
        "domain_meta": _domain_meta(t.domain),
        "role": t.role,
        "due": t.due,
        "due_label": t.due.isoformat(),
        "status": t.status,
        "status_label": STATUS_LABEL[t.status],
        "overdue": overdue,
        "blocked_reason": t.blocked_reason,
        "result_note": t.result_note,
        "derived_from": t.derived_from,
        "derived_from_label": task_display_id(t.derived_from) if t.derived_from else None,
        "parent_title": parent_title,  # 父任务标题（决策 17 派生关联展示）
        "child_count": child_count,    # 本任务的子任务数（0 = 无子任务）
        "source_type": t.source_type,
        "source": t.source or {},
        "created_by": t.created_by,
    }


def _proposal_view(p: TaskProposal) -> dict[str, Any]:
    """提案视图：风险徽章 / 依据区（evidence 类型+ref_id+原文摘录）/ 来源追溯。"""
    return {
        "id": p.id,
        "display_id": proposal_display_id(p.id),
        "title": p.title,
        "detail": p.detail,
        "domain": p.domain,
        "domain_meta": _domain_meta(p.domain),
        "risk": p.risk,
        "risk_label": RISK_LABEL.get(p.risk, p.risk),
        "suggested_role": p.suggested_role,
        "suggested_due_days": p.suggested_due_days,
        "evidence": p.evidence or [],
        "source": p.source or {},
    }


def _role_label(request: Request) -> str:
    """当前角色中文标签（cookie ASCII 键映射；无 cookie 缺省管理员）。"""
    role_key = request.cookies.get("role", "admin")
    return ROLE_LABEL.get(role_key, role_key)


def _ctx(request: Request, active: str, **extra: Any) -> dict[str, Any]:
    """页面公共上下文：角色 + 模块清单 + 当前页高亮。"""
    return {"role": _role_label(request), "modules": MODULES, "active": active, **extra}


def _redirect(
    path: str = "/tasks",
    *,
    msg: str | None = None,
    err: str | None = None,
    extra: dict[str, str] | None = None,
) -> RedirectResponse:
    """POST 后重定向（PRG 模式）+ 消息/错误提示（query 参数）。"""
    params: dict[str, str] = {}
    if msg:
        params["msg"] = msg
    if err:
        params["err"] = err
    if extra:
        params.update(extra)
    qs = urlencode(params)
    url = f"{path}?{qs}" if qs else path
    return RedirectResponse(url, status_code=303)


def _store(request: Request) -> TMStore:
    return request.app.state.tm_store


def _engine_client(request: Request) -> EngineAPIClient:
    return request.app.state.engine_client_factory()


# ---- 应用工厂（测试构造注入：tm_store / engine_client_factory）----

def create_app(
    *,
    tm_store: TMStore | None = None,
    engine_client_factory: Callable[[], EngineAPIClient] | None = None,
) -> FastAPI:
    """建 web 应用；依赖可注入（R12：测试注入嵌入式 PG store + MockTransport 引擎桩）。"""

    @asynccontextmanager
    async def _lifespan(app_: FastAPI):
        if app_.state.tm_store is None:
            app_.state.tm_store = TMStore.from_env()  # 生产：.env 连接串
        yield
        await app_.state.tm_store.dispose()

    app = FastAPI(title="刘全 · 综合智能运营系统（v0.2）", lifespan=_lifespan)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")
    app.state.tm_store = tm_store
    app.state.engine_client_factory = engine_client_factory or (lambda: EngineAPIClient())

    # ---- 业务读写接口（v0.3 决策 26 接口化：引擎经 /api/biz/* 读写业务数据，不直连业务库）----
    from web.api_biz import create_biz_router

    app.include_router(create_biz_router())

    # ---- 登录 / 导航 / 占位页（原型保留，最小改动）----

    @app.get("/")
    def index(request: Request):
        return RedirectResponse("/tasks")

    @app.get("/login")
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"roles": ROLES})

    @app.post("/login")
    def login_submit(request: Request, role: str = "ops"):
        resp = RedirectResponse("/tasks", status_code=303)
        resp.set_cookie("role", role, max_age=86400)  # 原型：明文角色 cookie
        return resp

    @app.get("/modules/{module_id}")
    def module_page(request: Request, module_id: str):
        module = next((m for m in MODULES if m["id"] == module_id), None)
        if module is None:
            return RedirectResponse("/tasks")
        return templates.TemplateResponse(
            request, "module.html", _ctx(request, module_id, module=module)
        )

    # ---- 任务中心（真实 tm 数据）----

    @app.get("/tasks")
    async def tasks_page(
        request: Request,
        status: str = "all",
        overdue: bool = False,
        q: str = "",
    ):
        """任务列表（真实 tm.task）+ 提案待审核侧栏（真实 tm.task_proposal）。

        筛选：状态 / 逾期 / 关键词（详设 §6.2，先做这三个）；逾期提醒条常驻
        （决策 17 第 5 条）；触发演示链面板链清单来自引擎 registry。
        """
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)  # A16
        store = _store(request)
        status_filter = status if status in STATUS_LABEL else None
        tasks = await store.list_tasks(
            status=status_filter, overdue_only=overdue, keyword=q.strip() or None
        )
        proposals = await store.list_pending_proposals()
        stats = await store.stats()
        # 引擎链清单（触发面板）；引擎未连接/未启动时降级展示，不影响其余功能
        registry: dict[str, Any] | None = None
        registry_error: str | None = None
        try:
            async with _engine_client(request) as client:
                registry = await client.list_registry()
        except EngineAPIError as exc:
            registry_error = f"引擎未连接：{exc}"
        return templates.TemplateResponse(
            request,
            "tasks.html",
            _ctx(
                request,
                "tm",
                tasks=[_task_view(t.task, parent_title=t.parent_title, child_count=t.child_count) for t in tasks],
                proposals=[_proposal_view(p) for p in proposals],
                stats=stats,
                status_options=list(STATUS_LABEL.items()),
                role_values=ROLE_VALUES,
                domain_options=[
                    (key, meta["label"])
                    for key, meta in DOMAIN_META.items()
                    if key != "demo"  # 人工/编辑表单的来源域选项（决策 16）
                ],
                filters={"status": status, "overdue": overdue, "q": q},
                registry=registry,
                registry_error=registry_error,
                chain_inputs=CHAIN_INPUTS,
                chain_labels=CHAIN_LABELS,
                msg=request.query_params.get("msg", ""),
                err=request.query_params.get("err", ""),
                engine_task_id=request.query_params.get("engine_task_id", ""),
            ),
        )

    @app.post("/tasks/status")
    async def task_status(
        request: Request,
        task_id: int = Form(...),
        action: str = Form(...),
        result_note: str = Form(""),
        blocked_reason: str = Form(""),
    ):
        """状态操作：started/completed/voided/blocked/unblocked（§3.5.1）。

        completed/voided 必填 result_note（决策 17，页面弹窗必填 + DB CHECK
        兜底）；blocked 必填 blocked_reason（决策 11：等物料/等回复）。
        """
        store = _store(request)
        try:
            await store.transition(
                task_id,
                action,
                actor=_role_label(request),
                result_note=result_note.strip() or None,
                blocked_reason=blocked_reason.strip() or None,
            )
        except TMWebError as exc:
            return _redirect(err=str(exc))
        return _redirect(msg=f"{task_display_id(task_id)} {_ACTION_MSG.get(action, action)}")

    @app.post("/tasks/create")
    async def task_create(
        request: Request,
        title: str = Form(...),
        detail: str = Form(""),
        domain: str = Form(...),
        role: str = Form(...),
        due: str = Form(...),
    ):
        """人工建任务（决策 16 双通道：人工通道，source_type=manual + 来源域）。"""
        store = _store(request)
        try:
            task = await store.create_task(
                title=title,
                detail=detail,
                domain=domain,
                role=role,
                due=_parse_due(due),
                actor=_role_label(request),
            )
        except (TMWebError, ValueError) as exc:
            return _redirect(err=str(exc))
        return _redirect(msg=f"已创建任务 {task_display_id(task.id)}（人工创建）")

    @app.post("/tasks/derive")
    async def task_derive(
        request: Request,
        parent_id: int = Form(...),
        title: str = Form(...),
        detail: str = Form(""),
        domain: str = Form(""),
        role: str = Form(...),
        due: str = Form(...),
    ):
        """派生新任务（决策 17：派生≠原任务结束；task_event 记 derived）。"""
        store = _store(request)
        try:
            task = await store.derive_task(
                parent_id,
                title=title,
                detail=detail,
                domain=domain,
                role=role,
                due=_parse_due(due),
                actor=_role_label(request),
            )
        except (TMWebError, ValueError) as exc:
            return _redirect(err=str(exc))
        return _redirect(
            msg=f"已由 {task_display_id(parent_id)} 派生新任务 {task_display_id(task.id)}"
        )

    @app.post("/tasks/edit")
    async def task_edit(
        request: Request,
        task_id: int = Form(...),
        title: str = Form(...),
        role: str = Form(...),
        due: str = Form(...),
        domain: str = Form(...),
        detail: str = Form(""),
    ):
        """编辑任务（§3.5.5）：改 title/role/due/domain/detail，updated 事件带快照。"""
        store = _store(request)
        try:
            task = await store.edit_task(
                task_id,
                title=title,
                role=role,
                due=_parse_due(due),
                domain=domain,
                detail=detail,
                actor=_role_label(request),
            )
        except (TMWebError, ValueError) as exc:
            return _redirect(err=str(exc))
        return _redirect(msg=f"{task_display_id(task_id)} 已更新")

    @app.post("/tasks/reopen")
    async def task_reopen(request: Request, task_id: int = Form(...)):
        """重开：done/void -> open（改状态 + updated 事件留痕，决策 17 第 4 条）。"""
        store = _store(request)
        try:
            await store.reopen_task(task_id, actor=_role_label(request))
        except TMWebError as exc:
            return _redirect(err=str(exc))
        return _redirect(msg=f"{task_display_id(task_id)} 已重开回待处理")

    @app.post("/tasks/proposals/approve")
    async def proposal_approve(request: Request, proposal_id: int = Form(...)):
        """批准提案（§3.2 审核流：事务内 提案 approved + 建任务 + 双事件）。"""
        store = _store(request)
        try:
            task = await store.approve_proposal(proposal_id, actor=_role_label(request))
        except TMWebError as exc:
            return _redirect(err=str(exc))
        return _redirect(
            msg=f"已批准提案 {proposal_display_id(proposal_id)}，生成任务 {task_display_id(task.id)}"
        )

    @app.post("/tasks/proposals/reject")
    async def proposal_reject(
        request: Request, proposal_id: int = Form(...), reason: str = Form("")
    ):
        """驳回提案：rejected + reject_reason（不建任务）。"""
        store = _store(request)
        try:
            await store.reject_proposal(
                proposal_id, actor=_role_label(request), reason=reason
            )
        except TMWebError as exc:
            return _redirect(err=str(exc))
        return _redirect(msg=f"已驳回提案 {proposal_display_id(proposal_id)}")

    # ---- 触发演示链（引擎三接口：registry -> POST -> 轮询 GET -> 刷新提案栏）----

    @app.post("/tasks/trigger")
    async def task_trigger(request: Request, chain_id: str = Form(...)):
        """人工触发一个工序链（详设 §4.1）：建引擎任务入队，页面侧轮询查终态。"""
        actor = _role_label(request)
        spec = CHAIN_INPUTS.get(chain_id)
        form = await request.form()
        if spec is None:
            raw = (form.get("input_json") or "").strip()
            if not raw:
                return _redirect(err="请填写链输入（JSON 文本域）")
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                return _redirect(err=f"链输入不是合法 JSON：{exc}")
            if not isinstance(payload, dict):
                return _redirect(err="链输入必须是 JSON 对象（{...}）")
        else:
            payload: dict[str, str] = {}
            missing = [f["label"] for f in spec["fields"]
                       if f.get("required") and not str(form.get(f["name"]) or "").strip()]
            if missing:
                return _redirect(err=f"缺少必填参数：{'、'.join(missing)}")
            for f in spec["fields"]:
                payload[f["name"]] = str(form.get(f["name"]) or "").strip()
        try:
            async with _engine_client(request) as client:
                created = await client.create_task(chain_id, payload, actor)
        except EngineAPIError as exc:
            return _redirect(err=f"触发失败：{exc}")
        eid = str(created.get("task_id", ""))
        return _redirect(
            msg=f"已触发链 {chain_id}，引擎任务 {eid}（等待完成，页面自动刷新待审核栏）",
            extra={"engine_task_id": eid},
        )

    @app.get("/tasks/engine/{engine_task_id}")
    async def engine_task_status(request: Request, engine_task_id: str):
        """轮询代理：GET /api/engine/tasks/{id} -> JSON（JS 轮询查终态，§4.2）。"""
        try:
            async with _engine_client(request) as client:
                data = await client.get_task(engine_task_id)
        except EngineAPIError as exc:
            return JSONResponse({"status": "error", "detail": str(exc)})
        terminal = data.get("status") in ("done", "failed", "error")
        return JSONResponse({**data, "terminal": terminal})

    return app


# ---- 工具 ----

def _parse_due(raw: str) -> date:
    """解析表单截止日期（yyyy-mm-dd）。"""
    value = (raw or "").strip()
    if not value:
        raise ValueError("截止日期（due）必填")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"截止日期格式应为 yyyy-mm-dd：{value!r}") from exc


app = create_app()
