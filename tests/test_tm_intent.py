"""v0.3 T4（主代理接管实现）：tm_intent 工序 run() 直调测试（决策 28 意图理解）。

- clear：产出结构化流转指令 -> 返回
- unclear：clarity=unclear + candidates 澄清选项 -> 返回（不猜测执行）
- config 未声明动作清单 / 动作不在清单内 -> ValueError
"""

from __future__ import annotations

import importlib

import pytest
from engine.core.context import EngineContext
from models.workers import IntentInput, IntentResult


def _ctx(inputs, llm_output, config=None) -> EngineContext:
    return EngineContext(
        worker_id="tm_intent",
        domain="tm",
        inputs=inputs,
        config=config if config is not None else {"actions": {"transfer": "x", "assign": "x", "tag": "x", "note": "x", "block": "x"}},
        context_data={},
        llm_output=llm_output,
    )


def _run():
    return importlib.import_module("engine.registry.workers.tm.tm_intent.run").run


def test_intent_clear_transfer() -> None:
    out = IntentResult(action="transfer", target_domain="erp", clarity="clear", note="生成采购申请")
    inputs = IntentInput(task_id=9, instruction="流转到 ERP 库存")
    result = _run()(inputs, _ctx(inputs, out))
    assert result is out


def test_intent_clear_assign() -> None:
    out = IntentResult(action="assign", target_role="采购", clarity="clear")
    inputs = IntentInput(task_id=9, instruction="交给采购")
    result = _run()(inputs, _ctx(inputs, out))
    assert result.action == "assign"


def test_intent_unclear_with_candidates() -> None:
    out = IntentResult(
        action="transfer", clarity="unclear", candidates=["流转到 ERP 库存", "流转到 ERP 采购"]
    )
    inputs = IntentInput(task_id=9, instruction="流转到 ERP")
    result = _run()(inputs, _ctx(inputs, out))
    assert result.clarity == "unclear"
    assert result.candidates


def test_intent_action_not_in_config_rejected() -> None:
    """动作不在 config/actions.yaml 声明集合 -> ValueError（R10：动作枚举住 config）。"""
    out = IntentResult(action="transfer", clarity="clear")
    inputs = IntentInput(task_id=9, instruction="流转")
    ctx = _ctx(inputs, out, config={"actions": {"assign": "x"}})
    with pytest.raises(ValueError):
        _run()(inputs, ctx)


def test_intent_empty_actions_config_rejected() -> None:
    out = IntentResult(action="transfer", clarity="clear")
    inputs = IntentInput(task_id=9, instruction="流转")
    ctx = _ctx(inputs, out, config={})
    with pytest.raises(ValueError):
        _run()(inputs, ctx)
