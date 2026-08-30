"""v0.3 T4（主代理接管实现）：crm 四工序 run() 直调测试（P3-1 契约，零网络零 DB）。

- chat_translate：消费 llm_output 返回；空译文/空原文 -> ValueError
- snapshot_update：消费 llm_output 返回；summary/current_need 全空 -> ValueError
- todo_generate：无证据项剔除；全无依据 -> ValueError（决策 16 无依据不出建议）
- customer_reply_draft：三模式；literal reply_zh=full_text 原样回传；points/literal
  缺输入 -> ValueError
"""

from __future__ import annotations

import importlib

import pytest
from engine.core.context import EngineContext
from models.contract.task import EvidenceRef
from models.workers import (
    ChatTranscriptInput,
    ChatTranscriptResult,
    CustomerSnapshotResult,
    ReplyDraftInput,
    ReplyDraftResult,
    SnapshotUpdateInput,
    SuggestedNext,
    TodoCandidateItem,
    TodoCandidateResult,
    TodoGenerateInput,
    TranslationItem,
)


def _ctx(inputs, llm_output, config=None) -> EngineContext:
    return EngineContext(
        worker_id="x",
        domain="crm",
        inputs=inputs,
        config=config or {},
        context_data={},
        llm_output=llm_output,
    )


def _load(domain: str, name: str):
    return importlib.import_module(f"engine.registry.workers.{domain}.{name}.run")


def _item(src: str, trans: str = "译", direction: str = "buyer") -> TranslationItem:
    return TranslationItem(source_text=src, translated_text=trans, direction=direction)


def _ev(ref_id: str = "1") -> EvidenceRef:
    return EvidenceRef(kind="message", ref_id=ref_id, quote="原文")


# ==== chat_translate ====


def test_chat_translate_returns_llm_output() -> None:
    run = _load("crm", "chat_translate").run
    out = ChatTranscriptResult(
        translations=[_item("hello"), _item("bye", direction="seller")]
    )
    result = run(
        ChatTranscriptInput(customer_id=1, conversation_text="hello\nbye"),
        _ctx(ChatTranscriptInput(customer_id=1, conversation_text="hello\nbye"), out),
    )
    assert result is out


def test_chat_translate_empty_translations_rejected() -> None:
    run = _load("crm", "chat_translate").run
    out = ChatTranscriptResult(translations=[])
    with pytest.raises(ValueError):
        run(
            ChatTranscriptInput(customer_id=1, conversation_text="hi"),
            _ctx(ChatTranscriptInput(customer_id=1, conversation_text="hi"), out),
        )


def test_chat_translate_empty_source_rejected() -> None:
    run = _load("crm", "chat_translate").run
    out = ChatTranscriptResult(translations=[_item("", "译")])
    with pytest.raises(ValueError):
        run(
            ChatTranscriptInput(customer_id=1, conversation_text="hi"),
            _ctx(ChatTranscriptInput(customer_id=1, conversation_text="hi"), out),
        )


# ==== snapshot_update ====


def test_snapshot_update_returns_llm_output() -> None:
    run = _load("crm", "snapshot_update").run
    out = CustomerSnapshotResult(
        current_need="定制花束", need_history=["首次联系"], sentiment="积极", summary="买家想定制"
    )
    inputs = SnapshotUpdateInput(translations=[_item("hi")], customer_id=1)
    result = run(inputs, _ctx(inputs, out))
    assert result is out


def test_snapshot_update_empty_rejected() -> None:
    run = _load("crm", "snapshot_update").run
    out = CustomerSnapshotResult()
    inputs = SnapshotUpdateInput(translations=[_item("hi")], customer_id=1)
    with pytest.raises(ValueError):
        run(inputs, _ctx(inputs, out))


# ==== todo_generate ====


def test_todo_generate_keeps_evidence_items() -> None:
    run = _load("crm", "todo_generate").run
    good = TodoCandidateItem(
        content="确认花材组合及婚礼日期",
        reason="卖家答应确认",
        suggested_tags=["报价"],
        evidence=[_ev("1")],
        suggested_next=SuggestedNext(action="transfer", target_domain="erp", note="建议流转"),
    )
    no_evidence = TodoCandidateItem(content="无依据项", reason="x")
    out = TodoCandidateResult(todos=[good, no_evidence])
    inputs = TodoGenerateInput(
        snapshot=CustomerSnapshotResult(summary="s"), customer_id=1
    )
    result = run(inputs, _ctx(inputs, out))
    assert len(result.todos) == 1
    assert result.todos[0].content == "确认花材组合及婚礼日期"
    assert result.todos[0].suggested_next is not None


def test_todo_generate_all_unfounded_rejected() -> None:
    run = _load("crm", "todo_generate").run
    out = TodoCandidateResult(todos=[TodoCandidateItem(content="无依据", reason="x")])
    inputs = TodoGenerateInput(snapshot=CustomerSnapshotResult(summary="s"), customer_id=1)
    with pytest.raises(ValueError):
        run(inputs, _ctx(inputs, out))


# ==== customer_reply_draft ====


def test_reply_draft_auto_ok() -> None:
    run = _load("crm", "customer_reply_draft").run
    out = ReplyDraftResult(reply_en="Hi there!", reply_zh="你好！")
    inputs = ReplyDraftInput(customer_id=1, mode="auto")
    result = run(inputs, _ctx(inputs, out))
    assert result is out


def test_reply_draft_literal_returns_full_text_as_zh() -> None:
    run = _load("crm", "customer_reply_draft").run
    out = ReplyDraftResult(reply_en="Translated", reply_zh="模型回述")
    inputs = ReplyDraftInput(customer_id=1, mode="literal", full_text="卖家中文原文")
    result = run(inputs, _ctx(inputs, out))
    assert result.reply_en == "Translated"
    assert result.reply_zh == "卖家中文原文"  # literal：原样回传，不经模型


def test_reply_draft_points_requires_points() -> None:
    run = _load("crm", "customer_reply_draft").run
    inputs = ReplyDraftInput(customer_id=1, mode="points", points="")
    with pytest.raises(ValueError):
        run(inputs, _ctx(inputs, ReplyDraftResult(reply_en="x")))


def test_reply_draft_literal_requires_full_text() -> None:
    run = _load("crm", "customer_reply_draft").run
    inputs = ReplyDraftInput(customer_id=1, mode="literal", full_text="")
    with pytest.raises(ValueError):
        run(inputs, _ctx(inputs, ReplyDraftResult(reply_en="x")))


def test_reply_draft_empty_reply_en_rejected() -> None:
    run = _load("crm", "customer_reply_draft").run
    inputs = ReplyDraftInput(customer_id=1, mode="auto")
    with pytest.raises(ValueError):
        run(inputs, _ctx(inputs, ReplyDraftResult(reply_en="")))
