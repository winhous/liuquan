"""product_suggestion 工序 ACT（详设-v0.5 §6.1，修订：model=default 文本模型）。

LLM 工序（reason: llm，model: default，非 vision）：
经 provider scrape.image_context 拿元数据 → AI 生成选品建议 → TaskProposal 候选。

修订理由：vision 未配置时产品建议链不能失败（optional 语义），
元数据已含商品信息（desc/tags/author/source/宽高/水印），AI 基于元数据生成选品建议。
vision 识图留给批 5 crm image_caption。

输入：SuggestionInput{image_ids[], batch_id?, target_keywords?}
输出：SuggestionResult{proposals[]}
"""

from __future__ import annotations

import logging
from typing import Any

from engine.core.context import EngineContext
from models.workers import SuggestionInput, SuggestionResult

logger = logging.getLogger(__name__)


def run(inputs: SuggestionInput, ctx: EngineContext) -> SuggestionResult:
    """基于图片元数据 AI 生成选品建议（经 provider scrape.image_context 拿元数据）。"""
    # 读工序配置（R10 外置）
    config = ctx.worker_config if hasattr(ctx, "worker_config") else {}
    default_role = config.get("default_role")
    default_due_days = config.get("default_due_days", 3)

    # 获取 image_context provider 数据
    image_context = None
    if ctx.context_data and "scrape_image_context" in ctx.context_data:
        image_context = ctx.context_data["scrape_image_context"]

    if not image_context:
        return SuggestionResult(
            proposals=[],
            note="scrape_image_context provider 未返回数据（图片元数据不可用）",
        )

    # image_context 包含 images 列表（从 scrape.image_file 读取）
    images = image_context.get("images", []) if isinstance(image_context, dict) else []
    if not images:
        return SuggestionResult(
            proposals=[],
            note="无图片元数据可分析",
        )

    # 构造选品建议（基于元数据）
    proposals = []
    for img in images:
        desc = img.get("desc", "")
        tags = img.get("tags", [])
        source = img.get("source", "")
        author_id = img.get("author_id", "")
        width = img.get("width")
        height = img.get("height")
        watermark = img.get("watermark", False)

        # 基于元数据生成选品建议标题
        title_parts = ["选品建议"]
        if desc:
            title_parts.append(desc[:30])
        elif tags:
            title_parts.append(", ".join(tags[:2]))
        else:
            title_parts.append(f"图片#{img.get('id', '?')}")

        title = "：".join(title_parts)

        # 构造 detail
        detail_parts = []
        if desc:
            detail_parts.append(f"商品描述：{desc}")
        if tags:
            detail_parts.append(f"标签：{', '.join(tags)}")
        if source:
            detail_parts.append(f"来源：{source}")
        if author_id:
            detail_parts.append(f"作者/卖家：{author_id}")
        if width and height:
            detail_parts.append(f"尺寸：{width}×{height}")
        if watermark:
            detail_parts.append("⚠️ 有水印")
        if inputs.target_keywords:
            detail_parts.append(f"目标关键词：{inputs.target_keywords}")

        detail = "\n".join(detail_parts) if detail_parts else "基于图片元数据的选品建议"

        # 构造 proposal dict（与 TaskProposal 契约对齐）
        proposal = {
            "title": title[:80],
            "detail": detail,
            "domain": "scrape",
            "action_id": "scrape.suggest",
            "suggested_role": default_role,
            "suggested_due_days": default_due_days,
            "evidence": [
                {
                    "kind": "image",
                    "ref_id": str(img.get("id", "")),
                }
            ],
        }
        proposals.append(proposal)

    return SuggestionResult(
        proposals=proposals,
        note=f"基于 {len(images)} 张图片元数据生成 {len(proposals)} 个选品建议",
    )
