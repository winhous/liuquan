"""v0.3 T5 crm_candidate 候选消费者测试（决策 19/26：todo_generate 候选落库）。

- 契约归一（dict -> TodoCandidateResult）
- 禁幻觉三件套反向：空 evidence / 白名单外 ref_id / audit 不可查 / whitelist
  缺注入 fail-closed
- 接口响应映射：200 ok -> inserted；skipped -> skipped_idempotent；422/网络
  异常 -> rejected
- 空候选数组 = 合法产出（规则：确实无可生成）
- source 注入（customer_id/worker_id/audit_ids 由调用方提供）

BizApiClient 注入 MockTransport 桩（零网络）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pytest_asyncio import fixture as async_fixture

from engine.actions.crm_candidate import consume_todo_candidates
from engine.actions.biz_client import BizApiClient
from engine.actions.tm_proposal import _REASON_HALLUCINATED
from engine.registry import load_registry

REPO_ROOT = Path(__file__).resolve().parents[1]

_WHITELIST = {"1", "2", "3"}

_SOURCE = {
    "customer_id": 1,
    "chain_id": "crm_chat_chain",
    "engine_task_id": "e-000005",
    "worker_id": "todo_generate",
    "audit_ids": ["1"],
}


def _deliverable(**overrides) -> dict:
    base = {
        "todos": [
            {
                "content": "确认花材组合及婚礼日期",
                "reason": "卖家答应确认",
                "suggested_tags": ["报价"],
                "evidence": [{"kind": "message", "ref_id": "1", "quote": "hi"}],
                "suggested_next": None,
            }
        ]
    }
    base.update(overrides)
    return base


def _biz_client(handler) -> BizApiClient:
    return BizApiClient(
        base_url="http" + "://test-" + "biz", token="test-token",
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")
    return load_registry(REPO_ROOT)


@pytest.mark.asyncio
async def test_ok_inserted(registry) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "id": 5, "skipped": False})

    outcome = await consume_todo_candidates(
        _deliverable(),
        registry=registry,
        audit_lookup=lambda ids: True,
        whitelist=_WHITELIST,
        biz_client=_biz_client(handler),
        source=_SOURCE,
    )
    assert outcome.status == "inserted"
    assert captured["body"]["customer_id"] == 1
    assert captured["body"]["content"] == "确认花材组合及婚礼日期"
    assert captured["body"]["source"]["engine_task_id"] == "e-000005"


@pytest.mark.asyncio
async def test_empty_evidence_rejected(registry) -> None:
    outcome = await consume_todo_candidates(
        _deliverable(todos=[{"content": "x", "reason": "", "evidence": []}]),
        registry=registry,
        audit_lookup=lambda ids: True,
        whitelist=_WHITELIST,
        biz_client=_biz_client(lambda req: httpx.Response(200, json={"ok": True})),
        source=_SOURCE,
    )
    assert outcome.status == "rejected"
    assert "evidence 为空" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_whitelist_outside_rejected(registry) -> None:
    outcome = await consume_todo_candidates(
        _deliverable(
            todos=[
                {
                    "content": "x",
                    "reason": "",
                    "evidence": [{"kind": "message", "ref_id": "999", "quote": "幻觉"}],
                }
            ]
        ),
        registry=registry,
        audit_lookup=lambda ids: True,
        whitelist=_WHITELIST,
        biz_client=_biz_client(lambda req: httpx.Response(200, json={"ok": True})),
        source=_SOURCE,
    )
    assert outcome.status == "rejected"
    assert _REASON_HALLUCINATED in (outcome.reason or "")


@pytest.mark.asyncio
async def test_audit_empty_for_llm_worker_rejected(registry) -> None:
    """todo_generate 为 reason=llm 工序，空 audit_ids 拒落（无审计追溯）。"""
    outcome = await consume_todo_candidates(
        _deliverable(),
        registry=registry,
        audit_lookup=lambda ids: True,
        whitelist=_WHITELIST,
        biz_client=_biz_client(lambda req: httpx.Response(200, json={"ok": True})),
        source={**_SOURCE, "audit_ids": []},
    )
    assert outcome.status == "rejected"
    assert "audit_ids 为空" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_audit_unverifiable_rejected(registry) -> None:
    outcome = await consume_todo_candidates(
        _deliverable(),
        registry=registry,
        audit_lookup=lambda ids: False,
        whitelist=_WHITELIST,
        biz_client=_biz_client(lambda req: httpx.Response(200, json={"ok": True})),
        source={**_SOURCE, "audit_ids": ["1"]},
    )
    assert outcome.status == "rejected"
    assert "audit_ids 不可查" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_whitelist_missing_fail_closed(registry) -> None:
    outcome = await consume_todo_candidates(
        _deliverable(),
        registry=registry,
        audit_lookup=lambda ids: True,
        whitelist=None,
        biz_client=_biz_client(lambda req: httpx.Response(200, json={"ok": True})),
        source=_SOURCE,
    )
    assert outcome.status == "rejected"
    assert "whitelist 未注入" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_skipped_on_idempotent(registry) -> None:
    outcome = await consume_todo_candidates(
        _deliverable(),
        registry=registry,
        audit_lookup=lambda ids: True,
        whitelist=_WHITELIST,
        biz_client=_biz_client(
            lambda req: httpx.Response(200, json={"ok": True, "id": 3, "skipped": True})
        ),
        source=_SOURCE,
    )
    assert outcome.status == "skipped_idempotent"


@pytest.mark.asyncio
async def test_rejected_on_422(registry) -> None:
    outcome = await consume_todo_candidates(
        _deliverable(),
        registry=registry,
        audit_lookup=lambda ids: True,
        whitelist=_WHITELIST,
        biz_client=_biz_client(lambda req: httpx.Response(422, json={"detail": "content 超长"})),
        source=_SOURCE,
    )
    assert outcome.status == "rejected"
    assert "422" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_rejected_on_network_error(registry) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    outcome = await consume_todo_candidates(
        _deliverable(),
        registry=registry,
        audit_lookup=lambda ids: True,
        whitelist=_WHITELIST,
        biz_client=_biz_client(handler),
        source=_SOURCE,
    )
    assert outcome.status == "rejected"
    assert "写接口" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_empty_todos_is_valid(registry) -> None:
    """空候选数组 = 合法产出（规则：确实无可生成，不拒）。"""
    outcome = await consume_todo_candidates(
        {"todos": []},
        registry=registry,
        audit_lookup=lambda ids: True,
        whitelist=_WHITELIST,
        biz_client=_biz_client(lambda req: httpx.Response(200, json={"ok": True})),
        source=_SOURCE,
    )
    assert outcome.status == "inserted"
