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
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import async_sessionmaker

from models.tm import Task, TaskProposal
from web.crm_store import CRMStore, CrmWebError
from web.engineapi.client import EngineAPIError, EngineAPIClient
from web.feishu import send_task_card
from web.settings_store import SettingsError, SettingsStore
from web.tm_store import (
    ROLE_VALUES,
    TMStore,
    TMWebError,
    proposal_display_id,
    task_display_id,
)

BASE = Path(__file__).resolve().parent
_REPO_ROOT = BASE.parent
_DOTENV_PATH = _REPO_ROOT / ".env"

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

# task_event 十二态 -> 中文标签（详设-v0.6 §15.7 流转历史卡时间线展示；
# 与 models/tm.py TaskEvent CHECK 十二态一一对应，未知值回退原 event_type）
EVENT_LABELS = {
    "created": "已创建",
    "approved": "已批准",
    "started": "已开始",
    "completed": "已完成",
    "voided": "已作废",
    "blocked": "已阻塞",
    "unblocked": "已解除阻塞",
    "derived": "已派生",
    "updated": "已编辑",
    "suggested": "AI 建议",
    "transferred": "已流转",
    "disagreed": "分歧留痕",
}

# 来源徽章 = domain（决策 16：任务来源标徽章，一眼可见"这任务是谁提的"）
# v0.5 §3.1：补 scrape 徽章
DOMAIN_META: dict[str, dict[str, str]] = {
    "crm": {"label": "CRM", "cls": "bg-indigo"},
    "erp": {"label": "ERP", "cls": "bg-purple"},
    "seo": {"label": "SEO", "cls": "bg-teal"},
    "scrape": {"label": "扒图", "cls": "bg-orange"},
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
    "seo_keyword_chain": {
        "fields": [
            {"name": "keywords", "label": "关键词（逗号分隔，最多 8 个）", "type": "text", "required": True},
            {"name": "page_size", "label": "每页商品数", "type": "number", "required": False},
        ]
    },
    "seo_optimize_chain": {
        "fields": [
            {"name": "title", "label": "商品标题", "type": "text", "required": False},
            {"name": "tags", "label": "标签（逗号分隔）", "type": "text", "required": False},
            {"name": "description", "label": "商品描述", "type": "textarea", "required": False},
            {"name": "target_keywords", "label": "目标关键词（逗号分隔）", "type": "text", "required": False},
            {"name": "playbook_key", "label": "Playbook", "type": "select", "required": False,
             "options": ["", "wall_art", "digital", "jewelry", "clothing", "home_candle", "personalized", "general"]},
        ]
    },
    "seo_healthcheck_chain": {
        "fields": [
            {"name": "keywords", "label": "体检关键词（逗号分隔，留空读设置）", "type": "text", "required": False},
        ]
    },
    "scrape_suggest_chain": {
        "fields": [
            {"name": "image_ids", "label": "图片 ID（逗号分隔）", "type": "text", "required": True},
        ]
    },
    # v0.6 批 7（详设 §15.2）：手动补传链（素材库「上传网盘」按钮触发，页面展示 input 说明）
    "scrape_upload_chain": {
        "fields": [
            {"name": "link_ids", "label": "链接记录 ID（逗号分隔）", "type": "text", "required": True},
        ]
    },
    # v0.6 批 4：下载链（贴链接立即扒走 /scrape/run；定时扒走调度器 input 模板，
    # 页面触发面板展示 input 说明）
    "scrape_download_chain": {
        "fields": [
            {"name": "urls", "label": "商品链接（每行一个）", "type": "textarea", "required": False},
            {"name": "batch_id", "label": "批次 ID", "type": "text", "required": False},
            {"name": "from_queue", "label": "定时队列（勾选从队列读）", "type": "checkbox", "required": False},
        ]
    },
    "crm_image_chain": {
        "fields": [
            {"name": "message_image_ids", "label": "图片 ID（逗号分隔）", "type": "text", "required": True},
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
    "seo_keyword_chain": "关键词研究链",
    "seo_optimize_chain": "SEO 优化链",
    "seo_healthcheck_chain": "listing 体检链",
    "scrape_suggest_chain": "扒图选品链",
    "scrape_download_chain": "定时扒图",  # v0.6 批 4：拆两链后下载链（种子行展示名）
    "scrape_upload_chain": "扒图补传链",  # v0.6 批 7（详设 §15.2）：历史素材夸克上传
    "crm_image_chain": "对话图片链",
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
    # v0.7：SKU 建档一级菜单（workspace 之后、tm 之前，详设 §4.3）
    {"id": "skus", "name": "SKU 建档", "icon": "ti ti-box", "href": "/skus",
     "desc": "货档案建档/配方/图库/库存（v0.7）"},
    {"id": "tm", "name": "任务中心", "icon": "ti ti-list-check", "href": "/tasks",
     "desc": "AI 提案转任务、人工处理回流（v0.2 首个真实模块）"},
    {"id": "crm", "name": "CRM", "icon": "ti ti-message-circle", "href": "/crm",
     "desc": "客户对话翻译 / 快照 / 待办（v0.3 已落地）",
     "children": [
         {"id": "crm-customers", "name": "客户列表", "icon": "ti ti-users", "href": "/crm"},
         {"id": "crm-translate", "name": "对话翻译", "icon": "ti ti-language", "href": "/translate"},
         {"id": "crm-follow", "name": "跟进待办", "icon": "ti ti-list", "href": "/tasks"},
     ]},
    {"id": "erp", "name": "ERP 增强", "icon": "ti ti-box", "href": "/modules/erp",
     "desc": "库存 / 补货建议 / 异常预警（v0.6，只读 NocoBase 视图）",
     "children": [
         {"id": "erp-stock", "name": "库存", "icon": "ti ti-box", "href": "/modules/erp"},
         {"id": "erp-replenish", "name": "补货建议", "icon": "ti ti-shopping-cart", "href": "/modules/erp"},
         {"id": "erp-alert", "name": "异常预警", "icon": "ti ti-alert-triangle", "href": "/modules/erp"},
     ]},
    # v0.5 §3.1：SEO 一级菜单下挂二级（扒图第一/关键词研究/SEO 优化/listing 体检）
    # 一级「扒图」菜单移除
    {"id": "seo", "name": "SEO", "icon": "ti ti-chart-line", "href": "/seo/keywords",
     "desc": "关键词研究 / 标题优化 / 体检（v0.5，数据源 eHunt）",
     "children": [
         {"id": "seo-scrape", "name": "扒图", "icon": "ti ti-photo", "href": "/scrape"},
         {"id": "seo-keywords", "name": "关键词研究", "icon": "ti ti-keyboard", "href": "/seo/keywords"},
         {"id": "seo-optimize", "name": "SEO 优化", "icon": "ti ti-edit", "href": "/seo/optimize"},
         {"id": "seo-healthcheck", "name": "listing 体检", "icon": "ti ti-chart-line", "href": "/seo/healthcheck"},
     ]},
    # ---- 设置一级菜单（v0.4 批 2a，详设 §7.1；复核反馈 2026-09-01：三分组——
    # 基础设置/AI 设置/系统设置，所有设置项归组，分组标题不可点）----
    {"id": "settings", "name": "设置", "icon": "ti ti-settings", "href": "/settings/params",
     "children": [
         {"id": "settings-basic", "name": "基础设置", "icon": "ti ti-building-store",
          "href": "", "group": True,
          "children": [
              {"id": "settings-shops", "name": "店铺管理", "icon": "ti ti-building-store",
               "href": "/settings/shops"},
          ]},
         {"id": "settings-ai", "name": "AI 设置", "icon": "ti ti-brand-openai",
          "href": "", "group": True,
          "children": [
              {"id": "settings-ai-key", "name": "API 密钥", "icon": "ti ti-key",
               "href": "/settings/ai/api-key"},
              # v0.5 §3.5：模型选择页 placeholder 移除改真页
              {"id": "settings-ai-model", "name": "模型选择", "icon": "ti ti-brain",
               "href": "/settings/ai/model"},
              {"id": "settings-ai-style", "name": "风格指南术语表", "icon": "ti ti-file-text",
               "href": "/settings/ai/style", "placeholder": True},
          ]},
         {"id": "settings-system", "name": "系统设置", "icon": "ti ti-settings",
          "href": "", "group": True,
          "children": [
              {"id": "settings-schedule", "name": "定时任务", "icon": "ti ti-clock",
               "href": "/settings/schedule"},
              {"id": "settings-notify", "name": "通知配置", "icon": "ti ti-bell",
               "href": "/settings/notify"},
              # v0.5 §3.5：新增「扒图设置」叶子
              {"id": "settings-scrape", "name": "扒图设置", "icon": "ti ti-photo",
               "href": "/settings/scrape"},
              {"id": "settings-params", "name": "系统参数", "icon": "ti ti-adjustments",
               "href": "/settings/params"},
          ]},
     ]},
]


# ---- 视图助手 ----

def _domain_meta(domain: str) -> dict[str, str]:
    return DOMAIN_META.get(domain, {"label": domain or "其他", "cls": "bg-secondary"})


def _task_view(
    t: Task,
    *,
    parent_title: str | None = None,
    child_count: int = 0,
    step_total: int = 0,
    step_done: int = 0,
) -> dict[str, Any]:
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
        "tags": list(t.tags or []),              # v0.3 决策 25：标签展示
        "ai_suggestion": t.ai_suggestion,        # v0.3 决策 27：AI 下一步建议
        "step_total": step_total,                # 复核反馈 #6：步骤 N/M
        "step_done": step_done,
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


def _dt_str(dt: Any) -> str:
    """datetime -> 'YYYY-MM-DD HH:MM' 展示形（None -> 空串；任务详情页头部/时间线）。"""
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")


def _source_cell(value: Any) -> str:
    """task.source JSONB 逐键展示的单元格文本：标量原样，dict/list 转 JSON 文本。"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _event_view(e: TaskEvent) -> dict[str, Any]:
    """task_event 时间线条目视图（详设-v0.6 §15.7）：中文标签 + from→to 状态 + 备注。

    detail JSONB 预格式化为中文可读文本（updated/suggested/transferred/disagreed
    事件的结构化详情摘要，模板直接展示；不经模板 tojson 以免非 ASCII 转义）。"""
    detail_text = ""
    if isinstance(e.detail, (dict, list)):
        detail_text = json.dumps(e.detail, ensure_ascii=False)
    return {
        "event_type": e.event_type,
        "label": EVENT_LABELS.get(e.event_type, e.event_type),
        "from_status": e.from_status,
        "from_label": STATUS_LABEL.get(e.from_status or "", e.from_status or ""),
        "to_status": e.to_status,
        "to_label": STATUS_LABEL.get(e.to_status or "", e.to_status or ""),
        "actor": e.actor,
        "note": e.note,
        "detail_text": detail_text,
        "created_at": _dt_str(e.created_at),
    }


def _task_detail_view(t: Task) -> dict[str, Any]:
    """任务详情页视图（详设-v0.6 §15.7 头部）：_task_view 之上补全字段
    （created_at/updated_at/done_at/source_type 中文），detail 全文透传。"""
    view = _task_view(t)
    view.update(
        {
            "created_by": t.created_by,
            "created_at": _dt_str(t.created_at),
            "updated_at": _dt_str(t.updated_at),
            "done_at": _dt_str(t.done_at),
            "source_type_label": "AI 提案批准" if t.source_type == "ai" else "人工创建",
            "blocked_reason": t.blocked_reason,
            "result_note": t.result_note,
        }
    )
    return view


def _safe_return_to(raw: str) -> str:
    """POST 后返回地址白名单：只允许站内 /tasks 前缀（详情页操作 303 回详情）。
    防开放重定向：'//' 开头（协议相对）与站外一律回退任务中心。"""
    raw = (raw or "").strip()
    if raw.startswith("/tasks") and not raw.startswith("//"):
        return raw
    return "/tasks"


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


def _hx_redirect(
    path: str,
    *,
    msg: str | None = None,
    err: str | None = None,
) -> RedirectResponse:
    """HTMX 局部刷新：303 重定向让 HTMX 跟随并局部替换。"""
    params: dict[str, str] = {}
    if msg:
        params["msg"] = msg
    if err:
        params["err"] = err
    qs = urlencode(params)
    url = f"{path}?{qs}" if qs else path
    return RedirectResponse(url, status_code=303)


def _check_env_key(name: str) -> bool:
    """检查 .env 中是否已配置指定密钥（R20：不回显值，只返回是否已配置）。"""
    from dotenv import dotenv_values
    values = dotenv_values(_DOTENV_PATH)
    val = (values.get(name) or "").strip()
    return bool(val)


def _time_to_cron(trigger_time: str) -> str:
    """'HH:MM' -> cron 5 段 'M H * * *'（分 时 日 月 周）；非法输入回退默认 07:00。"""
    try:
        hour, minute = trigger_time.strip().split(":")
        h, m = int(hour), int(minute)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return "0 7 * * *"
        return f"{m:02d} {h:02d} * * *"
    except (ValueError, TypeError):
        return "0 7 * * *"


def _scrape_time_cron(trigger_time: str) -> str:
    """'HH:MM' → 定时扒图链 cron（不补前导零，照详设-v0.6 §8：'08:00' → '0 8 * * *'）。

    v0.4 的 _time_to_cron 补零形态（'00 08 * * *'）被 test_schedule_create_sends_daily_cron
    锁住，此处按 v0.6 详设口径独立实现；调用方已先做 HH:MM 校验。
    """
    h, m = (int(x) for x in trigger_time.split(":"))
    return f"{m} {h} * * *"


# ---- 素材库视图助手（v0.6 批 2：链接记录列表/详情 + 图片网格展示）----

_SCRAPE_SOURCE_BADGE = {"xhs": "📕", "xianyu": "🐟", "http": "🔗"}
_SCRAPE_SOURCE_LABEL = {"xhs": "小红书", "xianyu": "闲鱼", "http": "HTTP"}
_LINK_STATUS_LABEL = {
    "pending": "待处理",
    "downloading": "下载中",
    "done": "已完成",
    "failed": "失败",
}
_LINK_STATUS_BADGE = {
    "pending": "bg-yellow-lt",
    "downloading": "bg-blue-lt",
    "done": "bg-green-lt",
    "failed": "bg-danger-lt",
}
_SOURCE_MARK_META = {
    "scraped": ("扒图", "bg-blue-lt"),
    "selfshot": ("自拍", "bg-green-lt"),
    "ai_generated": ("AI", "bg-purple-lt"),
    "authorized": ("授权", "bg-teal-lt"),
}

# 网盘上传状态徽章（批 7，详设 §15.2：未上传/上传中/已上传/失败）
_NETDISK_STATUS_LABEL = {
    "none": "未上传",
    "pending": "上传中",
    "uploaded": "已上传",
    "failed": "上传失败",
}
_NETDISK_STATUS_BADGE = {
    "none": "bg-secondary-lt",
    "pending": "bg-blue-lt",
    "uploaded": "bg-green-lt",
    "failed": "bg-danger-lt",
}


def _truncate(s: object, n: int) -> str:
    """字符串截断展示（元数据 desc/链接摘要用）。"""
    text = str(s or "")
    return text if len(text) <= n else text[:n] + "…"


def _link_view(l: dict[str, Any]) -> dict[str, Any]:
    """链接记录行视图（素材库列表/详情共用）。"""
    src = l.get("source") or ""
    tags = list(l.get("tags") or [])
    desc = l.get("desc") or ""
    return {
        "id": l["id"],
        "url": l.get("url") or "",
        "summary": _truncate(l.get("url") or "", 60),
        "source": src,
        "source_badge": _SCRAPE_SOURCE_BADGE.get(src, "🔗"),
        "source_label": _SCRAPE_SOURCE_LABEL.get(src, src),
        "status": l.get("status") or "",
        "status_label": _LINK_STATUS_LABEL.get(l.get("status") or "", l.get("status") or ""),
        "status_badge": _LINK_STATUS_BADGE.get(l.get("status") or "", "bg-secondary-lt"),
        "image_count": l.get("image_count") or 0,
        "desc": _truncate(desc, 40),
        "tags": list(tags),
        "tags_total": len(tags),
        "author_id": l.get("author_id") or "",
        "batch_id": l.get("batch_id") or "",
        "created_at": l.get("created_at"),
        "error_note": l.get("error_note") or "",
        "degraded_note": l.get("degraded_note") or "",
        # 批 6（详设 §15.1）：链接文件夹相对路径（相对 scrape.storage_dir），
        # 页面展示「本地文件夹」；为空 = 未落盘（pending/failed）
        "storage_dir": l.get("storage_dir") or "",
        # 批 7（详设 §15.2）：夸克网盘上传状态徽章 + 分享链接 + 补传按钮条件
        # （status=done 且 netdisk_status≠uploaded → 显示「上传网盘」）
        "netdisk_status": l.get("netdisk_status") or "none",
        "netdisk_label": _NETDISK_STATUS_LABEL.get(
            l.get("netdisk_status") or "none", "未上传"
        ),
        "netdisk_cls": _NETDISK_STATUS_BADGE.get(
            l.get("netdisk_status") or "none", "bg-secondary-lt"
        ),
        "netdisk_url": l.get("netdisk_url") or "",
        "can_upload": (l.get("status") == "done")
        and (l.get("netdisk_status") or "none") != "uploaded",
    }


def _image_view(i: dict[str, Any]) -> dict[str, Any]:
    """图片行视图（详情网格：宽高/水印/来源标记徽章）。"""
    mark = i.get("source_mark") or "scraped"
    label, cls = _SOURCE_MARK_META.get(mark, (mark, "bg-secondary-lt"))
    return {
        "id": i["id"],
        "url": i.get("url") or "",
        "source": i.get("source") or "",
        "source_mark": mark,
        "source_mark_label": label,
        "source_mark_cls": cls,
        "width": i.get("width"),
        "height": i.get("height"),
        "watermark": bool(i.get("watermark")),
        "local_path": i.get("local_path") or "",
        "created_at": i.get("created_at"),
    }


# 系统参数键清单（详设 §7.3：七键；每个键独立存/独立提交）
_PARAM_DEFS: list[dict[str, Any]] = [
    {"key": "crm.follow_up_days", "description": "客户跟进逾期天数阈值（int）", "type": "int", "default": 5},
    {"key": "crm.page_size", "description": "客户列表每页条数（int）", "type": "int", "default": 20},
    {"key": "schedule.default_time", "description": "定时链默认触发时间（HH:MM）", "type": "time", "default": "07:00"},
    {"key": "engine.max_attempts", "description": "LLM 重试次数（int）", "type": "int", "default": 2},
    {"key": "engine.timeout_s", "description": "LLM 单次超时秒（float）", "type": "float", "default": 30},
    {"key": "engine.backoff_cap", "description": "LLM 退避封顶秒（int）", "type": "int", "default": 30},
    {"key": "notify.feishu_enabled", "description": "飞书通知总开关（bool）", "type": "bool", "default": True},
]


def _store(request: Request) -> TMStore:
    return request.app.state.tm_store


def _engine_client(request: Request) -> EngineAPIClient:
    return request.app.state.engine_client_factory()


def _settings_store(request: Request) -> SettingsStore:
    return request.app.state.settings_store


def _resolve_biz_engine(request: Request):
    """v0.7 SKU 页路由获取业务库 engine（优先用 TMStore 同库 engine）。"""
    # 优先使用已注入的 TMStore 的 engine（测试和生产一致）
    tm = getattr(request.app.state, "tm_store", None)
    if tm is not None and hasattr(tm, "_engine"):
        return tm._engine
    # 回退：从 api_biz 的已解析 engine
    from web.api_biz import _resolve_engine  # noqa: F811
    return _resolve_engine()


# ---- 应用工厂（测试构造注入：tm_store / engine_client_factory）----

def create_app(
    *,
    tm_store: TMStore | None = None,
    crm_store: CRMStore | None = None,
    settings_store: SettingsStore | None = None,
    engine_client_factory: Callable[[], EngineAPIClient] | None = None,
) -> FastAPI:
    """建 web 应用；依赖可注入（R12：测试注入嵌入式 PG store + MockTransport 引擎桩）。"""

    @asynccontextmanager
    async def _lifespan(app_: FastAPI):
        if app_.state.tm_store is None:
            app_.state.tm_store = TMStore.from_env()  # 生产：.env 连接串
        if app_.state.crm_store is None:
            app_.state.crm_store = CRMStore.from_env()
        if app_.state.settings_store is None:
            app_.state.settings_store = SettingsStore.from_env()
        yield
        await app_.state.tm_store.dispose()
        await app_.state.crm_store.dispose()
        await app_.state.settings_store.dispose()

    app = FastAPI(title="刘全 · 综合智能运营系统（v0.2）", lifespan=_lifespan)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")
    # v0.5 批 4/5 新路由（/scrape、/seo/*）用 request.app.state.templates 渲染；
    # 老路由走闭包 templates。注入 state 使两种方式一致可用（缺注入 = 新路由 500
    # 'State' object has no attribute 'templates'，页面验收回归时补测）。
    app.state.templates = templates
    app.state.tm_store = tm_store
    app.state.crm_store = crm_store
    app.state.settings_store = settings_store
    app.state.engine_client_factory = engine_client_factory or (lambda: EngineAPIClient())

    # ---- 设置页登录保护（决策 37-5：所有「登录用户」可访问；未登录拦到 /login）----
    @app.middleware("http")
    async def _settings_login_guard(request: Request, call_next):
        path = request.url.path
        if (path.startswith("/settings") or path.startswith("/skus")) and not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

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

    # ---- /scrape 扒图页（v0.6 批 2 重构：贴链接 + 素材库一页，详设-v0.6 §3.1）----

    @app.get("/scrape")
    async def scrape_page(
        request: Request,
        source: str = "",
        status: str = "",
        page: str = "1",
    ):
        """扒图页：区块 A 贴链接（立即扒 + 定时队列管理）+ 区块 B 素材库。

        筛选：来源（xhs/xianyu/http）· 状态（全部/进行中/完成/失败，GET 参数）；
        「进行中」= pending + downloading（详设 §3.1 筛选口径）。
        批 8（详设 §15.3）：定时队列管理从设置页移回本页——传入 queue（url 列表）+
        schedule_time（定时默认时间提示）+ upload_default（同步上传复选框初始值，
        设置键 netdisk.upload_default，缺省 true）。
        """
        from datetime import time as _dtime

        from web import scrape_store

        try:
            page_no = max(1, int(page))
        except ValueError:
            page_no = 1
        source_f = source if source in ("xhs", "xianyu", "http") else None
        status_f = status if status in ("pending", "downloading", "done", "failed") else None
        status_in = ["pending", "downloading"] if status == "processing" else None
        page_size = 20
        links = await scrape_store.get_links(
            source=source_f,
            status=status_f,
            status_in=status_in,
            limit=page_size,
            offset=(page_no - 1) * page_size,
        )
        total = await scrape_store.count_links(
            source=source_f, status=status_f, status_in=status_in
        )
        store = _settings_store(request)
        queue = await scrape_store.get_link_queue(settings=store)
        raw_time = await store.get("scrape.schedule_time", "07:00")
        schedule_time = (
            raw_time.strftime("%H:%M") if isinstance(raw_time, _dtime) else str(raw_time)
        )
        upload_default = bool(await store.get("netdisk.upload_default", True))
        return templates.TemplateResponse(
            request,
            "scrape/index.html",
            _ctx(
                request,
                "seo-scrape",
                links=[_link_view(l) for l in links],
                total=total,
                page=page_no,
                page_size=page_size,
                pages=(total + page_size - 1) // page_size,
                filters={"source": source, "status": status},
                queue=queue,
                queue_count=len(queue),
                schedule_time=schedule_time,
                upload_default=upload_default,
                pending_proposal_count=0,
            ),
        )

    @app.get("/scrape/links/{link_id}")
    async def scrape_link_detail(request: Request, link_id: int):
        """链接详情页：状态/元数据/error_note/degraded_note + 本地文件夹提示 + 图片网格。"""
        from web import scrape_store

        detail = await scrape_store.get_link_by_id(link_id)
        if detail is None:
            return RedirectResponse("/scrape", status_code=303)
        return templates.TemplateResponse(
            request,
            "scrape/link_detail.html",
            _ctx(
                request,
                "seo-scrape",
                link=_link_view(detail["link"]),
                images=[_image_view(i) for i in detail["images"]],
                msg=request.query_params.get("msg", ""),
                err=request.query_params.get("err", ""),
            ),
        )

    # ---- 扒图页 JSON 接口（页面 JS 用；照 crm 粘贴链模式）----

    @app.post("/scrape/run")
    async def scrape_run(request: Request):
        """扒图页「立即扒」（详设-v0.6 §3.1/§7 + §15.2 批 7）：{urls: [...],
        upload_netdisk?: bool} → web 生成 batch_id → create_task(scrape_download_chain,
        {urls, batch_id, from_queue: false, upload_netdisk})。

        upload_netdisk 缺省 true（扒图页「同步上传网盘」复选框默认勾选，用户拍板
        §15.2；设置键 netdisk.upload_default 归批 8 A74）。
        引擎侧 422/404 错误透传友好提示（下载链批 4 才修通，本批提交失败给友好错误）。
        """
        import uuid

        from web.engineapi.client import EngineAPIError as _EngineAPIError

        if not request.cookies.get("role"):
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "请求体非 JSON"}, status_code=400)
        raw = data.get("urls")
        if isinstance(raw, str):
            urls = [u.strip() for u in raw.replace(",", "\n").splitlines() if u.strip()]
        elif isinstance(raw, list):
            urls = [str(u).strip() for u in raw if str(u).strip()]
        else:
            urls = []
        if not urls:
            return JSONResponse({"ok": False, "error": "链接为空"}, status_code=400)
        # 批 8（详设 §15.2/§15.6 批 8 技术定）：服务端缺省 upload_netdisk 读设置键
        # netdisk.upload_default（缺省 true）——JS 显式提交勾选值时以显式值为准
        upload_default = bool(
            await _settings_store(request).get("netdisk.upload_default", True)
        )
        upload_netdisk = bool(data.get("upload_netdisk", upload_default))
        batch_id = uuid.uuid4().hex
        try:
            async with _engine_client(request) as client:
                resp = await client.create_task(
                    "scrape_download_chain",
                    {
                        "urls": urls,
                        "batch_id": batch_id,
                        "from_queue": False,
                        "upload_netdisk": upload_netdisk,
                    },
                    request.cookies.get("role", "运营"),
                )
        except _EngineAPIError as exc:
            detail = exc.detail if exc.detail is not None else str(exc)
            return JSONResponse(
                {"ok": False, "error": f"引擎调用失败（{exc.status_code}）：{detail}"},
                status_code=502,
            )
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"引擎调用失败：{exc}"}, status_code=502
            )
        return JSONResponse(
            {"ok": True, "task_id": resp.get("task_id"), "batch_id": batch_id}
        )

    async def _scrape_queue_fragment(
        request: Request,
        *,
        queue: list,
        schedule_time: str,
        msg: str = "",
        err: str = "",
    ):
        """扒图页定时队列卡片片段（批 8，详设 §15.3：加入/清空后局部刷新队列块）。"""
        return templates.TemplateResponse(
            request,
            "scrape/_queue_card.html",
            _ctx(
                request,
                "seo-scrape",
                queue=queue,
                queue_count=len(queue),
                schedule_time=schedule_time,
                msg=msg,
                err=err,
            ),
        )

    @app.post("/scrape/queue")
    async def scrape_queue(request: Request):
        """扒图页「定时队列」管理（批 8，详设 §15.3：原 /settings/scrape/queue 迁来）。

        action=add：urls（多行/列表）按 normalized_url 去重追加（同作品不同
        xsec_token 只留一条）；action=clear：清空队列。队列存设置键
        scrape.link_queue（json 数组），语义照批 2/批 4 不变（定时链
        from_queue 读队列 → 处理 → 链完成清空）。

        响应：HTMX 头（hx-request=true）→ 队列卡片片段 HTML（局部刷新）；
        非 HTMX → JSON {ok, count, msg}（扒图页 JS fetch 用；错误 400）。
        """
        from web import scrape_store

        if not request.cookies.get("role"):
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        store = _settings_store(request)
        is_hx = bool(request.headers.get("hx-request"))

        # 定时默认时间（片段提示用；读设置键 scrape.schedule_time，缺省 07:00）
        from datetime import time as _dtime

        raw_time = await store.get("scrape.schedule_time", "07:00")
        schedule_time = (
            raw_time.strftime("%H:%M")
            if isinstance(raw_time, _dtime)
            else str(raw_time)
        )

        async def _fragment(queue: list, msg: str = "", err: str = "") -> Any:
            return await _scrape_queue_fragment(
                request,
                queue=queue,
                schedule_time=schedule_time,
                msg=msg,
                err=err,
            )

        content_type = str(request.headers.get("content-type", "")).split(";")[0].strip().lower()
        if content_type == "application/json":
            data = await request.json()
            action = str(data.get("action", "add"))
            raw = data.get("urls", "")
            if isinstance(raw, list):
                urls = [str(u).strip() for u in raw if str(u).strip()]
            else:
                urls = [u.strip() for u in str(raw).replace(",", "\n").splitlines() if u.strip()]
        else:
            form = await request.form()
            action = str(form.get("action", "add"))
            urls = [u.strip() for u in str(form.get("urls", "")).splitlines() if u.strip()]

        queue = await scrape_store.get_link_queue(settings=store)
        if action == "clear":
            queue = []
            await scrape_store.set_link_queue([], settings=store)
            msg = "定时队列已清空"
            err = ""
        else:
            if not urls:
                if is_hx:
                    return await _fragment(queue, err="请先粘贴链接")
                return JSONResponse(
                    {"ok": False, "error": "请先粘贴链接"}, status_code=400
                )
            # 按 normalized_url 去重追加（同作品不同 xsec_token 只留一条）
            seen = {scrape_store.normalize_link_url(u) for u in queue}
            added = 0
            for u in urls:
                norm = scrape_store.normalize_link_url(u)
                if norm not in seen:
                    queue.append(u)
                    seen.add(norm)
                    added += 1
            await scrape_store.set_link_queue(queue, settings=store)
            msg = f"已加入 {added} 条（去重后共 {len(queue)} 条）"
            err = ""
        if is_hx:
            return await _fragment(queue, msg=msg, err=err)
        return JSONResponse({"ok": True, "count": len(queue), "msg": msg})

    @app.post("/scrape/links/{link_id}/upload")
    async def scrape_link_netdisk_upload(request: Request, link_id: int):
        """素材库链接行/详情页「上传网盘」按钮（历史补传，详设 §15.2 批 7）：
        守卫（status=done 且 netdisk_status≠uploaded）→ PATCH netdisk_status=pending
        （页面「上传中」）→ create_task(scrape_upload_chain, {link_ids: [id]})。

        返回 JSON {ok, task_id}；引擎 502/守卫失败 400/409 透传友好错误。
        """
        from web import scrape_store
        from web.engineapi.client import EngineAPIError as _EngineAPIError

        if not request.cookies.get("role"):
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        detail = await scrape_store.get_link_by_id(link_id)
        if detail is None:
            return JSONResponse({"ok": False, "error": "链接记录不存在"}, status_code=404)
        link = detail["link"]
        if link.get("status") != "done":
            return JSONResponse(
                {"ok": False, "error": "仅已完成的链接可补传网盘"},
                status_code=409,
            )
        if (link.get("netdisk_status") or "none") == "uploaded":
            return JSONResponse(
                {"ok": False, "error": "该链接已上传网盘，无需补传"},
                status_code=409,
            )
        # 页面「上传中」徽章（引擎补传链完成后回填 uploaded/failed）
        await scrape_store.update_link(link_id, netdisk_status="pending")
        try:
            async with _engine_client(request) as client:
                resp = await client.create_task(
                    "scrape_upload_chain",
                    {"link_ids": [link_id]},
                    request.cookies.get("role", "运营"),
                )
        except _EngineAPIError as exc:
            detail_msg = exc.detail if exc.detail is not None else str(exc)
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"引擎调用失败（{exc.status_code}）：{detail_msg}",
                },
                status_code=502,
            )
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"引擎调用失败：{exc}"}, status_code=502
            )
        return JSONResponse(
            {"ok": True, "task_id": resp.get("task_id"), "link_id": link_id}
        )

    @app.post("/scrape/suggest")
    async def scrape_suggest(request: Request):
        """扒图页：勾选图片 → 批量生成选品建议（scrape_suggest_chain，JSON；功能批 4）。"""
        if not request.cookies.get("role"):
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        try:
            data = await request.json()
        except Exception:
            return {"ok": False, "error": "请求体非 JSON"}
        image_ids = [str(i) for i in (data.get("image_ids") or [])]
        if not image_ids:
            return {"ok": False, "error": "未选择图片"}
        client = _engine_client(request)
        try:
            resp = await client.create_task(
                "scrape_suggest_chain",
                {"image_ids": image_ids, "target_keywords": "", "mode": "batch"},
                request.cookies.get("role", "运营"),
            )
            return {"ok": True, "task_id": resp.get("task_id")}
        except Exception as exc:
            return {"ok": False, "error": f"引擎调用失败：{exc}"}

    @app.get("/scrape/thumbnail/{image_id}")
    async def scrape_thumbnail(request: Request, image_id: int):
        """返回图片缩略图（从本地文件读取；批 6：local_path 为相对 storage_dir
        路径时经设置键 scrape.storage_dir 解析，绝对路径兼容存量）。

        2026-09-03 集成验收修复（复核反馈 1）：image_id 参数必须 int 注解——URL 路径参数
        默认 str，而 scrape_store.get_image_by_id 绑定 BIGINT 列（asyncpg 拒绝字符串 →
        500「invalid input for query argument」，缩略图全占位图）；int 注解让 FastAPI 转换。"""
        from pathlib import Path
        from web import scrape_store

        img = await scrape_store.get_image_by_id(image_id)
        if not img or not img.get("local_path"):
            return Response("Not found", status_code=404, media_type="text/plain")
        path = Path(img["local_path"])
        if not path.is_absolute():
            # 批 6（详设 §15.1）：归集后 local_path = 链接文件夹内相对路径
            storage_root = Path(
                str(await _settings_store(request).get("scrape.storage_dir", "/opt/liuquan/scrape/"))
            )
            path = storage_root / path
        if not path.exists():
            return Response("File not found", status_code=404, media_type="text/plain")
        import mimetypes
        ct = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        return FileResponse(str(path), media_type=ct)

    @app.post("/scrape/open-folder")
    async def scrape_open_folder(request: Request):
        """复核反馈 2（2026-09-03）：打开本地文件夹（刘全所在机器的文件管理器）。

        仅限存储目录内路径：kind=root → storage_dir 根；kind=link + link_id →
        storage_dir/{link.storage_dir}。服务器有桌面环境（Linux xdg-open / mac open /
        Windows explorer）时打开文件管理器；无桌面/命令缺失 → 返回路径文本提示不报错。
        安全：路径必须 resolve 后位于 storage_dir 内（防任意路径打开）；subprocess 列表参数无 shell。
        """
        import platform
        import subprocess
        from pathlib import Path

        from web import scrape_store

        if not request.cookies.get("role"):
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        try:
            data = await request.json()
        except Exception:
            data = {}
        kind = data.get("kind", "")
        storage_root = Path(
            str(await _settings_store(request).get("scrape.storage_dir", "/opt/liuquan/scrape/"))
        )
        target: Path | None = None
        if kind == "root":
            target = storage_root
        elif kind == "link":
            link_id = data.get("link_id")
            try:
                link_id = int(link_id)
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "链接 id 无效"}, status_code=400)
            link = await scrape_store.get_link_by_id(link_id)
            link_row = (link or {}).get("link") or link
            if not link_row or not link_row.get("storage_dir"):
                return JSONResponse({"ok": False, "error": "链接无本地文件夹（未成功下载）"}, status_code=404)
            target = storage_root / str(link_row["storage_dir"])
        if target is None:
            return JSONResponse({"ok": False, "error": "未知 kind"}, status_code=400)

        try:
            target_resolved = target.resolve()
            root_resolved = storage_root.resolve()
            target_resolved.relative_to(root_resolved)  # 防越界
        except ValueError:
            return JSONResponse({"ok": False, "error": "路径越界被拒"}, status_code=403)
        if not target_resolved.is_dir():
            return JSONResponse(
                {"ok": False, "error": f"本地文件夹不存在（未下载或已被清理）：{target_resolved}"},
                status_code=404,
            )

        sysname = platform.system()
        if sysname == "Linux":
            cmd = ["xdg-open"]
        elif sysname == "Darwin":
            cmd = ["open"]
        elif sysname == "Windows":
            cmd = ["explorer"]
        else:
            cmd = []
        if cmd:
            try:
                subprocess.Popen(cmd + [str(target_resolved)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return JSONResponse({"ok": True, "message": "已尝试打开本地文件夹", "path": str(target_resolved)})
            except Exception as exc:  # noqa: BLE001 - 无桌面/命令缺失降级提示
                return JSONResponse(
                    {"ok": True, "message": f"无法打开文件管理器（{exc}），路径如下可手动打开", "path": str(target_resolved)}
                )
        return JSONResponse(
            {"ok": True, "message": "当前系统不支持自动打开，路径如下可手动打开", "path": str(target_resolved)}
        )

    @app.get("/crm/thumbnail/{image_id}")
    async def crm_thumbnail(request: Request, image_id: int):
        """返回 CRM 对话图片缩略图（从本地文件读取）。"""
        from pathlib import Path
        import mimetypes

        from models.crm import MessageImage

        async with AsyncSession(request.app.state.crm_store._engine) as session:
            img = await session.get(MessageImage, image_id)
            if not img or not img.local_path:
                return Response("Not found", status_code=404, media_type="text/plain")
            local_path = img.local_path

        path = Path(local_path)
        if not path.exists():
            return Response("File not found", status_code=404, media_type="text/plain")

        ct = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        return FileResponse(str(path), media_type=ct)

    @app.get("/modules/{module_id}")
    def module_page(request: Request, module_id: str):
        module = next((m for m in MODULES if m["id"] == module_id), None)
        if module is None:
            return RedirectResponse("/tasks")
        return templates.TemplateResponse(
            request, "module.html", _ctx(request, module_id, module=module)
        )

    # ---- v0.7 SKU 建档页（详设 §4.3 / §7）----

    @app.get("/skus")
    async def sku_list(
        request: Request,
        product_name: str = "",
        kind: str = "",
        keyword: str = "",
        msg: str = "",
        err: str = "",
    ):
        """档案列表页（按商品名分组 + 新建入口）。"""
        from web.catalog_store import list_items

        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        biz = _resolve_biz_engine(request)
        async with async_sessionmaker(biz, expire_on_commit=False)() as session:
            result = await list_items(
                session,
                product_name=product_name or None,
                kind=kind or None,
                keyword=keyword or None,
            )
        # 按 product_name 分组
        groups: dict[str, list] = {}
        for item in result["items"]:
            pn = item["product_name"] or "(无商品名)"
            groups.setdefault(pn, []).append(item)
        return templates.TemplateResponse(
            request,
            "skus/index.html",
            _ctx(
                request,
                "skus",
                groups=groups,
                total=result["total"],
                msg=msg,
                err=err,
            ),
        )

    @app.get("/skus/new")
    async def sku_new(request: Request, image_ids: str = ""):
        """建档表单页。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        from web.catalog_store import list_items
        # 已有档案列表（combo 引用子件用）
        biz = _resolve_biz_engine(request)
        async with async_sessionmaker(biz, expire_on_commit=False)() as session:
            items_result = await list_items(session, page_size=1000)
        # 图片列表（素材库勾选用）
        from web import scrape_store
        images = await scrape_store.get_images(limit=1000)
        # 仓库列表
        from web.inventory_service import list_warehouses
        async with async_sessionmaker(biz, expire_on_commit=False)() as session:
            warehouses = await list_warehouses(session)
        preselected_ids = [int(x) for x in image_ids.split(",") if x.strip().isdigit()]
        return templates.TemplateResponse(
            request,
            "skus/new.html",
            _ctx(
                request,
                "skus",
                items=items_result["items"],
                images=images,
                warehouses=[{"id": w.id, "name": w.name} for w in warehouses],
                preselected_image_ids=preselected_ids,
            ),
        )

    @app.post("/skus/new")
    async def sku_new_submit(request: Request):
        """建档表单提交（页面表单 POST → 路由 → service，与 api_biz 共用）。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        from web.catalog_service import CatalogServiceError, create_items

        biz = _resolve_biz_engine(request)
        form = await request.form()
        product_name = (form.get("product_name") or "").strip() or None

        # 解析多行档（row_0_name, row_0_kind, ...）
        rows: list[dict] = []
        idx = 0
        while True:
            name = form.get(f"row_{idx}_name")
            if name is None:
                break
            kind = form.get(f"row_{idx}_kind", "physical")
            code = form.get(f"row_{idx}_code", "")
            cost_str = form.get(f"row_{idx}_cost", "")
            supplier = form.get(f"row_{idx}_supplier", "")
            row: dict = {
                "name": str(name).strip(),
                "kind": str(kind),
                "code": str(code).strip(),
                "cost": float(cost_str) if cost_str else None,
                "supplier": str(supplier).strip() or None,
            }
            # BOM 行
            bom_rows = []
            bidx = 0
            while True:
                child_id_str = form.get(f"row_{idx}_bom_{bidx}_child")
                if child_id_str is None:
                    break
                qty_str = form.get(f"row_{idx}_bom_{bidx}_qty", "1")
                bom_rows.append({
                    "child_item_id": int(child_id_str),
                    "qty": float(qty_str) if qty_str else 1,
                })
                bidx += 1
            if bom_rows:
                row["bom"] = bom_rows
            rows.append(row)
            idx += 1

        # 图片 ID
        image_ids_raw = form.get("image_file_ids", "")
        image_ids = [int(x) for x in str(image_ids_raw).split(",") if x.strip().isdigit()]

        try:
            async with async_sessionmaker(biz, expire_on_commit=False)() as session, session.begin():
                ids = await create_items(session, product_name=product_name, rows=rows)
                # 挂图
                if image_ids:
                    from web.image_service import attach_images
                    for item_id in ids:
                        try:
                            await attach_images(session, item_id, image_ids)
                        except Exception:
                            pass
        except CatalogServiceError as exc:
            return _redirect("/skus/new", err=str(exc))
        return _redirect("/skus", msg=f"建档成功（{len(ids)} 条）")

    @app.get("/skus/{item_id}")
    async def sku_detail(request: Request, item_id: int, msg: str = "", err: str = ""):
        """档案详情页。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        from web.catalog_store import get_item_detail

        biz = _resolve_biz_engine(request)
        async with async_sessionmaker(biz, expire_on_commit=False)() as session:
            detail = await get_item_detail(session, item_id)
        if detail is None:
            return _redirect("/skus", err="档案不存在")
        # 店铺列表（足迹登记下拉 + 状态操作）
        from web.settings_store import SettingsStore
        store = SettingsStore(biz)
        shops = await store.list_shops()
        return templates.TemplateResponse(
            request,
            "skus/detail.html",
            _ctx(
                request,
                "skus",
                item=detail,
                shops=[{"id": s["id"], "name": s["name"]} for s in shops],
                msg=msg,
                err=err,
            ),
        )

    @app.get("/skus/{item_id}/inventory")
    async def sku_inventory(
        request: Request,
        item_id: int,
        msg: str = "",
        err: str = "",
    ):
        """库存页。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        from web.catalog_store import get_item_detail
        from web.inventory_service import list_stocks, list_ledgers, list_warehouses

        biz = _resolve_biz_engine(request)
        async with async_sessionmaker(biz, expire_on_commit=False)() as session:
            detail = await get_item_detail(session, item_id)
            if detail is None:
                return _redirect("/skus", err="档案不存在")
            stocks = await list_stocks(session, item_id=item_id)
            ledgers = await list_ledgers(session, item_id=item_id)
            warehouses = await list_warehouses(session)
        return templates.TemplateResponse(
            request,
            "skus/inventory.html",
            _ctx(
                request,
                "skus",
                item=detail,
                stocks=stocks,
                ledgers=ledgers,
                warehouses=[{"id": w.id, "name": w.name} for w in warehouses],
                msg=msg,
                err=err,
            ),
        )

    @app.post("/skus/{item_id}/status")
    async def sku_status_submit(request: Request, item_id: int):
        """档案状态转换（页面表单 → service）。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        from web.catalog_service import CatalogServiceError, transition_status

        biz = _resolve_biz_engine(request)
        form = await request.form()
        to = form.get("to", "active")
        try:
            async with async_sessionmaker(biz, expire_on_commit=False)() as session, session.begin():
                await transition_status(session, item_id, to=to)
        except CatalogServiceError as exc:
            return _redirect(f"/skus/{item_id}", err=str(exc))
        return _redirect(f"/skus/{item_id}", msg="状态已更新")

    @app.post("/skus/{item_id}/delete")
    async def sku_delete_submit(request: Request, item_id: int):
        """删除档案（页面表单 → service）。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        from web.catalog_service import CatalogServiceError, delete_item

        biz = _resolve_biz_engine(request)
        try:
            async with async_sessionmaker(biz, expire_on_commit=False)() as session, session.begin():
                await delete_item(session, item_id)
        except CatalogServiceError as exc:
            return _redirect(f"/skus/{item_id}", err=str(exc))
        return _redirect("/skus", msg="档案已删除")

    @app.post("/skus/{item_id}/inventory/ledger")
    async def sku_inventory_ledger_submit(request: Request, item_id: int):
        """库存记账（页面表单 → service）。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        from web.inventory_service import InventoryServiceError, write_ledger

        biz = _resolve_biz_engine(request)
        form = await request.form()
        change_type = form.get("change_type", "purchase")
        warehouse_id = int(form.get("warehouse_id", 0))
        qty_str = form.get("qty", "0")
        note = form.get("note", "")
        try:
            qty = float(qty_str)
        except (ValueError, TypeError):
            return _redirect(f"/skus/{item_id}/inventory", err="数量格式错误")
        try:
            async with async_sessionmaker(biz, expire_on_commit=False)() as session, session.begin():
                await write_ledger(
                    session,
                    item_id=item_id,
                    warehouse_id=warehouse_id,
                    change_type=change_type,
                    qty=qty,
                    note=note,
                )
        except InventoryServiceError as exc:
            return _redirect(f"/skus/{item_id}/inventory", err=str(exc))
        return _redirect(f"/skus/{item_id}/inventory", msg="记账成功")

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
                tasks=[
                    _task_view(
                        t.task,
                        parent_title=t.parent_title,
                        child_count=t.child_count,
                        step_total=t.step_total,  # 复核反馈 #6：步骤 N/M
                        step_done=t.step_done,
                    )
                    for t in tasks
                ],
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
        return_to: str = Form(""),
    ):
        """状态操作：started/completed/voided/blocked/unblocked（§3.5.1）。

        completed/voided 必填 result_note（决策 17，页面弹窗必填 + DB CHECK
        兜底）；blocked 必填 blocked_reason（决策 11：等物料/等回复）。
        详情页操作（详设-v0.6 §15.7 批 9 技术定）：可选 return_to 表单参数 →
        POST 后 303 回详情页（列表页不带该参数，行为不变回 /tasks）。
        """
        store = _store(request)
        path = _safe_return_to(return_to)
        try:
            await store.transition(
                task_id,
                action,
                actor=_role_label(request),
                result_note=result_note.strip() or None,
                blocked_reason=blocked_reason.strip() or None,
            )
        except TMWebError as exc:
            return _redirect(path, err=str(exc))
        return _redirect(path, msg=f"{task_display_id(task_id)} {_ACTION_MSG.get(action, action)}")

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
        link_parent: str = Form(""),
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
                link_parent=str(link_parent) == "1",  # 复核反馈 #6：关联可选
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
        return_to: str = Form(""),
    ):
        """编辑任务（§3.5.5）：改 title/role/due/domain/detail，updated 事件带快照。

        详情页编辑（详设-v0.6 §15.7 批 9 技术定）：可选 return_to → 303 回详情页。
        """
        store = _store(request)
        path = _safe_return_to(return_to)
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
            return _redirect(path, err=str(exc))
        return _redirect(path, msg=f"{task_display_id(task_id)} 已更新")

    @app.post("/tasks/reopen")
    async def task_reopen(
        request: Request, task_id: int = Form(...), return_to: str = Form("")
    ):
        """重开：done/void -> open（改状态 + updated 事件留痕，决策 17 第 4 条）。

        详情页重开（详设-v0.6 §15.7 批 9 技术定）：可选 return_to → 303 回详情页。
        """
        store = _store(request)
        path = _safe_return_to(return_to)
        try:
            await store.reopen_task(task_id, actor=_role_label(request))
        except TMWebError as exc:
            return _redirect(path, err=str(exc))
        return _redirect(path, msg=f"{task_display_id(task_id)} 已重开回待处理")

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

    # ---- 任务详情页（详设-v0.6 §15.7 批 9，用户拍板：独立任务详情页）----
    # 注意：注册须在 GET /tasks/engine/{engine_task_id} 之后（/tasks/engine 是非 int
    # 路径段，先注册会先匹配，路由内 int 校验失败返回 422 遮蔽后续路由）。

    @app.get("/tasks/{task_id}")
    async def task_detail_page(request: Request, task_id: int):
        """独立任务详情页 GET /tasks/{id}：完整 title + detail 全文 + 头部字段 +
        来源与依据（task.source 逐键 + 关联提案 evidence）+ 流转历史（task_event
        时间线）+ 步骤清单 + 完成/作废/编辑/开始等入口（操作 = 简单表单 POST +
        303 回详情，批 9 技术定）。越界/不存在 id → 404。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)  # A16 口径
        store = _store(request)
        detail = await store.get_task_detail(task_id)
        if detail.task is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="任务不存在")
        task = detail.task
        open_steps = sum(1 for s in detail.steps if s["status"] == "open")
        return templates.TemplateResponse(
            request,
            "tasks_detail.html",
            _ctx(
                request,
                "tm",
                task=_task_detail_view(task),
                source_rows=[
                    {"key": k, "value": _source_cell(v)}
                    for k, v in (task.source or {}).items()
                ],
                events=[_event_view(e) for e in detail.events],
                steps=detail.steps,
                open_steps=open_steps,
                proposal=_proposal_view(detail.proposal) if detail.proposal else None,
                status_options=list(STATUS_LABEL.items()),
                role_values=ROLE_VALUES,
                domain_options=[
                    (key, meta["label"])
                    for key, meta in DOMAIN_META.items()
                    if key != "demo"  # 编辑表单的来源域选项（决策 16）
                ],
                msg=request.query_params.get("msg", ""),
                err=request.query_params.get("err", ""),
            ),
        )


    # ---- CRM（v0.3，决策 19/22/23/24；活档案 + 异步链）----

    @app.get("/crm")
    async def crm_index(request: Request, q: str = "", status: str = "", page: str = "1"):
        if request.cookies.get("role") not in ROLE_LABEL:
            return RedirectResponse("/login", status_code=303)
        try:
            page_no = max(1, int(page))
        except ValueError:
            page_no = 1
        rows, total = await request.app.state.crm_store.list_customers(
            q=q, status=status, page=page_no
        )
        # CRM source_shop 下拉候选（详设 §4 / 决策 37-6）
        settings_store = _settings_store(request)
        shops = await settings_store.list_shops() if settings_store else []
        return templates.TemplateResponse(
            request,
            "crm/index.html",
            _ctx(
                request,
                "crm-customers",
                q=q,
                status=status,
                rows=rows,
                total=total,
                page=page_no,
                shops=shops,
                status_filters=[
                    ("", "进行中"), ("waiting_reply", "待回复"), ("replied", "已回复"),
                    ("closed_deal", "已成交"), ("on_hold", "搁置"), ("archived", "归档"),
                    ("all", "全部"),
                ],
            ),
        )

    @app.get("/crm/customers/check-name")
    async def crm_check_name(request: Request, q: str = "", nickname: str = ""):
        keyword = (q or nickname).strip()
        if not keyword:
            return JSONResponse({"matches": []})
        matches = await request.app.state.crm_store.find_same_name(keyword)
        return JSONResponse({"matches": matches})

    @app.post("/crm/customers")
    async def crm_create(request: Request):
        form = await request.form()
        try:
            await request.app.state.crm_store.create_customer(
                nickname=str(form.get("nickname", "")),
                source_shop=str(form.get("source_shop", "")),
                remark=str(form.get("remark", "")),
                force=str(form.get("force", "")) == "1",
            )
        except CrmWebError as exc:
            return RedirectResponse(f"/crm?error={urlencode({'msg': str(exc)})}", status_code=303)
        return RedirectResponse("/crm", status_code=303)

    @app.post("/crm/{customer_id}/delete")
    async def crm_delete(request: Request, customer_id: int):
        try:
            await request.app.state.crm_store.delete_customer(customer_id)
        except CrmWebError:
            pass
        return RedirectResponse("/crm", status_code=303)

    @app.get("/crm/{customer_id}")
    async def crm_detail(request: Request, customer_id: int, engine_task_id: str = ""):
        if request.cookies.get("role") not in ROLE_LABEL:
            return RedirectResponse("/login", status_code=303)
        data = await request.app.state.crm_store.detail(customer_id)
        if data is None:
            return RedirectResponse("/crm", status_code=303)
        return templates.TemplateResponse(
            request, "crm/detail.html", _ctx(request, "crm-customers", **data, engine_task_id=engine_task_id)
        )

    @app.post("/crm/{customer_id}/messages")
    async def crm_paste_messages(request: Request, customer_id: int):
        form = await request.form()
        conversation = str(form.get("conversation_text", ""))
        if not conversation.strip():
            return RedirectResponse(f"/crm/{customer_id}?error={urlencode({'msg': '请先粘贴对话原文'})}", status_code=303)
        try:
            async with _engine_client(request) as client:
                resp = await client.create_task(
                    "crm_chat_chain",
                    {"customer_id": customer_id, "conversation_text": conversation},
                    trigger_ref=request.cookies.get("role", "运营"),
                )
            engine_task_id = resp["task_id"]
        except EngineAPIError as exc:
            return RedirectResponse(
                f"/crm/{customer_id}?error={urlencode({'msg': f'引擎触发失败: {exc}'})}",
                status_code=303,
            )

        # v0.5 批 5：识别 source_text 中的图片链接，落 crm.message_image(pending)
        # 正则提取 http(s) 图片链接（.jpg/.jpeg/.png/.webp/.gif）
        import re
        image_pattern = re.compile(r'https?://[^\s<>\"]+\.(jpg|jpeg|png|webp|gif)', re.IGNORECASE)
        image_urls = image_pattern.findall(conversation)
        if image_urls:
            # 提取完整的 URL（而不是只匹配扩展名）
            full_urls = re.findall(r'https?://[^\s<>\"]+\.(?:jpg|jpeg|png|webp|gif)', conversation, re.IGNORECASE)
            # 落 crm.message_image(pending)
            try:
                async with request.app.state.crm_store._engine.begin() as conn:
                    from sqlalchemy import text as _t
                    # 先获取刚插入的 message_id（最后插入的）
                    result = await conn.execute(
                        _t("SELECT id FROM crm.message WHERE customer_id = :cid ORDER BY id DESC LIMIT 1"),
                        {"cid": customer_id},
                    )
                    message_row = result.fetchone()
                    if message_row:
                        message_id = message_row[0]
                        for url in full_urls:
                            # 幂等检查：同 message_id + url 已存在
                            existing = await conn.execute(
                                _t("SELECT id FROM crm.message_image WHERE message_id = :mid AND url = :url"),
                                {"mid": message_id, "url": url},
                            )
                            if not existing.fetchone():
                                # 插入 pending 状态
                                await conn.execute(
                                    _t("INSERT INTO crm.message_image (message_id, url, status) VALUES (:mid, :url, 'pending')"),
                                    {"mid": message_id, "url": url},
                                )
                        # 自动触发 crm_image_chain
                        if full_urls:
                            try:
                                async with _engine_client(request) as img_client:
                                    # 获取所有 pending 的 message_image_ids
                                    pending_result = await conn.execute(
                                        _t("SELECT id FROM crm.message_image WHERE message_id = :mid AND status = 'pending'"),
                                        {"mid": message_id},
                                    )
                                    pending_ids = [row[0] for row in pending_result.fetchall()]
                                    if pending_ids:
                                        await img_client.create_task(
                                            "crm_image_chain",
                                            {"message_image_ids": pending_ids},
                                            trigger_ref=request.cookies.get("role", "运营"),
                                        )
                            except Exception as e:
                                # 图片处理失败不阻塞主流程
                                import logging
                                logging.getLogger(__name__).warning("触发图片处理链失败: %s", e)
            except Exception as e:
                # 图片处理失败不阻塞主流程
                import logging
                logging.getLogger(__name__).warning("处理图片链接失败: %s", e)

        # 异步：返回任务 id，页面 JS 轮询 -> 终态后调 apply 落库
        return RedirectResponse(f"/crm/{customer_id}?engine_task_id={engine_task_id}", status_code=303)

    @app.post("/crm/{customer_id}/apply")
    async def crm_apply_chain(request: Request, customer_id: int, engine_task_id: str = ""):
        """轮询终态后落库：从引擎任务结果取译文/快照落 crm.message/snapshot。"""
        if not engine_task_id:
            return JSONResponse({"ok": False, "error": "缺 engine_task_id"})
        try:
            async with _engine_client(request) as client:
                data = await client.get_task(engine_task_id)
        except EngineAPIError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        if data.get("status") != "done":
            return JSONResponse({"ok": False, "status": data.get("status")})
        # v0.3：链各步骤输出——translations 在 chat_translate、snapshot 在
        # snapshot_update（中间步骤）；合并传给落库
        merged: dict[str, Any] = {}
        for step in data.get("steps_output") or []:
            step_out = step.get("output") or {}
            if "translations" in step_out:
                merged["translations"] = step_out["translations"]
            if "current_need" in step_out or "summary" in step_out:
                merged["snapshot"] = step_out
        await request.app.state.crm_store.apply_chain_result(customer_id, merged)
        return JSONResponse({"ok": True})

    @app.post("/crm/{customer_id}/reply")
    async def crm_reply(request: Request, customer_id: int):
        form = await request.form()
        mode = str(form.get("mode", "auto")) or "auto"
        points = str(form.get("points", ""))
        full_text = str(form.get("full_text", ""))
        try:
            async with _engine_client(request) as client:
                resp = await client.create_task(
                    "crm_reply_chain",
                    {"customer_id": customer_id, "mode": mode, "points": points, "full_text": full_text},
                    trigger_ref=request.cookies.get("role", "运营"),
                )
        except EngineAPIError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        return JSONResponse({"ok": True, "engine_task_id": resp["task_id"]})

    @app.post("/crm/{customer_id}/reply/send")
    async def crm_reply_send(request: Request, customer_id: int):
        form = await request.form()
        try:
            await request.app.state.crm_store.reply_send(
                customer_id,
                str(form.get("reply_en", "")),
                str(form.get("reply_zh", "")),
            )
        except CrmWebError as exc:
            return RedirectResponse(f"/crm/{customer_id}?error={urlencode({'msg': str(exc)})}", status_code=303)
        return RedirectResponse(f"/crm/{customer_id}", status_code=303)

    @app.post("/crm/{customer_id}/messages/{message_id}/delete")
    async def crm_message_delete(request: Request, customer_id: int, message_id: int):
        async with request.app.state.crm_store._engine.begin() as conn:
            from sqlalchemy import text as _t

            await conn.execute(
                _t("DELETE FROM crm.message WHERE id = :mid AND customer_id = :cid"),
                {"mid": message_id, "cid": customer_id},
            )
        return RedirectResponse(f"/crm/{customer_id}", status_code=303)

    @app.post("/crm/{customer_id}/todos/confirm")
    async def crm_todos_confirm(request: Request, customer_id: int):
        form = await request.form()
        ids = [
            int(v) for v in form.getlist("candidate_ids") if str(v).isdigit()
        ]
        try:
            role_key = request.cookies.get("role", "ops")
            actor = ROLE_LABEL.get(role_key, "运营")  # cookie ASCII 键 -> 中文标签（CHECK 值域）
            created = await request.app.state.crm_store.confirm_candidates(
                customer_id, ids, actor=actor
            )
        except CrmWebError as exc:
            return RedirectResponse(f"/crm/{customer_id}?error={urlencode({'msg': str(exc)})}", status_code=303)
        # 决策 21：确认生成任务 -> 飞书卡片（失败静默不阻塞）
        if created:
            data = await request.app.state.crm_store.detail(customer_id)
            for task_id in created:
                task_view = next((t for t in (data or {}).get("tasks", []) if t["id"] == task_id), None)
                if task_view:
                    await send_task_card({**task_view, "customer": (data or {}).get("customer", {}).get("nickname", "")})
        return RedirectResponse(f"/crm/{customer_id}", status_code=303)

    @app.post("/crm/{customer_id}/todos/dismiss")
    async def crm_todos_dismiss(request: Request, customer_id: int):
        form = await request.form()
        ids = [int(v) for v in form.getlist("candidate_ids") if str(v).isdigit()]
        await request.app.state.crm_store.dismiss_candidates(customer_id, ids)
        return RedirectResponse(f"/crm/{customer_id}", status_code=303)

    @app.get("/crm/api/customers")
    async def crm_api_customers(request: Request, q: str = ""):
        """客户列表 JSON（翻译工具「归入某客户」下拉搜索用；登录保护）。"""
        if request.cookies.get("role") not in ROLE_LABEL:
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        rows, _ = await request.app.state.crm_store.list_customers(q=q, page_size=200)
        return JSONResponse({"ok": True, "customers": [{"id": r["id"], "nickname": r["nickname"]} for r in rows]})

    # ---- 翻译工具（决策 23：对话翻译子页 = 单纯翻译，可选归入客户）----

    @app.get("/translate")
    async def translate_page(request: Request):
        if request.cookies.get("role") not in ROLE_LABEL:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "translate.html", _ctx(request, "crm-translate", result=None))

    @app.post("/translate")
    async def translate_run(request: Request):
        form = await request.form()
        text = str(form.get("text", ""))
        source_lang = str(form.get("source_lang", "en")) or "en"
        target_lang = str(form.get("target_lang", "zh")) or "zh"
        try:
            async with _engine_client(request) as client:
                resp = await client.create_task(
                    "crm_translate_chain",
                    {"text": text, "source_lang": source_lang, "target_lang": target_lang},
                    trigger_ref=request.cookies.get("role", "运营"),
                )
        except EngineAPIError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        # 异步轮询：返回 engine_task_id，JS 查终态展示译文
        return JSONResponse({"ok": True, "engine_task_id": resp["task_id"]})

    @app.post("/translate/archive")
    async def translate_archive(request: Request):
        form = await request.form()
        try:
            await request.app.state.crm_store.archive_translation(
                customer_id=int(str(form.get("customer_id", "0"))),
                source_text=str(form.get("source_text", "")),
                translated_text=str(form.get("translated_text", "")),
                direction=str(form.get("direction", "buyer")),
                language=str(form.get("language", "")),
            )
        except CrmWebError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        return JSONResponse({"ok": True})


    # ---- 任务步骤（复核反馈 #6：分解清单 + 完成依赖）----

    @app.post("/tasks/{task_id}/steps")
    async def task_step_add(request: Request, task_id: int):
        form = await request.form()
        try:
            step = await request.app.state.tm_store.add_step(
                task_id, str(form.get("content", ""))
            )
        except TMWebError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        return JSONResponse({"ok": True, "step": step})

    @app.post("/tasks/{task_id}/steps/{step_id}/toggle")
    async def task_step_toggle(request: Request, task_id: int, step_id: int):
        try:
            step = await request.app.state.tm_store.toggle_step(task_id, step_id)
        except TMWebError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        return JSONResponse({"ok": True, "step": step})

    @app.post("/tasks/{task_id}/steps/{step_id}/delete")
    async def task_step_delete(request: Request, task_id: int, step_id: int):
        try:
            await request.app.state.tm_store.delete_step(task_id, step_id)
        except TMWebError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        return JSONResponse({"ok": True})

    @app.get("/tasks/{task_id}/steps")
    async def task_step_list(request: Request, task_id: int):
        steps = await request.app.state.tm_store.list_steps(task_id)
        return JSONResponse({"ok": True, "steps": steps})

    # ---- 任务「下一步」区（决策 27/28：AI 建议 + 人决定 + 自然语言入口）----

    @app.post("/tasks/{task_id}/next")
    async def task_next(request: Request, task_id: int):
        form = await request.form()
        action = str(form.get("action", ""))
        try:
            await request.app.state.tm_store.next_action(
                task_id,
                action=action,
                target_role=str(form.get("target_role", "")),
                tags=[t.strip() for t in str(form.get("tags", "")).split(",") if t.strip()],
                note=str(form.get("note", "")),
                actor=ROLE_LABEL.get(request.cookies.get("role", "ops"), "运营"),
            )
        except TMWebError as exc:
            return RedirectResponse(f"/tasks?err={urlencode({'msg': str(exc)})}", status_code=303)
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/tasks/{task_id}/next-intent")
    async def task_next_intent(request: Request, task_id: int):
        """自然语言入口（决策 28）：触发 tm_intent_chain -> 返回 engine_task_id。"""
        form = await request.form()
        instruction = str(form.get("instruction", "")).strip()
        if not instruction:
            return JSONResponse({"ok": False, "error": "指令不能为空"})
        try:
            async with _engine_client(request) as client:
                resp = await client.create_task(
                    "tm_intent_chain",
                    {"task_id": task_id, "instruction": instruction},
                    trigger_ref=request.cookies.get("role", "运营"),
                )
        except EngineAPIError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        return JSONResponse({"ok": True, "engine_task_id": resp["task_id"]})

    # ---- 设置页（v0.4 批 2a，详设 §7.1/§7.2）----

    async def _shops_rows_fragment(
        request: Request, *, msg: str = "", err: str = ""
    ) -> TemplateResponse:
        """店铺列表 tbody 片段（HTMX 局部刷新：只刷新列表区，不整页不牵连其他块）。"""
        store = _settings_store(request)
        shops = await store.list_shops(include_disabled=True)
        return templates.TemplateResponse(
            request, "settings/_shops_rows.html",
            _ctx(request, "settings-shops", shops=shops, msg=msg, err=err),
        )

    # v0.5 §3.5：模型选择页（真页，非 placeholder）
    @app.get("/settings/ai/model")
    async def settings_ai_model(request: Request):
        """模型选择页（语言模型 + 识图模型，独立提交 + HTMX 局部刷新）。"""
        store = _settings_store(request)
        llm_model = await store.get("ai.llm_model", "deepseek-chat")
        vision_model = await store.get("ai.vision_model", "qwen-vl-max")
        ctx = _ctx(
            request,
            "settings-ai-model",
            llm_model=llm_model,
            vision_model=vision_model,
            llm_key_configured=_check_env_key("DEEPSEEK_API_KEY"),
            vision_key_configured=_check_env_key("VISION_API_KEY"),
            msg=request.query_params.get("msg", ""),
            err=request.query_params.get("err", ""),
        )
        return templates.TemplateResponse(request, "settings/model.html", ctx)

    @app.post("/settings/ai/model")
    async def settings_ai_model_save(request: Request):
        """模型选择保存（语言模型块 + 识图模型块独立提交）。"""
        from web.env_writer import write_env_var
        form = await request.form()
        store = _settings_store(request)
        is_hx = bool(request.headers.get("hx-request"))

        # 语言模型块
        llm_api_key = str(form.get("llm_api_key", "")).strip()
        llm_model = str(form.get("llm_model", "deepseek-chat")).strip()

        # 识图模型块
        vision_api_key = str(form.get("vision_api_key", "")).strip()
        vision_base_url = str(form.get("vision_base_url", "")).strip()
        vision_model = str(form.get("vision_model", "qwen-vl-max")).strip()

        # 写 .env（密钥只进 .env，R20）
        if llm_api_key:
            write_env_var("DEEPSEEK_API_KEY", llm_api_key)
        if vision_api_key:
            write_env_var("VISION_API_KEY", vision_api_key)
        if vision_base_url:
            write_env_var("VISION_BASE_URL", vision_base_url)

        # 写 settings 表（模型名）
        await store.set("ai.llm_model", llm_model, "语言模型名")
        await store.set("ai.vision_model", vision_model, "识图模型名")

        msg = "模型设置已保存（密钥存 .env，模型名存设置表，重启引擎后生效）"
        if is_hx:
            return _hx_redirect("/settings/ai/model", msg=msg)
        return _redirect("/settings/ai/model", msg=msg)

    @app.get("/settings/ai/style")
    def settings_ai_style_placeholder(request: Request):
        return templates.TemplateResponse(
            request, "settings/placeholder.html",
            _ctx(request, "settings-ai-style", page_name="风格指南术语表"),
        )

    @app.get("/settings/ai/api-key")
    def settings_ai_api_key(request: Request):
        """API 密钥页（R20：值只存 .env，友好壳）。"""
        configured = _check_env_key("DEEPSEEK_API_KEY")
        ctx = _ctx(request, "settings-ai-key",
                   configured=configured,
                   msg=request.query_params.get("msg", ""),
                   err=request.query_params.get("err", ""))
        if request.headers.get("hx-request"):
            return templates.TemplateResponse(request, "settings/ai_key.html", ctx)
        return templates.TemplateResponse(request, "settings/ai_key.html", ctx)

    @app.post("/settings/ai/api-key")
    async def settings_ai_api_key_save(request: Request):
        from web.env_writer import write_env_var
        form = await request.form()
        api_key = str(form.get("api_key", "")).strip()
        is_hx = bool(request.headers.get("hx-request"))
        if not api_key:
            if is_hx:
                return _hx_redirect("/settings/ai/api-key", err="API 密钥不能为空")
            return _redirect("/settings/ai/api-key", err="API 密钥不能为空")
        ok = write_env_var("DEEPSEEK_API_KEY", api_key)
        if not ok:
            if is_hx:
                return _hx_redirect("/settings/ai/api-key", err="写入 .env 失败，请检查文件权限")
            return _redirect("/settings/ai/api-key", err="写入 .env 失败，请检查文件权限")
        msg = "API 密钥已保存（重启引擎后生效）"
        if is_hx:
            # HTMX 局部刷新：返回卡片片段（独立提交，只刷新本块，不整页）
            ctx = _ctx(request, "settings-ai-key",
                       configured=_check_env_key("DEEPSEEK_API_KEY"),
                       msg=msg, err="")
            return templates.TemplateResponse(request, "settings/_ai_key_card.html", ctx)
        return _redirect("/settings/ai/api-key", msg=msg)

    @app.get("/settings/shops")
    async def settings_shops(request: Request):
        """店铺管理页（列表 + 新建表单）。"""
        store = _settings_store(request)
        shops = await store.list_shops(include_disabled=True)
        return templates.TemplateResponse(
            request, "settings/shops.html",
            _ctx(request, "settings-shops",
                 shops=shops,
                 msg=request.query_params.get("msg", ""),
                 err=request.query_params.get("err", "")),
        )

    @app.post("/settings/shops")
    async def settings_shops_create(request: Request):
        """新建店铺（独立提交，重名 SettingsError -> err 提示）。"""
        form = await request.form()
        name = str(form.get("name", "")).strip()
        remark = str(form.get("remark", "")).strip()
        store = _settings_store(request)
        is_hx = bool(request.headers.get("hx-request"))
        try:
            await store.create_shop(name, remark)
        except SettingsError as exc:
            if is_hx:
                return await _shops_rows_fragment(request, err=str(exc))
            return _redirect("/settings/shops", err=str(exc))
        if is_hx:
            return await _shops_rows_fragment(request, msg=f"店铺「{name}」已创建")
        return _redirect("/settings/shops", msg=f"店铺「{name}」已创建")

    @app.post("/settings/shops/{shop_id}")
    async def settings_shops_update(request: Request, shop_id: int):
        """改店铺（name/remark/platform）。"""
        form = await request.form()
        name = str(form.get("name", "")).strip()
        remark = str(form.get("remark", "")).strip()
        platform = str(form.get("platform", "other")).strip()
        store = _settings_store(request)
        try:
            await store.update_shop(shop_id, name, remark, platform=platform)
        except SettingsError as exc:
            return _redirect("/settings/shops", err=str(exc))
        return _redirect("/settings/shops", msg="店铺已更新")

    @app.post("/settings/shops/{shop_id}/toggle")
    async def settings_shops_toggle(request: Request, shop_id: int):
        """启停店铺（独立提交，HTMX 局部刷新店铺列表）。"""
        store = _settings_store(request)
        is_hx = bool(request.headers.get("hx-request"))
        try:
            new_enabled = await store.toggle_shop(shop_id)
        except SettingsError as exc:
            if is_hx:
                return await _shops_rows_fragment(request, err=str(exc))
            return _redirect("/settings/shops", err=str(exc))
        label = "已启用" if new_enabled else "已停用"
        if is_hx:
            return await _shops_rows_fragment(request, msg=f"店铺{label}")
        return _redirect("/settings/shops", msg=f"店铺{label}")

    @app.post("/settings/shops/{shop_id}/delete")
    async def settings_shops_delete(request: Request, shop_id: int):
        """删除店铺（hx-confirm 二次确认，物理删；HTMX 局部刷新店铺列表）。"""
        store = _settings_store(request)
        is_hx = bool(request.headers.get("hx-request"))
        try:
            await store.delete_shop(shop_id)
        except SettingsError as exc:
            if is_hx:
                return await _shops_rows_fragment(request, err=str(exc))
            return _redirect("/settings/shops", err=str(exc))
        if is_hx:
            return await _shops_rows_fragment(request, msg="店铺已删除")
        return _redirect("/settings/shops", msg="店铺已删除")

    @app.get("/settings/schedule")
    async def settings_schedule(request: Request):
        """定时任务页（详设 §7.2）。"""
        store = _settings_store(request)
        default_time = await store.get("schedule.default_time", "07:00")
        # 引擎链清单（触发面板）；引擎未连接/未启动时降级展示
        schedules: list[dict] = []
        available_chains: list[dict] = []
        engine_error: str | None = None
        try:
            async with _engine_client(request) as client:
                reg = await client.list_registry()
                chains = reg.get("chains", [])
                available_chains = [{"id": c["id"], "name": c.get("name", c["id"])} for c in chains]
                schedules = await client.list_schedules()
        except EngineAPIError as exc:
            engine_error = f"引擎未连接：{exc}"
        return templates.TemplateResponse(
            request, "settings/schedule.html",
            _ctx(request, "settings-schedule",
                 schedules=schedules,
                 available_chains=available_chains,
                 default_time=default_time,
                 engine_error=engine_error,
                 msg=request.query_params.get("msg", ""),
                 err=request.query_params.get("err", "")),
        )

    @app.post("/settings/schedule")
    async def settings_schedule_create(request: Request):
        """新增定时链（独立提交）。"""
        form = await request.form()
        chain_id = str(form.get("chain_id", "")).strip()
        trigger_time = str(form.get("trigger_time", "07:00")).strip()
        if not chain_id:
            return _redirect("/settings/schedule", err="请选择工序链")
        trigger_expr = _time_to_cron(trigger_time)
        try:
            async with _engine_client(request) as client:
                await client.create_schedule(chain_id, trigger_expr)
        except EngineAPIError as exc:
            return _redirect("/settings/schedule", err=f"新增失败：{exc}")
        return _redirect("/settings/schedule", msg=f"定时链「{chain_id}」已新增")

    @app.post("/settings/schedule/{schedule_id}/toggle")
    async def settings_schedule_toggle(request: Request, schedule_id: int):
        """启停定时链（独立提交）。"""
        try:
            async with _engine_client(request) as client:
                await client.toggle_schedule(schedule_id)
        except EngineAPIError as exc:
            return _redirect("/settings/schedule", err=f"启停失败：{exc}")
        return _redirect("/settings/schedule", msg="定时链状态已更新")

    @app.post("/settings/schedule/{schedule_id}/time")
    async def settings_schedule_time(request: Request, schedule_id: int):
        """改定时链触发时间（独立提交）。"""
        form = await request.form()
        trigger_time = str(form.get("trigger_time", "07:00")).strip()
        trigger_expr = _time_to_cron(trigger_time)
        try:
            async with _engine_client(request) as client:
                await client.update_schedule_time(schedule_id, trigger_expr)
        except EngineAPIError as exc:
            return _redirect("/settings/schedule", err=f"改时间失败：{exc}")
        return _redirect("/settings/schedule", msg="触发时间已更新")

    @app.post("/settings/schedule/{schedule_id}/run")
    async def settings_schedule_run(request: Request, schedule_id: int):
        """立即运行一次定时链（独立提交）。"""
        try:
            async with _engine_client(request) as client:
                resp = await client.run_schedule(schedule_id)
        except EngineAPIError as exc:
            return _redirect("/settings/schedule", err=f"运行失败：{exc}")
        eid = resp.get("engine_task_id", "")
        return _redirect("/settings/schedule", msg=f"已触发运行，引擎任务 {eid}")

    @app.get("/settings/notify")
    async def settings_notify(request: Request):
        """通知配置页（详设 §7.2/§7.3）。"""
        store = _settings_store(request)
        feishu_enabled = await store.get("notify.feishu_enabled", True)
        webhook_configured = _check_env_key("LIUQUAN_FEISHU_WEBHOOK_URL")
        ctx = _ctx(request, "settings-notify",
                   feishu_enabled=feishu_enabled,
                   webhook_configured=webhook_configured,
                   msg=request.query_params.get("msg", ""),
                   err=request.query_params.get("err", ""))
        if request.headers.get("hx-request"):
            return templates.TemplateResponse(request, "settings/notify.html", ctx)
        return templates.TemplateResponse(request, "settings/notify.html", ctx)

    @app.post("/settings/notify")
    async def settings_notify_save(request: Request):
        """保存通知配置（开关写 settings + webhook 写 .env）。"""
        from web.env_writer import write_env_var
        form = await request.form()
        store = _settings_store(request)
        is_hx = bool(request.headers.get("hx-request"))
        # 总开关
        feishu_enabled = str(form.get("feishu_enabled", "")).lower() == "on"
        await store.set("notify.feishu_enabled", feishu_enabled, "飞书通知总开关")
        # webhook URL（仅在有值时写入）
        webhook_url = str(form.get("webhook_url", "")).strip()
        msg = "通知配置已保存"
        if webhook_url:
            ok = write_env_var("LIUQUAN_FEISHU_WEBHOOK_URL", webhook_url)
            if not ok:
                msg = "开关已保存，但 Webhook URL 写入失败"
                if is_hx:
                    return _hx_redirect("/settings/notify", msg=msg)
                return _redirect("/settings/notify", msg=msg)
        if is_hx:
            # HTMX 局部刷新：返回卡片片段（独立提交，只刷新本块）
            ctx = _ctx(request, "settings-notify",
                       feishu_enabled=feishu_enabled,
                       webhook_configured=_check_env_key("LIUQUAN_FEISHU_WEBHOOK_URL"),
                       msg=msg, err="")
            return templates.TemplateResponse(request, "settings/_notify_card.html", ctx)
        return _redirect("/settings/notify", msg=msg)

    @app.get("/settings/params")
    async def settings_params(request: Request):
        """系统参数页（详设 §7.2/§7.3）。"""
        store = _settings_store(request)
        params = []
        for kd in _PARAM_DEFS:
            val = await store.get(kd["key"], kd["default"])
            params.append({
                "key": kd["key"],
                "description": kd["description"],
                "type": kd["type"],
                "value": val,
            })
        ctx = _ctx(request, "settings-params",
                   params=params,
                   msg=request.query_params.get("msg", ""),
                   err=request.query_params.get("err", ""))
        if request.headers.get("hx-request"):
            return templates.TemplateResponse(request, "settings/params.html", ctx)
        return templates.TemplateResponse(request, "settings/params.html", ctx)

    @app.post("/settings/params/{key}")
    async def settings_params_save(request: Request, key: str):
        """单键保存（独立提交，只原子更新该 key）。"""
        from web.settings_store import _TYPE_REGISTRY
        store = _settings_store(request)
        is_hx = bool(request.headers.get("hx-request"))
        form = await request.form()
        value = str(form.get("value", "")).strip()
        # 类型校验
        type_kind = _TYPE_REGISTRY.get(key, "str")
        if type_kind == "int":
            try:
                value = int(value)
            except ValueError:
                return _hx_redirect("/settings/params", err=f"参数 {key} 需为整数")
        elif type_kind == "float":
            try:
                value = float(value)
            except ValueError:
                return _hx_redirect("/settings/params", err=f"参数 {key} 需为数字")
        elif type_kind == "bool":
            value = value.lower() in ("true", "1", "yes", "on")
        await store.set(key, value)
        if is_hx:
            # HTMX 局部刷新：只返回该参数行片段（其他 key 不动，A41 独立提交）
            desc = next((d["description"] for d in _PARAM_DEFS if d["key"] == key), "")
            param = {
                "key": key,
                "description": desc,
                "type": type_kind,
                "value": await store.get(key, None),
            }
            ctx = _ctx(request, "settings-params",
                       param=param,
                       msg=f"参数 {key} 已保存", err="")
            return templates.TemplateResponse(request, "settings/_params_row.html", ctx)
        return _hx_redirect("/settings/params", msg=f"参数 {key} 已保存")

    # v0.6 批 2：扒图设置页三块（存储目录 / 定时默认时间 / 定时队列，独立提交 + HTMX
    # 局部刷新，详设-v0.6 §3.2——定时相关设置统一进设置菜单，扒图页不做队列入口）
    @app.get("/settings/scrape")
    async def settings_scrape(request: Request):
        """扒图设置页：存储目录 / 定时默认时间 / 上传默认开启 / 夸克登录四块独立提交。

        批 8（详设 §15.3）：定时队列管理块已移回扒图页（/scrape）——本页不再含
        队列块与队列路由；新增「上传默认开启」开关块（设置键 netdisk.upload_default）。

        夸克块页面 GET 不触子进程（批 7 技术定：状态探测只在显式动作执行）——
        只做工具路径存在性判断（quark_available），真实登录状态经「检测登录状态」
        按钮/登录/退出动作返回片段刷新。
        """
        from datetime import time as _dtime

        from web import quark_tool

        store = _settings_store(request)
        storage_dir = str(await store.get("scrape.storage_dir", "/opt/liuquan/scrape/"))
        raw_time = await store.get("scrape.schedule_time", "07:00")
        schedule_time = (
            raw_time.strftime("%H:%M") if isinstance(raw_time, _dtime) else str(raw_time)
        )
        upload_default = bool(await store.get("netdisk.upload_default", True))
        return templates.TemplateResponse(
            request,
            "settings/scrape.html",
            _ctx(
                request,
                "settings-scrape",
                storage_dir=storage_dir,
                schedule_time=schedule_time,
                upload_default=upload_default,
                quark_available=quark_tool.tool_available(),
                quark_status=None,
                msg=request.query_params.get("msg", ""),
                err=request.query_params.get("err", ""),
            ),
        )

    async def _scrape_time_fragment(
        request: Request,
        *,
        schedule_time: str,
        msg: str = "",
        err: str = "",
    ):
        """定时默认时间卡片片段（HTMX 局部刷新，独立提交只刷本块）。"""
        return templates.TemplateResponse(
            request,
            "settings/_scrape_time_card.html",
            _ctx(
                request,
                "settings-scrape",
                schedule_time=schedule_time,
                msg=msg,
                err=err,
            ),
        )

    async def _scrape_upload_default_fragment(
        request: Request,
        *,
        upload_default: bool,
        msg: str = "",
        err: str = "",
    ):
        """「上传默认开启」开关卡片片段（批 8：独立提交 + HTMX 局部刷新只刷本块）。"""
        return templates.TemplateResponse(
            request,
            "settings/_scrape_upload_default_card.html",
            _ctx(
                request,
                "settings-scrape",
                upload_default=upload_default,
                msg=msg,
                err=err,
            ),
        )

    @app.post("/settings/scrape")
    async def settings_scrape_save(request: Request):
        """扒图设置保存：存储目录 / 定时默认时间 / 上传默认开启 各块独立提交（按表单字段区分）。

        定时默认时间保存时同步引擎定时链 cron：list_schedules() 找到
        chain_id=scrape_download_chain 的行则 update_schedule_time（详设 §8，A63 实锤）；
        找不到行跳过不报错（种子批 4 才加），引擎未连接提示不阻塞设置保存。
        批 8：+「上传默认开启」块（upload_default 字段 on/off → 设置键
        netdisk.upload_default；独立提交不牵连其他块）。
        """
        from web import scrape_store

        store = _settings_store(request)
        is_hx = bool(request.headers.get("hx-request"))
        form = await request.form()

        if str(form.get("block", "")) == "upload_default":
            # ---- 「上传默认开启」块（批 8，独立提交）----
            # 隐藏 off + 复选框 on 同名 → getlist 取末值（未勾选=off，勾选=on）
            upload_default = [str(v) for v in form.getlist("upload_default")][-1] == "on"
            await store.set(
                "netdisk.upload_default",
                upload_default,
                "同步上传网盘默认开关（页面勾选初始值 + 定时 input）",
            )
            msg = f"上传默认开关已保存（{'开启' if upload_default else '关闭'}）"
            if is_hx:
                return await _scrape_upload_default_fragment(
                    request, upload_default=upload_default, msg=msg, err=""
                )
            return _redirect("/settings/scrape", msg=msg)

        if "schedule_time" in form:
            # ---- 定时默认时间块（独立提交）----
            raw = str(form.get("schedule_time", "")).strip()
            try:
                hour_s, minute_s = raw.split(":")
                h, m = int(hour_s), int(minute_s)
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
                norm = f"{h:02d}:{m:02d}"
            except ValueError:
                if is_hx:
                    return await _scrape_time_fragment(
                        request, schedule_time=raw, err="时间格式应为 HH:MM"
                    )
                return _redirect("/settings/scrape", err="时间格式应为 HH:MM")
            await store.set("scrape.schedule_time", norm, "扒图定时默认时间")
            msg = f"定时默认时间已保存（{norm}）"
            # 同步引擎定时链 cron（改设置生效实锤；找不到行跳过不报错）
            try:
                async with _engine_client(request) as client:
                    schedules = await client.list_schedules()
                    row = next(
                        (
                            s
                            for s in schedules
                            if s.get("chain_id") == "scrape_download_chain"
                        ),
                        None,
                    )
                    if row is not None:
                        await client.update_schedule_time(
                            int(row["id"]), _scrape_time_cron(norm)
                        )
                        msg += "，定时链已同步"
            except EngineAPIError:
                msg += "（引擎未连接，定时链 cron 未同步）"
            if is_hx:
                return await _scrape_time_fragment(
                    request, schedule_time=norm, msg=msg, err=""
                )
            return _redirect("/settings/scrape", msg=msg)

        # ---- 存储目录块（v0.5 原有，独立提交）----
        storage_dir = str(form.get("storage_dir", "/opt/liuquan/scrape/")).strip()
        if not storage_dir:
            storage_dir = "/opt/liuquan/scrape/"
        await store.set("scrape.storage_dir", storage_dir, "扒图存储目录")
        msg = "扒图设置已保存"
        if is_hx:
            return _hx_redirect("/settings/scrape", msg=msg)
        return _redirect("/settings/scrape", msg=msg)

    # ---- 夸克网盘登录块（批 7，详设 §15.2：独立提交；页面 GET 不触子进程）----

    async def _scrape_quark_fragment(
        request: Request,
        *,
        quark_available: bool,
        status: dict | None = None,
        msg: str = "",
        err: str = "",
    ):
        """夸克登录卡片片段（HTMX 局部刷新：登录/检测/退出后只刷本块）。"""
        return templates.TemplateResponse(
            request,
            "settings/_scrape_quark_card.html",
            _ctx(
                request,
                "settings-scrape",
                quark_available=quark_available,
                quark_status=status,
                msg=msg,
                err=err,
            ),
        )

    @app.post("/settings/scrape/quark-login")
    async def settings_scrape_quark_login(request: Request):
        """夸克授权码登录（独立提交块）：执行 quark.sh login --token <code>。

        授权码一次性输入（R20 精神）：不落库不落盘、页面不回显；登录后探测
        get-user-info 状态随片段刷新。工具路径缺失（quark_available=False）直接
        提示，不触子进程。
        """
        from web import quark_tool

        available = quark_tool.tool_available()
        if not available:
            return await _scrape_quark_fragment(
                request,
                quark_available=False,
                status=quark_tool.quark_status(),
                err="夸克工具路径缺失（quark.sh 未安装），无法登录",
            )
        form = await request.form()
        code = str(form.get("code", "")).strip()
        ok, message = quark_tool.quark_login(code)
        status = quark_tool.quark_status() if ok else None
        if ok:
            return await _scrape_quark_fragment(
                request, quark_available=True, status=status, msg=message
            )
        return await _scrape_quark_fragment(
            request, quark_available=True, status=status, err=message
        )

    @app.post("/settings/scrape/quark-status")
    async def settings_scrape_quark_status(request: Request):
        """检测夸克登录状态（显式动作，独立提交块）。"""
        from web import quark_tool

        status = quark_tool.quark_status()
        err = "" if status.get("state") == "authorized" else str(status.get("note", ""))
        return await _scrape_quark_fragment(
            request,
            quark_available=quark_tool.tool_available(),
            status=status,
            err=err,
        )

    @app.post("/settings/scrape/quark-logout")
    async def settings_scrape_quark_logout(request: Request):
        """退出夸克登录（独立提交块）。"""
        from web import quark_tool

        ok, message = quark_tool.quark_logout()
        status = quark_tool.quark_status() if ok else None
        if ok:
            return await _scrape_quark_fragment(
                request, quark_available=True, status=status, msg=message
            )
        return await _scrape_quark_fragment(
            request, quark_available=True, status=status, err=message
        )

    @app.post("/tasks/{task_id}/next-confirm")
    async def task_next_confirm(request: Request, task_id: int):
        """确认解析后的流转指令并执行（决策 28：人确认后才执行；模糊不猜测）。"""
        form = await request.form()
        action = str(form.get("action", ""))
        clarity = str(form.get("clarity", "clear"))
        if clarity != "clear":
            return RedirectResponse("/tasks?err=" + urlencode({"msg": "指令不明确，请重新描述"}), status_code=303)
        target_domain = str(form.get("target_domain", ""))
        if action == "transfer":
            # 决策 28：目标域未接入（erp/seo）明确提示不可执行并记录意图
            if target_domain in ("erp", "seo"):
                await request.app.state.tm_store.record_disagreement(
                    task_id,
                    ai_suggestion={},
                    human_chose=f"transfer -> {target_domain}（未接入域）",
                    actor=ROLE_LABEL.get(request.cookies.get("role", "ops"), "运营"),
                )
                return RedirectResponse(
                    "/tasks?err=" + urlencode({"msg": f"流转到 {target_domain} 域 v0.6/v0.5 才接入，暂不可执行（意图已记录）"}),
                    status_code=303,
                )
        try:
            await request.app.state.tm_store.next_action(
                task_id,
                action=action,
                target_role=str(form.get("target_role", "")),
                tags=[t.strip() for t in str(form.get("tags", "")).split(",") if t.strip()],
                note=str(form.get("note", "")),
                actor=ROLE_LABEL.get(request.cookies.get("role", "ops"), "运营"),
            )
        except TMWebError as exc:
            return RedirectResponse(f"/tasks?err={urlencode({'msg': str(exc)})}", status_code=303)
        return RedirectResponse("/tasks", status_code=303)

    # ---- SEO 路由（v0.5 批 2，详设-v0.5 §3.3）----

    @app.get("/seo/keywords")
    async def seo_keywords_page(request: Request):
        """关键词研究页（照 crm 页模式 + 登录保护）。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        return request.app.state.templates.TemplateResponse(
            request, "seo/keywords.html", _ctx(request, "seo-keywords")
        )

    @app.post("/seo/keywords")
    async def seo_keywords_research(request: Request):
        """关键词研究接口（HTMX 局部刷新，照 crm 粘贴链模式）。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        keywords_str = str(form.get("keywords", "")).strip()
        page_size = int(form.get("page_size", 10))

        if not keywords_str:
            return request.app.state.templates.TemplateResponse(
                request, "seo/keywords.html",
                {**_ctx(request, "seo-keywords"), "error": "请输入关键词"}
            )

        keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]
        if len(keywords) > 8:
            keywords = keywords[:8]

        # 调用引擎 create_task 触发 seo_keyword_chain → 轮询 → 展示结果
        try:
            async with EngineAPIClient() as engine_client:
                created = await engine_client.create_task(
                    "seo_keyword_chain",
                    {"keywords": keywords, "page_size": page_size},
                    "运营",
                )
                task_id = created["task_id"]

                # 轮询引擎任务状态（最多 60s）
                import asyncio
                keyword_data = None
                for _ in range(60):
                    await asyncio.sleep(1)
                    task = await engine_client.get_task(str(task_id))
                    status = task.get("status", "")
                    if status == "done":
                        # 从 steps_output 取 keyword_research 产出
                        steps_output = task.get("steps_output", {})
                        if steps_output:
                            last_step = steps_output.get("keyword_research", {})
                            if last_step:
                                keyword_data = last_step
                        break
                    elif status in ("failed", "paused"):
                        return request.app.state.templates.TemplateResponse(
                            request, "seo/keywords.html",
                            {**_ctx(request, "seo-keywords"), "error": f"调研失败：{task.get('error', '任务失败')}"}
                        )

                if keyword_data:
                    return request.app.state.templates.TemplateResponse(
                        request, "seo/keywords.html",
                        {**_ctx(request, "seo-keywords"), "keyword_data": keyword_data}
                    )
                else:
                    return request.app.state.templates.TemplateResponse(
                        request, "seo/keywords.html",
                        {**_ctx(request, "seo-keywords"), "error": "调研超时或无结果"}
                    )
        except Exception as exc:
            return request.app.state.templates.TemplateResponse(
                request, "seo/keywords.html",
                {**_ctx(request, "seo-keywords"), "error": f"调研失败：{exc}"}
            )

    @app.get("/seo/optimize")
    async def seo_optimize_page(request: Request):
        """SEO 优化页（照 crm 页模式 + 登录保护）。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        return request.app.state.templates.TemplateResponse(
            request, "seo/optimize.html", _ctx(request, "seo-optimize")
        )

    @app.post("/seo/optimize")
    async def seo_optimize_action(request: Request):
        """SEO 优化接口（HTMX 局部刷新，照 crm 粘贴链模式）。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        title = str(form.get("title", "")).strip()
        tags_str = str(form.get("tags", "")).strip()
        description = str(form.get("description", "")).strip()
        target_keywords_str = str(form.get("target_keywords", "")).strip()
        playbook_key = str(form.get("playbook_key", "")).strip()

        if not title and not tags_str and not description:
            return request.app.state.templates.TemplateResponse(
                request, "seo/optimize.html",
                {**_ctx(request, "seo-optimize"), "error": "请至少输入标题、标签或描述"}
            )

        tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []
        target_keywords = [k.strip() for k in target_keywords_str.split(",") if k.strip()] if target_keywords_str else None

        # 调用引擎 create_task 触发 seo_optimize_chain → 轮询 → 展示报告
        try:
            product_text = {"title": title, "tags": tags, "description": description}
            chain_input = {"product_text": product_text}
            if target_keywords:
                chain_input["target_keywords"] = target_keywords
            if playbook_key:
                chain_input["playbook_key"] = playbook_key

            async with EngineAPIClient() as engine_client:
                created = await engine_client.create_task(
                    "seo_optimize_chain", chain_input, "运营",
                )
                task_id = created["task_id"]

                import asyncio
                report = None
                for _ in range(120):  # SEO 优化 LLM 工序可能较慢，等 120s
                    await asyncio.sleep(1)
                    task = await engine_client.get_task(str(task_id))
                    status = task.get("status", "")
                    if status == "done":
                        steps_output = task.get("steps_output", {})
                        if steps_output:
                            report = steps_output.get("seo_optimize", {})
                        break
                    elif status in ("failed", "paused"):
                        return request.app.state.templates.TemplateResponse(
                            request, "seo/optimize.html",
                            {**_ctx(request, "seo-optimize"), "error": f"优化失败：{task.get('error', '任务失败')}"}
                        )

                if report:
                    return request.app.state.templates.TemplateResponse(
                        request, "seo/optimize.html",
                        {**_ctx(request, "seo-optimize"), "report": report}
                    )
                else:
                    return request.app.state.templates.TemplateResponse(
                        request, "seo/optimize.html",
                        {**_ctx(request, "seo-optimize"), "error": "优化超时或无结果"}
                    )
        except Exception as exc:
            return request.app.state.templates.TemplateResponse(
                request, "seo/optimize.html",
                {**_ctx(request, "seo-optimize"), "error": f"优化失败：{exc}"}
            )

    @app.get("/seo/healthcheck")
    async def seo_healthcheck_page(request: Request):
        """listing 体检页（照 crm 页模式 + 登录保护）。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)

        # 获取有指标数据的关键词列表
        try:
            from web.seo_store import SEOStore
            seo_store = SEOStore(request.app.state.engine)
            keywords = await seo_store.list_keywords_with_metrics()
        except Exception:
            keywords = []

        return request.app.state.templates.TemplateResponse(
            request, "seo/healthcheck.html",
            {**_ctx(request, "seo-healthcheck"), "keywords": keywords}
        )

    @app.get("/seo/healthcheck/{keyword}")
    async def seo_healthcheck_detail(request: Request, keyword: str):
        """listing 体检详情页（单个关键词历史指标）。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)

        try:
            from web.seo_store import SEOStore
            seo_store = SEOStore(request.app.state.engine)
            metrics = await seo_store.get_keyword_metrics(keyword)
        except Exception:
            metrics = []

        return request.app.state.templates.TemplateResponse(
            request, "seo/healthcheck.html",
            {**_ctx(request, "seo-healthcheck"), "keyword": keyword, "metrics": metrics}
        )

    @app.post("/seo/healthcheck/run")
    async def seo_healthcheck_run(request: Request):
        """listing 体检立即运行（HTMX 局部刷新，照定时页立即运行模式）。"""
        if not request.cookies.get("role"):
            return RedirectResponse("/login", status_code=303)

        try:
            async with EngineAPIClient() as engine_client:
                # 先查 seo_healthcheck_chain 种子 schedule id
                schedules = await engine_client.list_schedules()
                hc_schedule = None
                for s in schedules:
                    if s.get("chain_id") == "seo_healthcheck_chain":
                        hc_schedule = s
                        break

                if hc_schedule:
                    # 立即运行种子 schedule
                    await engine_client.run_schedule(hc_schedule["id"])
                    return "<div class='alert alert-success'>体检已触发（定时链立即运行）</div>"
                else:
                    # 种子不存在，直接 create_task
                    created = await engine_client.create_task(
                        "seo_healthcheck_chain", {"keywords": []}, "运营",
                    )
                    return "<div class='alert alert-success'>体检已触发（直接创建任务）</div>"
        except Exception as exc:
            return f"<div class='alert alert-danger'>体检失败：{exc}</div>"

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
