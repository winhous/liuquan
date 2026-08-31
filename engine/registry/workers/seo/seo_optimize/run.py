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

    # 归一化处理（照广成逻辑，纯 Python 硬约束）
    report = _normalize_report(llm_output, inputs)

    return report


def _normalize_report(llm_output: dict[str, Any], inputs: SeoOptimizeInput) -> SeoOptimizationReport:
    """归一化 LLM 输出（照广成 _normalize_tags/_normalize_titles 逻辑）。"""

    # 标题归一化：3-5 个，每个 ≤140 字符
    titles_raw = llm_output.get("titles", [])
    titles = []
    for t in titles_raw[:5]:
        if isinstance(t, dict):
            title_text = t.get("title", "")[:140]
            angle = t.get("angle", "")
            titles.append({"title": title_text, "angle": angle})
        elif isinstance(t, str):
            titles.append({"title": t[:140], "angle": ""})

    # 标签归一化：恰好 13 个，每个 ≤20 字符，去除 #，大小写不敏感去重
    tags_raw = llm_output.get("tags", [])
    tags_normalized = _normalize_tags(tags_raw)

    # 描述归一化：重写优化
    listing_description = str(llm_output.get("listing_description", ""))

    # 材质归一化：≤13 个
    materials_raw = llm_output.get("materials", [])
    materials = [str(m)[:50] for m in materials_raw[:13]]

    # Alt 文本归一化：≤250 字符
    alt_text = str(llm_output.get("alt_text", ""))[:250]

    # 建议类目
    suggested_category = str(llm_output.get("suggested_category", ""))

    # SEO 关键词：6-10 个
    seo_keywords_raw = llm_output.get("seo_keywords", [])
    seo_keywords = [str(k)[:50] for k in seo_keywords_raw[:10]]

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
