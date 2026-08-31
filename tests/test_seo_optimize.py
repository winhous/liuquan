"""seo_optimize 工序单测（fake LLM，零网络，规范 R12）。

覆盖：
- 工序 LLM 输出归一化（照广成逻辑）
- 标题/标签/材质/alt 文本硬约束
- 降级场景（LLM 输出缺失）
"""

from __future__ import annotations

import pytest
from typing import Any

from engine.core.context import EngineContext
from models.workers import SeoOptimizeInput, SeoOptimizationReport, SeoProductText


def _make_ctx(
    product_text: SeoProductText = None,
    llm_output: dict = None,
) -> EngineContext:
    """构造测试用 EngineContext。"""
    inputs = SeoOptimizeInput(
        product_text=product_text or SeoProductText(
            title="Test Product",
            tags=["test", "product"],
            description="A test product",
        )
    )
    return EngineContext(
        worker_id="seo_optimize",
        domain="seo",
        inputs=inputs,
        config={},
        context_data={},
        llm_output=llm_output,
    )


def test_seo_optimize_basic():
    """seo_optimize 基本流程（LLM 输出归一化）。"""
    from engine.registry.workers.seo.seo_optimize.run import run

    llm_output = {
        "titles": [
            {"title": "Test Product Title 1", "angle": "Main"},
            {"title": "Test Product Title 2", "angle": "Alternative"},
        ],
        "tags": ["test", "product", "custom", "personalized", "gift", "handmade",
                 "unique", "artisan", "quality", "premium", "exclusive", "limited", "special"],
        "listing_description": "This is a test product description with SEO keywords.",
        "materials": ["Cotton", "Silk", "Leather"],
        "alt_text": "Test product image description",
        "suggested_category": "Home & Living > Home Decor",
        "seo_keywords": ["test product", "custom gift", "personalized item", "handmade craft"],
        "search_intent": "Transaction - looking to buy",
    }

    ctx = _make_ctx(llm_output=llm_output)
    result = run(ctx.inputs, ctx)

    assert isinstance(result, SeoOptimizationReport)
    assert result.original.title == "Test Product"
    assert len(result.titles) == 2
    assert result.titles[0]["title"] == "Test Product Title 1"
    assert result.titles[0]["angle"] == "Main"
    assert len(result.tags) == 13  # 恰好 13 个
    assert all(len(t) <= 20 for t in result.tags)  # 每个 ≤20 字符
    assert len(result.materials) == 3
    assert len(result.alt_text) <= 250
    assert len(result.seo_keywords) == 4


def test_seo_optimize_no_llm_output():
    """seo_optimize LLM 输出缺失（异常路径）。"""
    from engine.registry.workers.seo.seo_optimize.run import run

    ctx = _make_ctx(llm_output=None)
    result = run(ctx.inputs, ctx)

    assert isinstance(result, SeoOptimizationReport)
    assert "缺失" in result.note


def test_seo_optimize_tag_normalization():
    """seo_optimize 标签归一化（去 #、空白折叠、截断 20 字符、大小写去重、上限 13）。"""
    from engine.registry.workers.seo.seo_optimize.run import _normalize_tags

    # 测试去 #
    tags = ["#test", "product", "#custom"]
    result = _normalize_tags(tags)
    assert result == ["test", "product", "custom"]

    # 测试截断 20 字符
    tags = ["a" * 30, "short"]
    result = _normalize_tags(tags)
    assert result[0] == "a" * 20

    # 测试大小写去重
    tags = ["Test", "test", "TEST"]
    result = _normalize_tags(tags)
    assert len(result) == 1
    assert result[0] == "Test"

    # 测试上限 13
    tags = [f"tag{i}" for i in range(20)]
    result = _normalize_tags(tags)
    assert len(result) == 13


def test_seo_optimize_title_normalization():
    """seo_optimize 标题归一化（≤140 字符，上限 5 个）。"""
    from engine.registry.workers.seo.seo_optimize.run import run

    # 超长标题
    llm_output = {
        "titles": [{"title": "A" * 200, "angle": "Long"}],
        "tags": ["test"],
    }

    ctx = _make_ctx(llm_output=llm_output)
    result = run(ctx.inputs, ctx)

    assert len(result.titles) == 1
    assert len(result.titles[0]["title"]) <= 140


def test_seo_optimize_material_normalization():
    """seo_optimize 材质归一化（≤13 个）。"""
    from engine.registry.workers.seo.seo_optimize.run import run

    llm_output = {
        "titles": [{"title": "Test", "angle": "Main"}],
        "tags": ["test"],
        "materials": [f"Material{i}" for i in range(20)],
    }

    ctx = _make_ctx(llm_output=llm_output)
    result = run(ctx.inputs, ctx)

    assert len(result.materials) <= 13


def test_seo_optimize_alt_text_normalization():
    """seo_optimize alt 文本归一化（≤250 字符）。"""
    from engine.registry.workers.seo.seo_optimize.run import run

    llm_output = {
        "titles": [{"title": "Test", "angle": "Main"}],
        "tags": ["test"],
        "alt_text": "A" * 300,
    }

    ctx = _make_ctx(llm_output=llm_output)
    result = run(ctx.inputs, ctx)

    assert len(result.alt_text) <= 250


def test_seo_optimize_playbook_key():
    """seo_optimize playbook key 透传。"""
    from engine.registry.workers.seo.seo_optimize.run import run

    llm_output = {
        "titles": [{"title": "Test", "angle": "Main"}],
        "tags": ["test"],
    }

    inputs = SeoOptimizeInput(
        product_text=SeoProductText(title="Test", tags=["test"], description="Desc"),
        playbook_key="jewelry",
    )
    ctx = EngineContext(
        worker_id="seo_optimize",
        domain="seo",
        inputs=inputs,
        config={},
        context_data={},
        llm_output=llm_output,
    )

    result = run(ctx.inputs, ctx)
    assert result.playbook == "jewelry"
