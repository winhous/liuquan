"""v0.3 T2 workers Model 测试（详设-v0.3 §5.1 工序 Model / §5.2 Context provider Model）。

纯模型层测试，零 DB 依赖（不 import 嵌入式 PG fixture，不与其他子代理抢簇）：
- 全部新 Model 的 pydantic 往返（构造 -> model_dump(json) -> model_validate -> 相等）
- 缺省值语义（空串 / 空列表 / None，对齐详设字段级）
- 约束正反：必填缺失拒、Literal 非法值拒、EvidenceRef 与 models.contract.task 对齐
- 前向引用：TodoCandidateItem.suggested_next 解析为 SuggestedNext（含 / 缺两态）
- 导出名：全部新 Model 进 models.workers.__all__（loader L3 引用契约）

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
不写 URL/IP/sk- 前缀字面量、不给敏感名赋字面量、不读 os.environ。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from models.contract.task import EvidenceRef as ContractEvidenceRef
from models.workers import (
    ChatContextData,
    ChatContextParams,
    ChatTranscriptInput,
    ChatTranscriptResult,
    CustomerBrief,
    CustomerSnapshotResult,
    EventBrief,
    IntentInput,
    IntentResult,
    MessageBrief,
    ReplyDraftInput,
    ReplyDraftResult,
    SnapshotBrief,
    SnapshotUpdateInput,
    SuggestedNext,
    TaskContextData,
    TaskContextParams,
    TodoCandidateItem,
    TodoCandidateResult,
    TodoGenerateInput,
    TranslationItem,
)


def _evidence(ref_id: str = "m-1") -> ContractEvidenceRef:
    """最小合法证据引用（对齐四契约 EvidenceRef：kind/ref_id/quote，决策 16）。"""
    return ContractEvidenceRef(kind="message", ref_id=ref_id, quote="我要退货")


# ==== 往返：构造 -> model_dump(json) -> 重新校验 ====


def test_new_models_roundtrip() -> None:
    """全部新 Model：构造 -> model_dump -> 重新校验，往返一致（R2 输出即类型）。"""
    snapshot = CustomerSnapshotResult(
        current_need="需要定制款报价",
        need_history=["想了解定制", "需要定制款报价"],
        sentiment="积极",
        todos=["跟进定制款报价"],
        summary="客户在询定制款，已报价跟进中",
    )
    item = TodoCandidateItem(
        content="跟进定制款报价",
        reason="客户明确要求报价",
        suggested_tags=["报价", "定制"],
        evidence=[_evidence()],
        suggested_next=SuggestedNext(
            action="assign", target_role="运营", tags=["报价"], note="生成报价单"
        ),
    )
    trans = TranslationItem(
        source_text="hello", translated_text="你好", direction="buyer", language="en"
    )
    cases: list[BaseModel] = [
        ChatTranscriptInput(customer_id=7, conversation_text="hello\nworld"),
        TranslationItem(source_text="hi", direction="seller", language="zh"),
        trans,
        ChatTranscriptResult(
            translations=[trans, TranslationItem(source_text="bye", direction="seller")],
            source_lang="en",
            target_lang="zh",
        ),
        ChatTranscriptResult(translations=[]),
        SnapshotUpdateInput(translations=[trans]),
        snapshot,
        CustomerSnapshotResult(),
        TodoGenerateInput(snapshot=snapshot),
        item,
        TodoCandidateItem(content="无建议的候选"),
        TodoCandidateResult(todos=[item, TodoCandidateItem(content="第二候选")]),
        SuggestedNext(action="transfer", target_domain="erp", note="流转 ERP 采购"),
        SuggestedNext(action="tag", tags=["采购"]),
        SuggestedNext(action="note", note="挂起等物料"),
        ReplyDraftInput(customer_id=5),
        ReplyDraftInput(customer_id=5, mode="points", points="1. 感谢询价\n2. 报价如下"),
        ReplyDraftInput(customer_id=5, mode="literal", full_text="原文回传"),
        ReplyDraftResult(reply_en="Thanks for your inquiry", reply_zh="感谢您的询价"),
        IntentInput(task_id=9, instruction="流转到 ERP 采购"),
        IntentResult(action="assign", target_role="运营", clarity="clear"),
        IntentResult(
            action="transfer",
            target_domain="erp",
            clarity="unclear",
            candidates=["流转到 ERP 采购申请", "流转到 ERP 库存"],
        ),
        IntentResult(action="block", note="挂起等物料", clarity="clear"),
        ChatContextParams(customer_id=3),
        CustomerBrief(id=1, nickname="Alice", latest_summary="在询定制款"),
        CustomerBrief(id=1, nickname="Bob"),
        MessageBrief(id=10, source_text="hello", translated_text="你好", direction="buyer"),
        MessageBrief(id=11, source_text="bye"),
        SnapshotBrief(
            id=5, current_need="x", need_history=["a"], sentiment="s",
            todos=["t"], summary="u",
        ),
        ChatContextData(
            customer=CustomerBrief(id=1, nickname="Alice"),
            messages=[MessageBrief(id=10, source_text="hello", direction="buyer")],
            snapshot=SnapshotBrief(id=5),
            existing_open_todos=["跟进报价"],
        ),
        ChatContextData(customer=CustomerBrief(id=1, nickname="Alice"), messages=[]),
        TaskContextParams(task_id=9),
        EventBrief(event_type="created", note="任务创建", created_at="2026-08-28T00:00:00Z"),
        TaskContextData(
            task_id=9, title="处理退货", domain="crm", status="open",
            tags=["售后"], recent_events=[EventBrief(event_type="created")],
        ),
    ]
    for model in cases:
        cls = type(model)
        dumped = model.model_dump(mode="json")
        rebuilt = cls.model_validate(dumped)
        assert rebuilt == model


# ==== 缺省值语义 ====


def test_new_model_defaults() -> None:
    """缺省值：空串 / 空列表 / None 对齐详设字段级。"""
    tr = TranslationItem(source_text="hi", direction="buyer")
    assert tr.translated_text == "" and tr.language == ""

    result = ChatTranscriptResult(translations=[])
    assert result.source_lang is None and result.target_lang is None

    snap = CustomerSnapshotResult()
    assert snap.current_need == "" and snap.need_history == [] and snap.sentiment == ""
    assert snap.todos == [] and snap.summary == ""

    draft = ReplyDraftInput(customer_id=5)
    assert draft.mode == "auto" and draft.points == "" and draft.full_text == ""

    reply = ReplyDraftResult(reply_en="hi")
    assert reply.reply_zh == ""

    cand = TodoCandidateItem(content="跟进报价")
    assert cand.reason == "" and cand.suggested_tags == [] and cand.evidence == []
    assert cand.suggested_next is None

    next_ = SuggestedNext(action="note")
    assert next_.target_domain is None and next_.target_role is None
    assert next_.tags is None and next_.note is None

    brief = MessageBrief(id=1, source_text="x")
    assert brief.translated_text == "" and brief.direction == "buyer"

    sb = SnapshotBrief(id=1)
    assert sb.current_need == "" and sb.need_history == [] and sb.sentiment == ""
    assert sb.todos == [] and sb.summary == ""

    ctx = ChatContextData(customer=CustomerBrief(id=1, nickname="A"), messages=[])
    assert ctx.snapshot is None and ctx.existing_open_todos == []

    ev = EventBrief(event_type="created")
    assert ev.note is None and ev.created_at is None

    tctx = TaskContextData(task_id=1, title="t", domain="d", status="open")
    assert tctx.tags == [] and tctx.recent_events == []


# ==== 约束正反：必填缺失拒 ====


def test_required_field_missing_rejected() -> None:
    """必填缺失拒：content / clarity / instruction / direction / customer 等。"""
    with pytest.raises(ValidationError):
        TodoCandidateItem()  # content 必填
    with pytest.raises(ValidationError):
        TodoCandidateItem(reason="只有理由没有内容")  # content 仍缺
    with pytest.raises(ValidationError):
        IntentResult(action="assign")  # clarity 必填
    with pytest.raises(ValidationError):
        IntentResult(clarity="clear")  # action 必填
    with pytest.raises(ValidationError):
        IntentInput()  # task_id / instruction 必填
    with pytest.raises(ValidationError):
        IntentInput(instruction="x")  # task_id 必填
    with pytest.raises(ValidationError):
        ReplyDraftInput()  # customer_id 必填
    with pytest.raises(ValidationError):
        ReplyDraftInput(mode="auto")  # customer_id 仍缺
    with pytest.raises(ValidationError):
        ChatTranscriptInput(conversation_text="x")  # customer_id 必填
    with pytest.raises(ValidationError):
        ChatTranscriptInput(customer_id=1)  # conversation_text 必填
    with pytest.raises(ValidationError):
        TranslationItem(source_text="x")  # direction 必填
    with pytest.raises(ValidationError):
        SnapshotUpdateInput()  # translations 必填
    with pytest.raises(ValidationError):
        TodoGenerateInput()  # snapshot 必填
    with pytest.raises(ValidationError):
        ReplyDraftResult()  # reply_en 必填
    with pytest.raises(ValidationError):
        ChatContextParams()  # customer_id 必填
    with pytest.raises(ValidationError):
        TaskContextParams()  # task_id 必填
    with pytest.raises(ValidationError):
        CustomerBrief(nickname="A")  # id 必填
    with pytest.raises(ValidationError):
        MessageBrief(source_text="x")  # id 必填
    with pytest.raises(ValidationError):
        TaskContextData(title="t", domain="d", status="open")  # task_id 必填
    with pytest.raises(ValidationError):
        ChatContextData(customer=CustomerBrief(id=1, nickname="A"))  # messages 必填
    with pytest.raises(ValidationError):
        ChatContextData(messages=[])  # customer 必填


# ==== 约束正反：Literal 非法值拒 ====


def test_literal_invalid_values_rejected() -> None:
    """Literal 约束：direction / action / mode / clarity 非法值拒。"""
    with pytest.raises(ValidationError):
        TranslationItem(source_text="x", direction="guest")
    with pytest.raises(ValidationError):
        SuggestedNext(action="block")  # block 不在 SuggestedNext 动作集
    with pytest.raises(ValidationError):
        IntentResult(action="fly", clarity="clear")
    with pytest.raises(ValidationError):
        ReplyDraftInput(mode="draft")
    with pytest.raises(ValidationError):
        IntentResult(action="assign", clarity="fuzzy")


# ==== EvidenceRef 与 models.contract.task 对齐 ====


def test_evidence_ref_aligned_with_contract() -> None:
    """EvidenceRef 复用 contract：同类型实例原样持有 + 契约字段约束生效。"""
    ref = ContractEvidenceRef(kind="message", ref_id="m-1", quote="我要退货")
    cand = TodoCandidateItem(content="跟进退货", evidence=[ref])
    assert cand.evidence[0] is ref  # pydantic v2 对同类型实例不复制
    assert cand.model_dump(mode="json")["evidence"] == [
        {"kind": "message", "ref_id": "m-1", "quote": "我要退货"}
    ]
    with pytest.raises(ValidationError):
        ContractEvidenceRef(kind="order", ref_id="x")  # kind 非法
    with pytest.raises(ValidationError):
        ContractEvidenceRef(kind="message")  # ref_id 必填
    # 空 evidence 合法（禁幻觉非空校验在代码层，模型层只定形状，对齐详设 §5.1）
    assert TodoCandidateItem(content="x").evidence == []


# ==== 前向引用：TodoCandidateItem.suggested_next ====


def test_todo_candidate_suggested_next_forward_ref() -> None:
    """前向引用：suggested_next 解析为 SuggestedNext（含 / 缺两态）。"""
    item = TodoCandidateItem(
        content="跟进报价",
        suggested_next=SuggestedNext(action="assign", target_role="采购", tags=["采购"]),
    )
    assert isinstance(item.suggested_next, SuggestedNext)
    assert item.model_dump(mode="json")["suggested_next"] == {
        "action": "assign",
        "target_domain": None,
        "target_role": "采购",
        "tags": ["采购"],
        "note": None,
    }
    # 缺省 None 往返
    plain = TodoCandidateItem(content="跟进报价")
    assert plain.suggested_next is None
    rebuilt = TodoCandidateItem.model_validate(plain.model_dump(mode="json"))
    assert rebuilt.suggested_next is None
    # 字段注解与导出名同源（loader L3 按名解析）
    assert "SuggestedNext" in str(TodoCandidateItem.model_fields["suggested_next"].annotation)


# ==== IntentResult clarity 语义（决策 28：模糊澄清不猜测执行）====


def test_intent_result_clarity_semantics() -> None:
    """clarity=clear 默认无 candidates；unclear 可带澄清选项（也可为空，页面层提示）。"""
    clear = IntentResult(action="assign", target_role="运营", clarity="clear")
    assert clear.candidates is None
    unclear = IntentResult(
        action="transfer",
        target_domain="erp",
        clarity="unclear",
        candidates=["流转到 ERP 采购申请", "流转到 ERP 库存"],
    )
    assert len(unclear.candidates) == 2
    assert IntentResult(action="transfer", clarity="unclear").candidates is None


# ==== 导出名契约（loader L3 引用）====


def test_new_model_export_names_in_all() -> None:
    """全部新 Model 名进 models.workers.__all__（worker.yaml / context yaml 引用契约）。"""
    import models.workers as mw

    expected = {
        "ChatTranscriptInput",
        "TranslationItem",
        "ChatTranscriptResult",
        "SnapshotUpdateInput",
        "CustomerSnapshotResult",
        "TodoGenerateInput",
        "TodoCandidateItem",
        "TodoCandidateResult",
        "SuggestedNext",
        "ReplyDraftInput",
        "ReplyDraftResult",
        "IntentInput",
        "IntentResult",
        "ChatContextParams",
        "CustomerBrief",
        "MessageBrief",
        "SnapshotBrief",
        "ChatContextData",
        "TaskContextParams",
        "EventBrief",
        "TaskContextData",
    }
    assert expected <= set(mw.__all__)
    for name in expected:
        assert hasattr(mw, name)
