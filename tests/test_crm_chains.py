"""v0.3 T4（主代理接管实现）：crm/tm 三链全链路测试（嵌入式 PG + FakeAgent + 真注册表 + fake provider）。

- crm_chat_chain：chat_translate -> snapshot_update -> todo_generate，DONE + 审计 3 条 +
  步骤 output（translations/snapshot/todos）+ context_data（crm_chat_context fake 注入）
- crm_reply_chain：customer_reply_draft 单工序链，DONE + ReplyDraftResult
- tm_intent_chain：tm_intent 单工序链，DONE + IntentResult

白名单正式化（决策 16③）：provider 返回数据 id 集合 = {1,2,3}（FakeCrmChatContext），
候选 evidence ref_id 必须命中（todo_generate prompt 侧约束 + 消费者层校验属 T5）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from models.contract.task import EvidenceRef
from models.workers import (
    ChatTranscriptResult,
    CustomerSnapshotResult,
    IntentResult,
    ReplyDraftResult,
    TodoCandidateItem,
    TodoCandidateResult,
    TranslationItem,
)
from test_runner import (
    FakeAgent,
    _clean_engine_tables,  # noqa: F401  # autouse 清库（跨模块 import）
    db_engine,
    get_audit,
    get_step,
    get_task,
    make_agent_factory,
)
from engine.core.llm.models_config import load_models
from engine.registry import load_registry
from engine.core.runner import TaskRunner
from tests.fake_providers import FakeCrmChatContext, FakeTmTaskContext

REPO_ROOT = Path(__file__).resolve().parents[1]


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


def _chat_output_factory(ot):
    """按工序 output_type 分派的 FakeAgent 输出（crm_chat_chain 三工序）。"""
    name = ot.__name__
    if name == "ChatTranscriptResult":
        return ot(
            translations=[
                TranslationItem(source_text="hi", translated_text="你好", direction="buyer")
            ]
        )
    if name == "CustomerSnapshotResult":
        return ot(current_need="定制花束", summary="买家想定制")
    if name == "TodoCandidateResult":
        return ot(
            todos=[
                TodoCandidateItem(
                    content="确认花材组合及婚礼日期",
                    reason="卖家答应确认",
                    evidence=[EvidenceRef(kind="message", ref_id="1", quote="hi")],
                )
            ]
        )
    return ot()


async def _run_chain(db_engine, chain_id: str, input_: dict, output_factory, providers):
    registry = load_registry(REPO_ROOT)
    model_registry = load_models(REPO_ROOT / "models.yaml")
    factory, agents = make_agent_factory(FakeAgent, output=output_factory)
    runner = TaskRunner(
        db_engine,
        registry,
        agent_factory=factory,
        model_registry=model_registry,
        repo_root=REPO_ROOT,
        writable_check=lambda: True,
        backoff=0.0,
        providers=providers,
    )
    result = await runner.run(chain_id, input_)
    return result, agents


@pytest.mark.asyncio
async def test_crm_chat_chain_done(
    monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    """crm_chat_chain 全链路：三工序 DONE + 审计 3 条 + 步骤产出。"""
    _set_env(monkeypatch)
    providers = {"crm.chat_context": FakeCrmChatContext()}
    result, agents = await _run_chain(
        db_engine,
        "crm_chat_chain",
        {"customer_id": 1, "conversation_text": "Hi! I love your flowers"},
        _chat_output_factory,
        providers,
    )
    assert result.status == "done", f"chain failed: {result.error}"
    assert result.error is None
    phases = [line.phase for line in result.phase_lines]
    # 三工序 × 六相位流水（phase_lines 为每工序一组拼接）
    assert len(phases) == 18, f"phases={phases}"
    assert phases[0:6] == ["INIT", "REASON", "ACT", "OBSERVE", "VERIFY", "DONE"]
    assert len(agents) == 3  # 三工序各一次 LLM
    # 复核反馈 #2：chat_translate prompt 含「混合文本识别发言人」指令
    audits = await get_audit(db_engine, result.task_id)
    prompt_text = audits[0].input_full or ""
    assert "无角色标记" in prompt_text

    task = await get_task(db_engine, result.task_id)
    assert task is not None and task.status == "done"
    audits = await get_audit(db_engine, result.task_id)
    assert len(audits) == 3  # 每工序 REASON 一次审计

    steps = await _all_steps(db_engine, result.task_id)
    assert len(steps) == 3
    # step0 = chat_translate 产出译文；step1 = snapshot；step2 = todos（均落 engine_step.output）
    assert steps[0].output["translations"][0]["source_text"] == "hi"
    assert steps[1].output["summary"] == "买家想定制"
    assert steps[2].output["todos"][0]["content"] == "确认花材组合及婚礼日期"
    assert steps[2].output["todos"][0]["evidence"][0]["ref_id"] == "1"


@pytest.mark.asyncio
async def test_crm_reply_chain_done(
    monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    """crm_reply_chain：customer_reply_draft 单工序链 DONE。"""
    _set_env(monkeypatch)
    providers = {"crm.chat_context": FakeCrmChatContext()}

    def output_factory(ot):
        if ot.__name__ == "ReplyDraftResult":
            return ot(reply_en="Hi there!", reply_zh="你好！")
        return ot()

    result, agents = await _run_chain(
        db_engine,
        "crm_reply_chain",
        {"customer_id": 1, "mode": "auto", "points": "", "full_text": ""},
        output_factory,
        providers,
    )
    assert result.status == "done", f"chain failed: {result.error}"
    assert len(agents) == 1
    row = await get_step(db_engine, (await get_task(db_engine, result.task_id)).current_step_row)
    assert row.output["reply_en"] == "Hi there!"


@pytest.mark.asyncio
async def test_tm_intent_chain_done(
    monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    """tm_intent_chain：tm_intent 单工序链 DONE（任务上下文 fake provider 注入）。"""
    _set_env(monkeypatch)
    providers = {"tm.task_context": FakeTmTaskContext()}

    def output_factory(ot):
        if ot.__name__ == "IntentResult":
            return ot(action="transfer", target_domain="erp", clarity="clear", note="生成采购申请")
        return ot()

    result, agents = await _run_chain(
        db_engine,
        "tm_intent_chain",
        {"task_id": 9, "instruction": "流转到 ERP 库存"},
        output_factory,
        providers,
    )
    assert result.status == "done", f"chain failed: {result.error}"
    assert len(agents) == 1
    row = await get_step(db_engine, (await get_task(db_engine, result.task_id)).current_step_row)
    assert row.output["action"] == "transfer"
    assert row.output["clarity"] == "clear"


async def _all_steps(db_engine, task_id: int):
    """取任务全部步骤（按 step_index 排序；EngineStep 行对象）。"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    from engine.core.db import EngineStep

    async with AsyncSession(db_engine) as session:
        rows = (await session.execute(select(EngineStep).where(EngineStep.task_id == task_id))).scalars().all()
    return sorted(rows, key=lambda r: r.step_index)
