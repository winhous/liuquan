"""刘全 v0.2 整体系统框架原型（页面原型阶段，mock 数据）。

- 本文件是「页面原型」（建设方案 v0.2 的 FastAPI 骨架先行形态），数据全部
  内存 mock，不接引擎、不碰业务库——真实 TM 数据模型/引擎三接口在 TM 详设
  确认后随 v0.2 开发落地（web 只经三接口碰引擎，不 import engine 内部模块，
  lint P3-2 执法：web/ 不得 import engine.core/engine.workers）。
- 登录为原型形态（共享密码 + 角色 cookie），真实认证随 v0.2 开发落地。
- 原型页面集（整体系统框架优先，任务功能为第一个真实页面）：
  /login 登录页 · / 应用壳首页（任务中心）· /modules/<id> 各模块占位页
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).resolve().parent

app = FastAPI(title="刘全 · 综合智能运营系统（v0.2 原型）")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

# ---- 整体系统框架：模块清单（L1 业务应用层） ----

MODULES: list[dict[str, str]] = [
    {"id": "tm", "name": "任务中心", "icon": "ti ti-list-check", "desc": "AI 提案转任务、人工处理回流（v0.2 首个真实模块）"},
    {"id": "crm", "name": "CRM", "icon": "ti ti-message-circle", "desc": "客户对话翻译 / 快照 / 待办（v0.3 平移，建设中）"},
    {"id": "erp", "name": "ERP 增强", "icon": "ti ti-box", "desc": "库存 / 补货建议 / 异常预警（v0.6，只读 NocoBase 视图）"},
    {"id": "seo", "name": "SEO", "icon": "ti ti-chart-line", "desc": "关键词研究 / 标题优化 / 体检（v0.5，数据源 eHunt）"},
    {"id": "scrape", "name": "扒图", "icon": "ti ti-photo", "desc": "选品扒图 / 图片体检（v0.5，建设中）"},
]

# 角色：cookie 存 ASCII 键（latin-1 限制），显示映射中文标签
ROLES: list[tuple[str, str]] = [("admin", "管理员"), ("ops", "运营"), ("buyer", "采购")]
ROLE_LABEL = dict(ROLES)

# ---- 任务中心 mock 数据（原型展示用；真实模型见 TM 详设） ----

_TODAY = date.today()


def _d(days: int) -> date:
    return _TODAY + timedelta(days=days)


# 状态五态：open / in_progress / done / void / blocked（决策 11）
# 优先级 P0-P3；逾期 = 截止 < 今天 且未 done/void
TASKS: list[dict[str, Any]] = [
    {"id": 1, "title": "回复买家 #A1821 关于物流时效的疑问", "priority": "P0", "status": "open",
     "role": "运营", "due": _d(-1), "source": "AI 建议", "created": _d(-2)},
    {"id": 2, "title": "店铺公告：春节发货安排更新", "priority": "P1", "status": "in_progress",
     "role": "运营", "due": _d(1), "source": "人工", "created": _d(-1)},
    {"id": 3, "title": "采购：补货 20 个 SKU-312 经典款", "priority": "P0", "status": "in_progress",
     "role": "采购", "due": _d(-1), "source": "AI 建议", "created": _d(-3)},
    {"id": 4, "title": "核查上架商品价格与供应商报价差异", "priority": "P1", "status": "open",
     "role": "采购", "due": _d(2), "source": "人工", "created": _d(-1)},
    {"id": 5, "title": "翻译买家对话并更新客户快照（CRM 演示）", "priority": "P2", "status": "in_progress",
     "role": "运营", "due": _d(3), "source": "AI 建议", "created": _d(0)},
    {"id": 6, "title": "整理 3 条差评归因给运营复盘", "priority": "P2", "status": "done",
     "role": "运营", "due": _d(-2), "source": "AI 建议", "created": _d(-4)},
    {"id": 7, "title": "确认大货期：供应商延迟 3 天", "priority": "P3", "status": "blocked",
     "role": "采购", "due": _d(1), "source": "人工", "created": _d(-2)},
    {"id": 8, "title": "过季款式下架清理", "priority": "P3", "status": "void",
     "role": "运营", "due": _d(-5), "source": "人工", "created": _d(-6)},
]

STATUS_LABEL = {"open": "待处理", "in_progress": "进行中", "done": "已完成", "void": "已作废", "blocked": "阻塞"}
PRIORITY_NOTE = {"P0": "今天", "P1": "三天内", "P2": "一周内", "P3": "可缓"}


def _task_view(t: dict[str, Any]) -> dict[str, Any]:
    overdue = t["status"] not in ("done", "void") and t["due"] < _TODAY
    return {**t, "status_label": STATUS_LABEL[t["status"]], "overdue": overdue,
            "due_label": t["due"].isoformat(), "prio_note": PRIORITY_NOTE[t["priority"]]}


# 提案待审核（决策 12：全部人工审，不设代码自动通过）
PROPOSALS: list[dict[str, Any]] = [
    {"id": 101, "title": "建议回复买家 #A1821（超 48h 未跟进）", "domain": "crm", "risk": "suggest",
     "priority": "P0", "role": "运营", "due": 0,
     "detail": "买家询问物流时效，超过 48 小时未跟进，建议当天回复并提供预计送达时间。",
     "evidence": [{"kind": "message", "ref": "msg-88231", "quote": "Where is my order? It's been 2 weeks."}],
     "source": {"chain": "chat-inbox", "worker": "crm_translate", "audit": "e-000001"}},
    {"id": 102, "title": "建议补货 SKU-312 经典款 20 件", "domain": "erp", "risk": "suggest",
     "priority": "P1", "role": "采购", "due": 3,
     "detail": "近 30 天销量 45 件、现库存 6 件、供应商交期 7 天，按安全库存建议补货 20 件。",
     "evidence": [{"kind": "metric", "ref": "sku-312", "quote": "库存 6 件 / 近 30 天销量 45 件"}],
     "source": {"chain": "inventory-check", "worker": "erp_replenish", "audit": "e-000002"}},
    {"id": 103, "title": "建议更新客户 A1821 快照（新对话要点）", "domain": "crm", "risk": "suggest",
     "priority": "P2", "role": "运营", "due": 7,
     "detail": "客户本次对话新增信息：偏好空运、对价格敏感。建议追加到快照。",
     "evidence": [{"kind": "message", "ref": "msg-88231", "quote": "prefer air shipping"}],
     "source": {"chain": "chat-inbox", "worker": "snapshot-update", "audit": "e-000003"}},
]


def _proposal_view(p: dict[str, Any]) -> dict[str, Any]:
    return {**p, "prio_note": PRIORITY_NOTE[p["priority"]],
            "risk_label": {"read": "只读分析", "suggest": "建议", "write": "写操作"}[p["risk"]]}


@app.get("/")
def index(request: Request):
    return RedirectResponse("/tasks")


@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"roles": ROLES})


@app.post("/login")
def login_submit(request: Request, role: str = "ops"):
    resp = RedirectResponse("/tasks", status_code=303)
    resp.set_cookie("role", role, max_age=86400)  # 原型：明文角色 cookie；真实认证随 v0.2 开发
    return resp


@app.get("/tasks")
def tasks_page(request: Request):
    role_key = request.cookies.get("role", "admin")
    role = ROLE_LABEL.get(role_key, role_key)
    tasks = [_task_view(t) for t in TASKS]
    proposals = [_proposal_view(p) for p in PROPOSALS]
    stats = {
        "open": sum(1 for t in tasks if t["status"] in ("open", "in_progress")),
        "overdue": sum(1 for t in tasks if t["overdue"]),
        "pending_proposals": len(proposals),
        "done_today": sum(1 for t in tasks if t["status"] == "done"),
    }
    return templates.TemplateResponse(request, "tasks.html", {
        "role": role, "modules": MODULES, "active": "tm",
        "tasks": tasks, "proposals": proposals, "stats": stats,
    })


@app.get("/modules/{module_id}")
def module_page(request: Request, module_id: str):
    role = ROLE_LABEL.get(request.cookies.get("role", "admin"), request.cookies.get("role", "admin"))
    module = next((m for m in MODULES if m["id"] == module_id), None)
    if module is None:
        return RedirectResponse("/tasks")
    return templates.TemplateResponse(request, "module.html", {
        "role": role, "modules": MODULES, "active": module_id,
        "module": module,
    })
