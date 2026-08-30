"""v0.3 T5 TM 转交器（engine/actions/tm_proposal.py）测试（接口化改造后）。

v0.2 直连落库 -> v0.3 HTTP 写接口（决策 26）：消费者校验（禁幻觉三件套 +
risk 标注）后经 BizApiClient POST /api/biz/tm/proposals；幂等/防重上移 web
接口层（测试用 MockTransport 桩模拟接口响应）。

覆盖：
- 消费者注册表：CONSUMERS 含 tm.proposal -> 转交器
- 三件套反向各一条（消费者层，失败不调 HTTP）：空 evidence 拒落；audit_ids
  不可查拒落；ref_id 白名单外拒落（记「幻觉证据: ref_id=...」）
- 技术决策：whitelist 缺注入 fail-closed
- 接口响应映射：200 {ok,id} -> inserted；200 {ok,skipped:true} ->
  skipped_idempotent；422 -> rejected；网络异常（handler 抛）-> rejected
- risk 代码规则标注：请求体带 risk（Action 声明 suggest）
- 端到端：真链 tm_demo_chain DONE -> 转交器 + mock 接口 -> inserted

基建复用：db_engine（引擎库）+ FakeAgent 桩；BizApiClient 注入
transport=MockTransport（零网络零真服务）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pytest_asyncio import fixture as async_fixture

from engine.actions import CONSUMERS, consume_task_proposal
from engine.actions.biz_client import BizApiClient
from engine.actions.tm_proposal import _REASON_HALLUCINATED
from engine.core.llm import load_models
from engine.core.runner import TaskRunner
from engine.registry import load_registry
from models.contract.task import TaskProposal
from test_runner import (  # noqa: F401  # 跨模块 fixture 随模块收集（autouse 清引擎库）
    _clean_engine_tables,
    db_engine,
    make_agent_factory,
)
from test_llm import FakeAgent

REPO_ROOT = Path(__file__).resolve().parents[1]

_DEFAULT_WHITELIST = {"msg-001"}
_DEFAULT_LOOKUP = lambda ids: True  # noqa: E731  # 桩 lookup：非空即放行


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


def _biz_client(handler) -> BizApiClient:
    """MockTransport 桩写接口客户端（零网络，R12）。"""
    return BizApiClient(
        base_url="http" + "://test-" + "biz", token="test-token",
        transport=httpx.MockTransport(handler),
    )


def _proposal(**overrides) -> TaskProposal:
    base = dict(
        title="跟进买家 Mia",
        detail="买家询问定制花束",
        domain="crm",
        action_id="tm.proposal",
        suggested_role="运营",
        suggested_due_days=1,
        evidence=[{"kind": "message", "ref_id": "msg-001", "quote": "原文"}],
        source={
            "chain_id": "tm_demo_chain",
            "engine_task_id": "e-000001",
            "worker_id": "demo_propose",
            "audit_ids": [],
        },
    )
    base.update(overrides)
    return TaskProposal.model_validate(base)


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    """真注册表（models.yaml env: 引用需测试环境变量，R20 值只进测试环境）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")
    return load_registry(REPO_ROOT)


# ---- 消费者注册表 ----


def test_consumers_registry_has_tm_proposal() -> None:
    assert "tm.proposal" in CONSUMERS
    assert CONSUMERS["tm.proposal"] is consume_task_proposal


# ---- 禁幻觉三件套反向（消费者层，不触发 HTTP）----


@pytest.mark.asyncio
async def test_empty_evidence_rejected(registry) -> None:
    outcome = await consume_task_proposal(
        _proposal(evidence=[]),
        registry=registry,
        audit_lookup=_DEFAULT_LOOKUP,
        whitelist=_DEFAULT_WHITELIST,
        biz_client=_biz_client(lambda req: httpx.Response(200, json={"ok": True, "id": 1})),
    )
    assert outcome.status == "rejected"
    assert "evidence 为空" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_audit_unverifiable_rejected(registry) -> None:
    outcome = await consume_task_proposal(
        _proposal(
            source={
                "chain_id": "crm_chat_chain",
                "engine_task_id": "e-000002",
                "worker_id": "todo_generate",
                "audit_ids": ["1"],
            }
        ),
        registry=registry,
        audit_lookup=lambda ids: False,
        whitelist=_DEFAULT_WHITELIST,
        biz_client=_biz_client(lambda req: httpx.Response(200, json={"ok": True, "id": 1})),
    )
    assert outcome.status == "rejected"
    assert "audit_ids 不可查" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_whitelist_outside_rejected(registry) -> None:
    outcome = await consume_task_proposal(
        _proposal(evidence=[{"kind": "message", "ref_id": "msg-999", "quote": "幻觉"}]),
        registry=registry,
        audit_lookup=_DEFAULT_LOOKUP,
        whitelist=_DEFAULT_WHITELIST,
        biz_client=_biz_client(lambda req: httpx.Response(200, json={"ok": True, "id": 1})),
    )
    assert outcome.status == "rejected"
    assert _REASON_HALLUCINATED in (outcome.reason or "")


@pytest.mark.asyncio
async def test_missing_injections_fail_closed(registry) -> None:
    """whitelist 缺注入 fail-closed（数据引用封闭性无法校验，拒落）。"""
    outcome = await consume_task_proposal(
        _proposal(),
        registry=registry,
        audit_lookup=_DEFAULT_LOOKUP,
        whitelist=None,
        biz_client=_biz_client(lambda req: httpx.Response(200, json={"ok": True, "id": 1})),
    )
    assert outcome.status == "rejected"
    assert "whitelist 未注入" in (outcome.reason or "")


# ---- 接口响应映射 ----


@pytest.mark.asyncio
async def test_inserted_on_ok(registry) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "id": 7, "skipped": False})

    outcome = await consume_task_proposal(
        _proposal(),
        registry=registry,
        audit_lookup=_DEFAULT_LOOKUP,
        whitelist=_DEFAULT_WHITELIST,
        biz_client=_biz_client(handler),
    )
    assert outcome.status == "inserted"
    assert outcome.proposal_id == 7
    assert captured["body"]["risk"] == "suggest"  # risk 代码规则标注（Action 声明）
    assert captured["body"]["action_id"] == "tm.proposal"


@pytest.mark.asyncio
async def test_skipped_on_idempotent(registry) -> None:
    outcome = await consume_task_proposal(
        _proposal(),
        registry=registry,
        audit_lookup=_DEFAULT_LOOKUP,
        whitelist=_DEFAULT_WHITELIST,
        biz_client=_biz_client(
            lambda req: httpx.Response(200, json={"ok": True, "id": 3, "skipped": True})
        ),
    )
    assert outcome.status == "skipped_idempotent"


@pytest.mark.asyncio
async def test_rejected_on_422(registry) -> None:
    outcome = await consume_task_proposal(
        _proposal(),
        registry=registry,
        audit_lookup=_DEFAULT_LOOKUP,
        whitelist=_DEFAULT_WHITELIST,
        biz_client=_biz_client(
            lambda req: httpx.Response(422, json={"detail": "evidence 为空"})
        ),
    )
    assert outcome.status == "rejected"
    assert "422" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_rejected_on_network_error(registry) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    outcome = await consume_task_proposal(
        _proposal(),
        registry=registry,
        audit_lookup=_DEFAULT_LOOKUP,
        whitelist=_DEFAULT_WHITELIST,
        biz_client=_biz_client(handler),
    )
    assert outcome.status == "rejected"
    assert "写接口" in (outcome.reason or "")


# ---- 端到端：真链 DONE -> 转交器 + mock 接口 ----


@pytest.mark.asyncio
async def test_e2e_chain_done_then_consume_lands(
    monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    """真链 tm_demo_chain（demo_echo -> demo_propose 纯代码）DONE 后，末步
    TaskProposal 经转交器 + mock 写接口 -> inserted。"""
    _set_env(monkeypatch)
    registry = load_registry(REPO_ROOT)
    model_registry = load_models(REPO_ROOT / "models.yaml")
    factory, agents = make_agent_factory(FakeAgent, output=lambda ot: ot(text="echoed"))
    runner = TaskRunner(
        db_engine,
        registry,
        agent_factory=factory,
        model_registry=model_registry,
        repo_root=REPO_ROOT,
        writable_check=lambda: True,
        backoff=0.0,
    )
    result = await runner.run(
        "tm_demo_chain",
        {"text": "跟进买家", "ref_id": "msg-001", "kind": "message"},
    )
    assert result.status == "done"
    from engine.core.db import get_last_step
    from engine.server import _extract_proposal

    step = await get_last_step(db_engine, result.task_id)
    proposal = _extract_proposal(step.output if step is not None else None)
    assert proposal is not None

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "id": 42, "skipped": False})

    outcome = await consume_task_proposal(
        proposal,
        registry=registry,
        audit_lookup=lambda ids: True,
        whitelist={"msg-001"},
        biz_client=_biz_client(handler),
    )
    assert outcome.status == "inserted"
    assert outcome.proposal_id == 42
    assert captured["body"]["evidence"][0]["ref_id"] == "msg-001"
