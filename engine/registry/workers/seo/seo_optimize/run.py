"""seo_optimize 工序 ACT（详设-v0.5 §6.1；照广成 seo-optimize/run.py 逻辑）。

LLM 工序（worker.yaml 的 reason: llm，model: default）：
三段一上下文 prompt（商品理解→关键词策略→文案生成，照广成 seo-optimize SKILL.md + run.py）。

LLM 网关用刘全 PydanticAI output_type + re-ask（worker.yaml 声明 output model，
runner REASON 相位自动校验，工序 run() 拿 llm_output 做归一化组装——不要在 run.py 里直调 LLM，R4）。

归一化 = 纯 Python 硬约束（标签/标题/材质/alt 上限在 config 可改，R10 规格外置）。

注意：image_path 字段保留可选但本批不接 vision（注释标注「vision 图片理解待批 4/5 接入」，
纯文字三段，与广成现状一致）。
"""

from __future__ import annotations

from typing import Any

from engine.core.context import EngineContext
from models.workers import SeoOptimizationReport, SeoOptimizeInput


def run(inputs: SeoOptimizeInput, ctx: EngineContext) -> SeoOptimizationReport:
    """SEO 优化：分析商品→关键词策略→文案生成（三段一上下文，LLM 工序）。"""

    # 从 llm_output 拿 LLM 结构化结果（runner REASON 相位已校验）
    llm_output = ctx.llm_output
    if llm_output is None:
        # 未过 REASON 相位（异常路径），返回空报告
        return SeoOptimizationReport(
            original=inputs.product_text,
            note="LLM 输出缺失（REASON 相位未执行）",
        )

    # 归一化处理（照广成逻辑，纯 Python 硬约束；规格从 config/ 读，R10）
    report = _normalize_report(llm_output, inputs, ctx)

    return report


def _spec_for(ctx: EngineContext) -> dict[str, Any]:
    """工序规格（R10 规格外置）：读 config/spec.yaml 合并结果 ctx.config["spec"]。

    缺省回退硬默认（与 SeoOptimizationReport 契约注释对齐）；config 缺失/
    非映射 → 空规格（全默认），保证无 spec 的旧调用（config={}）行为不变。
    """
    raw = (ctx.config or {}).get("spec") if ctx.config else None
    return raw if isinstance(raw, dict) else {}


def _section(spec: dict[str, Any], key: str) -> dict[str, Any]:
    return spec.get(key) if isinstance(spec.get(key), dict) else {}


def _normalize_report(
    llm_output: dict[str, Any], inputs: SeoOptimizeInput, ctx: EngineContext
) -> SeoOptimizationReport:
    """归一化 LLM 输出（照广成 _normalize_tags/_normalize_titles 逻辑）。

    各上限/长度规格 = config/spec.yaml 可改（改规格不改代码，A51 行为实锤）；
    数字字面量只作缺省回退（spec 缺键时），不入业务规格外置。
    """

    spec = _spec_for(ctx)
    titles_cfg = _section(spec, "titles")
    tags_cfg = _section(spec, "tags")
    materials_cfg = _section(spec, "materials")
    alt_cfg = _section(spec, "alt_text")
    seo_cfg = _section(spec, "seo_keywords")

    titles_max = int(titles_cfg.get("max", 5))
    titles_max_length = int(titles_cfg.get("max_length", 140))
    tags_max = int(tags_cfg.get("max", 13))
    tag_max_length = int(tags_cfg.get("max_length", 20))
    materials_max = int(materials_cfg.get("max", 13))
    materials_max_length = int(materials_cfg.get("max_length", 50))
    alt_max_length = int(alt_cfg.get("max_length", 250))
    seo_keywords_max = int(seo_cfg.get("max", 10))
    seo_keywords_max_length = int(seo_cfg.get("max_length", 50))

    # 标题归一化：上限与单条长度规格可改（缺省 max 5 / 每条 ≤140 字符）
    titles_raw = llm_output.get("titles", [])
    titles = []
    for t in titles_raw[:titles_max]:
        if isinstance(t, dict):
            title_text = t.get("title", "")[:titles_max_length]
            angle = t.get("angle", "")
            titles.append({"title": title_text, "angle": angle})
        elif isinstance(t, str):
            titles.append({"title": t[:titles_max_length], "angle": ""})

    # 标签归一化：上限/每条长度规格可改（缺省 13 个 / 每个 ≤20 字符），
    # 去 #，大小写不敏感去重
    tags_raw = llm_output.get("tags", [])
    tags_normalized = _normalize_tags(
        tags_raw, max_tag_length=tag_max_length, max_tags=tags_max
    )

    # 描述归一化：重写优化
    listing_description = str(llm_output.get("listing_description", ""))

    # 材质归一化：上限/每条长度规格可改（缺省 ≤13 个）
    materials_raw = llm_output.get("materials", [])
    materials = [
        str(m)[:materials_max_length] for m in materials_raw[:materials_max]
    ]

    # Alt 文本归一化：长度规格可改（缺省 ≤250 字符）
    alt_text = str(llm_output.get("alt_text", ""))[:alt_max_length]

    # 建议类目
    suggested_category = str(llm_output.get("suggested_category", ""))

    # SEO 关键词：上限/每条长度规格可改（缺省 6-10 个）
    seo_keywords_raw = llm_output.get("seo_keywords", [])
    seo_keywords = [
        str(k)[:seo_keywords_max_length] for k in seo_keywords_raw[:seo_keywords_max]
    ]

    # 搜索意图
    search_intent = str(llm_output.get("search_intent", ""))

    # 原始输入
    original = inputs.product_text

    # Playbook key
    playbook = inputs.playbook_key or ""

    return SeoOptimizationReport(
        original=original,
        titles=titles,
        tags=tags_normalized,
        listing_description=listing_description,
        materials=materials,
        alt_text=alt_text,
        suggested_category=suggested_category,
        seo_keywords=seo_keywords,
        search_intent=search_intent,
        playbook=playbook,
    )


def _normalize_tags(
    tags: list[str],
    *,
    max_tag_length: int = 20,
    max_tags: int = 13,
) -> list[str]:
    """标签归一化（照广成 _normalize_tags 逻辑）：去 #、空白折叠、截断、
    大小写不敏感去重、上限。阈值可注入（R10 规格外置，默认值在签名）。"""
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        # 去 # 和空白
        t = str(tag).strip().lstrip("#").strip()
        if not t:
            continue
        # 截断
        t = t[:max_tag_length]
        # 大小写不敏感去重
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(t)
        if len(result) >= max_tags:
            break
    return result
