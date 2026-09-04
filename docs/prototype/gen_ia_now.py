# -*- coding: utf-8 -*-
"""生成《刘全信息架构现状图》 Excalidraw 文件（docs/prototype/ia-now-20260904.excalidraw）。

数据源：docs/原型盘点-全系统页面与交互问题.md（来自 web/app.py MODULES + 路由真值）。
Excalidraw v2 JSON：盒子=rectangle + 绑定文本（自动居中）；占位页=红色虚线框。
"""
import json
import os

OUT = os.path.join(os.path.dirname(__file__), "ia-now-20260904.excalidraw")

INK = "#1e1e1e"
DIM = "#868e96"
RED = "#fa5252"

# 模块列：(id, 名称, 底色, 叶子列表 [(名称, 是否占位), ...])
MODULES = [
    ("workspace", "工作台", "#e9ecef", [("工作台（统计/总览）", True)]),
    ("tm", "任务中心", "#a5d8ff", [
        ("任务列表", False),
        ("任务详情 + 步骤面板", False),
        ("引擎任务页", False),
    ]),
    ("crm", "CRM", "#b2f2bb", [
        ("客户列表", False),
        ("客户详情（消息/快照/待办）", False),
        ("对话翻译", False),
        ("跟进待办 → 复用任务列表", False),
    ]),
    ("erp", "ERP 建档/库存", "#ffd8a8", [
        ("货品建档（列表）", False),
        ("新建档案（多行+BOM+选图）", False),
        ("档案详情 / 编辑", False),
        ("库存管理（结存）", False),
        ("图库浏览器（文件夹/搜索/上传）", False),
        ("补货建议", True),
        ("异常预警", True),
        ("生产库存（只读）", True),
    ]),
    ("seo", "SEO", "#eebefa", [
        ("扒图（贴链接+素材库）", False),
        ("关键词研究", False),
        ("SEO 优化", False),
        ("listing 体检", False),
    ]),
    ("settings", "设置", "#ffc9c9", [
        ("店铺管理", False),
        ("API 密钥 / 模型选择", False),
        ("风格指南术语表", True),
        ("定时任务", False),
        ("通知配置", False),
        ("扒图设置（目录/夸克）", False),
        ("系统参数", False),
    ]),
]

# 画布
MARGIN_X = 60
COL_W = 200          # 每列宽度（含间隙）
ROOT_Y = 60
MOD_Y = 200
LEAF_Y0 = 300
LEAF_H = 42
LEAF_GAP = 12
CANVAS_W = MARGIN_X * 2 + COL_W * len(MODULES)
CANVAS_H = 820

_elements = []
_seed = 1000


def _nid(kind):
    global _seed
    _seed += 1
    return f"{kind}{_seed}"


def box(x, y, w, h, fill, label, font=13, dashed=False, stroke=INK, bold=False,
        font_family=1):
    bid = _nid("b")
    tid = _nid("t")
    _elements.append({
        "id": bid, "type": "rectangle",
        "x": x, "y": y, "width": w, "height": h, "angle": 0,
        "strokeColor": stroke, "backgroundColor": fill,
        "fillStyle": "solid", "strokeWidth": 1.6,
        "strokeStyle": "dashed" if dashed else "solid",
        "roughness": 1, "opacity": 100, "groupIds": [], "frameId": None,
        "roundness": {"type": 3}, "seed": _seed * 7, "version": _seed,
        "versionNonce": _seed * 3, "isDeleted": False,
        "boundElements": [{"id": tid, "type": "text"}], "updated": _seed,
        "link": None, "locked": False,
    })
    _elements.append({
        "id": tid, "type": "text",
        "x": x, "y": y + h / 2 - font, "width": w - 16, "height": font * 1.6,
        "angle": 0, "strokeColor": INK, "backgroundColor": "transparent",
        "fillStyle": "solid", "strokeWidth": 1, "strokeStyle": "solid",
        "roughness": 1, "opacity": 100, "groupIds": [], "frameId": None,
        "roundness": None, "seed": _seed * 11, "version": _seed,
        "versionNonce": _seed * 13, "isDeleted": False,
        "boundElements": None, "updated": _seed, "link": None, "locked": False,
        "fontSize": font, "fontFamily": font_family,
        "text": label, "textAlign": "center", "verticalAlign": "middle",
        "containerId": bid, "originalText": label,
    })


def text_raw(x, y, label, font=20, color=INK, align="left", bold=False):
    _elements.append({
        "id": _nid("t"), "type": "text",
        "x": x, "y": y, "width": len(label) * font + 20, "height": font * 1.6,
        "angle": 0, "strokeColor": color, "backgroundColor": "transparent",
        "fillStyle": "solid", "strokeWidth": 1, "strokeStyle": "solid",
        "roughness": 1, "opacity": 100, "groupIds": [], "frameId": None,
        "roundness": None, "seed": _seed * 17, "version": _seed,
        "versionNonce": _seed * 19, "isDeleted": False,
        "boundElements": None, "updated": _seed, "link": None, "locked": False,
        "fontSize": font, "fontFamily": 1,
        "text": label, "textAlign": align, "verticalAlign": "top",
        "containerId": None, "originalText": label,
    })


# 标题 + 注脚
text_raw(60, 18, "刘全 · 信息架构现状图（2026-09-04，v0.7 复核后）", 24, INK)
text_raw(60, CANVAS_H - 90,
         "红色虚线 = 占位页（建设中）｜ 一级菜单 6 个：工作台/任务中心/CRM/ERP/SEO/设置",
         14, DIM)

# 根
root_w = 260
root_x = (CANVAS_W - root_w) / 2
box(root_x, ROOT_Y, root_w, 54, "#ffec99",
    "刘全系统 · 登录（管理员/运营/采购）", font=16, bold=True)

# 模块列 + 叶子
for i, (mid, mname, mcolor, leaves) in enumerate(MODULES):
    cx = MARGIN_X + i * COL_W
    box(cx, MOD_Y, 170, 46, mcolor, mname, font=15, bold=True)
    k = 0
    for label, ph in leaves:
        ly = LEAF_Y0 + k * (LEAF_H + LEAF_GAP)
        label_show = label + ("（占位）" if ph else "")
        if ph:
            box(cx, ly, 170, LEAF_H, "#fff5f5", label_show, font=11,
                dashed=True, stroke=RED)
        else:
            box(cx, ly, 170, LEAF_H, "#ffffff", label_show, font=11)

# 图例框
box(60, CANVAS_H - 70, 170, 40, "#fff5f5", "占位页（建设中）", font=12,
    dashed=True, stroke=RED)

doc = {
    "type": "excalidraw",
    "version": 2,
    "source": "local-generator",
    "elements": _elements,
    "appState": {
        "gridSize": None,
        "viewBackgroundColor": "#ffffff",
        "zoom": {"value": 0.62},
        "currentItemFontFamily": 1,
    },
    "files": {},
}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False)
print("written:", OUT, "elements:", len(_elements))
