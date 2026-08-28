"""T4a 四契约落位 models/contract/ 的单测（详设-v0.1 §6，字段级规范）。

覆盖（正反都写，防止契约改坏）：
- ContractEvent：正常构造、payload 任意 dict、dedup_key 缺省 None
- ActionDeclaration / ActionResult：正常构造；risk 只许
  read/suggest/write（transaction 拒载，详设 §6.3）
- SourceTrace / EvidenceRef：缺省值（audit_ids=[]、quote=None）、
  kind 枚举校验
- TaskProposal：title 长度（1..80）、priority/role 枚举、evidence 条数
  （<=20，pydantic v2 用 Field(max_length=20)）
- §6.4 业务校验函数：assert_suggest_has_evidence（evidence 空抛
  ValueError）、validate_audit_ids（注入 audit_lookup，非空 + 可查才 True）
- ContextProvider Protocol（§6.2 实现侧）：async def provide 实现类满足
  协议；桩类只住 tests/（R12/P3-4）

本文件自身在 lint P2 扫描对象内：不含 URL/IP/sk- 前缀字面量、
敏感名赋字面量、环境变量读取。
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from models.contract import (
    ActionDeclaration,
    ActionResult,
    ContextProvider,
    ContractEvent,
    EvidenceRef,
    SourceTrace,
    TaskProposal,
    assert_suggest_has_evidence,
    validate_audit_ids,
)

# ==== 构造辅助 ====


def _make_proposal(**overrides: Any) -> TaskProposal:
    data: dict[str, Any] = {
        "title": "跟进买家消息",
        "detail": "买家询问发货时间，需要运营跟进确认",
        "domain": "crm",
        "action_id": "crm.create_followup_task",
        "suggested_priority": "P1",
        "suggested_role": "运营",
        "suggested_due_days": 3,
        "evidence": [
            {"kind": "message", "ref_id": "msg-1001", "quote": "请问什么时候发货？"}
        ],
        "source": {
            "chain_id": "chat-inbox",
            "engine_task_id": "e-000001",
            "worker_id": "crm.translate",
            "audit_ids": ["a-1"],
        },
    }
    data.update(overrides)
    return TaskProposal(**data)


def _make_event(**overrides: Any) -> ContractEvent:
    data: dict[str, Any] = {
        "event_type": "crm.message_received",
        "domain": "crm",
        "source": "web.crm",
        "occurred_at": datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
        "payload": {"conversation_id": "c-1", "customer_id": "cu-9"},
    }
    data.update(overrides)
    return ContractEvent(**data)


# ==== 包导出（公共契约入口）====


def test_contract_package_exports_all_public_types() -> None:
    """models/contract/__init__ 导出全部公共类型与校验函数。"""
    from models import contract

    for name in (
        "ContractEvent",
        "ActionDeclaration",
        "ActionResult",
        "ContextProvider",
        "SourceTrace",
        "EvidenceRef",
        "TaskProposal",
        "assert_suggest_has_evidence",
        "validate_audit_ids",
    ):
        assert hasattr(contract, name), f"公共导出缺失：{name}"


# ==== §6.1 ContractEvent ====


def test_contract_event_constructs_with_all_fields() -> None:
    ev = _make_event()
    assert ev.event_type == "crm.message_received"
    assert ev.domain == "crm"
    assert ev.source == "web.crm"
    assert ev.occurred_at == datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    assert ev.payload == {"conversation_id": "c-1", "customer_id": "cu-9"}
    assert ev.dedup_key is None  # 缺省


def test_contract_event_payload_arbitrary_dict() -> None:
    """payload 是任意 dict[str, Any]：任意嵌套结构都收。"""
    payload: dict[str, Any] = {
        "nested": {"a": [1, 2, {"b": None}]},
        "flag": True,
        "count": 3,
        "text": "买家消息",
    }
    ev = _make_event(payload=payload)
    assert ev.payload == payload


def test_contract_event_dedup_key_optional() -> None:
    ev = _make_event(dedup_key="crm.message_received:c-1")
    assert ev.dedup_key == "crm.message_received:c-1"


# ==== §6.3 ActionDeclaration / ActionResult ====


def test_action_declaration_constructs() -> None:
    decl = ActionDeclaration(
        action_id="crm.create_followup_task",
        domain="crm",
        risk="suggest",
        output_model="FollowupProposal",
        target="tm.proposal",
    )
    assert decl.action_id == "crm.create_followup_task"
    assert decl.risk == "suggest"
    assert decl.output_model == "FollowupProposal"
    assert decl.target == "tm.proposal"


def test_action_declaration_transaction_risk_rejected() -> None:
    """§6.3：transaction 不可登记（一期无对外事务，L7 同源约束）。"""
    with pytest.raises(ValidationError):
        ActionDeclaration(
            action_id="bad.transaction",
            domain="crm",
            risk="transaction",
            output_model="X",
            target="tm.proposal",
        )


def test_action_declaration_unknown_risk_rejected() -> None:
    with pytest.raises(ValidationError):
        ActionDeclaration(
            action_id="bad.delete",
            domain="crm",
            risk="delete",
            output_model="X",
            target="tm.proposal",
        )


def test_action_declaration_read_and_write_risk_ok() -> None:
    for risk in ("read", "write"):
        decl = ActionDeclaration(
            action_id=f"demo.risk_{risk}",
            domain="demo",
            risk=risk,  # type: ignore[arg-type]
            output_model="X",
            target="tm.proposal",
        )
        assert decl.risk == risk


def test_action_result_constructs() -> None:
    res = ActionResult(
        action_id="crm.create_followup_task",
        risk="suggest",
        payload={"title": "跟进买家", "due_days": 3},
    )
    assert res.action_id == "crm.create_followup_task"
    assert res.risk == "suggest"
    assert res.payload == {"title": "跟进买家", "due_days": 3}


# ==== §6.4 SourceTrace / EvidenceRef ====


def test_source_trace_constructs_and_audit_ids_default_empty() -> None:
    st = SourceTrace(
        chain_id="chat-inbox",
        engine_task_id="e-000001",
        worker_id="crm.translate",
    )
    assert st.chain_id == "chat-inbox"
    assert st.engine_task_id == "e-000001"
    assert st.worker_id == "crm.translate"
    assert st.audit_ids == []  # 缺省空表


def test_source_trace_audit_ids_filled() -> None:
    st = SourceTrace(
        chain_id="chat-inbox",
        engine_task_id="e-000001",
        worker_id="crm.translate",
        audit_ids=["a-1", "a-2"],
    )
    assert st.audit_ids == ["a-1", "a-2"]


def test_evidence_ref_constructs_and_quote_default_none() -> None:
    ev = EvidenceRef(kind="listing", ref_id="l-88")
    assert ev.kind == "listing"
    assert ev.ref_id == "l-88"
    assert ev.quote is None


def test_evidence_ref_quote_filled() -> None:
    ev = EvidenceRef(kind="message", ref_id="msg-1001", quote="请问什么时候发货？")
    assert ev.quote == "请问什么时候发货？"


def test_evidence_ref_unknown_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceRef(kind="video", ref_id="x")  # type: ignore[arg-type]


# ==== §6.4 TaskProposal 模型约束 ====


def test_task_proposal_full_construct() -> None:
    p = _make_proposal()
    assert p.title == "跟进买家消息"
    assert p.detail == "买家询问发货时间，需要运营跟进确认"
    assert p.domain == "crm"
    assert p.action_id == "crm.create_followup_task"
    assert p.suggested_priority == "P1"
    assert p.suggested_role == "运营"
    assert p.suggested_due_days == 3
    assert isinstance(p.evidence[0], EvidenceRef)
    assert p.evidence[0].kind == "message"
    assert p.evidence[0].ref_id == "msg-1001"
    assert p.source.chain_id == "chat-inbox"
    assert p.source.engine_task_id == "e-000001"
    assert p.source.worker_id == "crm.translate"
    assert p.source.audit_ids == ["a-1"]


def test_task_proposal_suggested_due_days_none_ok() -> None:
    """P3 级任务无截止建议（详设 §6.4 注释：P0=0/P1=3/P2=7/P3=None）。"""
    p = _make_proposal(suggested_priority="P3", suggested_due_days=None)
    assert p.suggested_due_days is None


def test_task_proposal_title_too_long_rejected() -> None:
    with pytest.raises(ValidationError):
        _make_proposal(title="长" * 81)


def test_task_proposal_title_empty_rejected() -> None:
    with pytest.raises(ValidationError):
        _make_proposal(title="")


def test_task_proposal_title_boundary_80_ok() -> None:
    p = _make_proposal(title="标" * 80)
    assert len(p.title) == 80


def test_task_proposal_priority_invalid_rejected() -> None:
    for bad in ("P5", "p1", "紧急", ""):
        with pytest.raises(ValidationError):
            _make_proposal(suggested_priority=bad)


def test_task_proposal_priority_all_valid_ok() -> None:
    for prio in ("P0", "P1", "P2", "P3"):
        p = _make_proposal(suggested_priority=prio)  # type: ignore[arg-type]
        assert p.suggested_priority == prio


def test_task_proposal_role_invalid_rejected() -> None:
    for bad in ("客服", "经理", ""):
        with pytest.raises(ValidationError):
            _make_proposal(suggested_role=bad)


def test_task_proposal_role_all_valid_ok() -> None:
    for role in ("运营", "采购", "管理员"):
        p = _make_proposal(suggested_role=role)  # type: ignore[arg-type]
        assert p.suggested_role == role


def test_task_proposal_evidence_over_20_rejected() -> None:
    """pydantic v2：list 限长用 Field(max_length=20)（详设 §6.4 max_items=20 的 v2 写法）。"""
    evs = [{"kind": "metric", "ref_id": f"m-{i}"} for i in range(21)]
    with pytest.raises(ValidationError):
        _make_proposal(evidence=evs)


def test_task_proposal_evidence_20_ok() -> None:
    evs = [{"kind": "metric", "ref_id": f"m-{i}"} for i in range(20)]
    p = _make_proposal(evidence=evs)
    assert len(p.evidence) == 20


def test_task_proposal_empty_evidence_allowed_by_model() -> None:
    """模型层允许空 evidence；「无依据不出建议」是 §6.4 校验函数层约束。"""
    p = _make_proposal(evidence=[])
    assert p.evidence == []


# ==== §6.4 业务校验函数 ====


def test_assert_suggest_has_evidence_positive() -> None:
    """有 evidence：不抛。"""
    proposal = _make_proposal()
    assert_suggest_has_evidence(proposal)


def test_assert_suggest_has_evidence_empty_rejected() -> None:
    """反向：risk: suggest 工序产出提案但 evidence 为空 -> ValueError。"""
    proposal = _make_proposal(evidence=[])
    with pytest.raises(ValueError):
        assert_suggest_has_evidence(proposal)


def test_validate_audit_ids_positive() -> None:
    """audit_ids 非空且 lookup 可查 -> True。"""
    proposal = _make_proposal()
    assert validate_audit_ids(proposal, audit_lookup=lambda ids: True) is True


def test_validate_audit_ids_passes_ids_to_lookup() -> None:
    """audit_lookup 收到的是 source.audit_ids 原样（注入契约）。"""
    proposal = _make_proposal()
    received: list[list[str]] = []

    def capture(ids: list[str]) -> bool:
        received.append(ids)
        return True

    assert validate_audit_ids(proposal, audit_lookup=capture) is True
    assert received == [["a-1"]]


def test_validate_audit_ids_empty_rejected() -> None:
    """反向：audit_ids 为空 -> False（即使 lookup 会放行）。"""
    proposal = _make_proposal(
        source={
            "chain_id": "chat-inbox",
            "engine_task_id": "e-000001",
            "worker_id": "crm.translate",
            "audit_ids": [],
        }
    )
    assert validate_audit_ids(proposal, audit_lookup=lambda ids: True) is False


def test_validate_audit_ids_lookup_false_rejected() -> None:
    """反向：audit_ids 非空但审计表查不到 -> False。"""
    proposal = _make_proposal()
    assert validate_audit_ids(proposal, audit_lookup=lambda ids: False) is False


# ==== §6.2 ContextProvider Protocol（实现侧）====


class FakeContextProvider:
    """tests/ 内桩：async def provide 实现类，验证满足 ContextProvider 协议。"""

    async def provide(self, params: BaseModel) -> BaseModel:
        return params


class FakeGreetingParams(BaseModel):
    customer_id: str


def test_context_provider_protocol_satisfied_by_async_provide() -> None:
    """实现类（async def provide）可被 isinstance 判定满足协议。"""
    provider = FakeContextProvider()
    assert isinstance(provider, ContextProvider)


def test_context_provider_provide_is_async_and_awaitable() -> None:
    """provide 必须是协程函数（§6.2：业务侧提供 async def provide）。"""
    assert inspect.iscoroutinefunction(FakeContextProvider.provide)


@pytest.mark.asyncio
async def test_context_provider_provide_returns_model() -> None:
    """调用 provide 返回 BaseModel（只读供给，无副作用由实现方保证）。"""
    provider = FakeContextProvider()
    params = FakeGreetingParams(customer_id="cu-9")
    result = await provider.provide(params)
    assert isinstance(result, BaseModel)
    assert result is params


def test_context_provider_protocol_rejects_missing_provide() -> None:
    """反向：没有 provide 方法的类不满足协议。"""

    class NotAProvider:
        def other(self) -> None:
            return None

    assert not isinstance(NotAProvider(), ContextProvider)
