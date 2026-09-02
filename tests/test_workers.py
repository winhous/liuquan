"""T10 首批工序（demo_echo + crm_translate）测试（详设-v0.1 §4/§11/§14；任务 T10）。

TDD 红→绿，全部零网络（FakeAgent 桩住 tests/，真仓库零桩 P3-4）：

- 模型层：models/workers 全部 Model 的 pydantic 往返与缺省值
- demo_echo run()：直接调真模块函数（P3-1 契约 import 绑定），确定性回显
- crm_translate run()：消费 ctx.llm_output——可用译文原样返回；
  空译文 / 类型不符 -> ValueError（宁失败不假成功，§2.1/§5.1）
- 真声明全链路：load_registry(仓库根) 过 L1-L9 + validate 0 违规
- 真 worker 全链路（嵌入式 PG engine_pg_cluster + FakeAgent + 真注册表 +
  TaskRunner 真跑）：
  - demo_echo_chain：DONE + 全相位流水 + 审计 1 条（ACT 回显入参落库）
  - crm_translate_chain：DONE + 审计 1 条（FakeAgent 输出 ChatTranslateResult，
    ACT 原样消费 ctx.llm_output 落库）
- P1 生效验证（本任务做）：真 config/ 的值出现在 run.py 里应被拦
  （R10 机器层；真仓库 run.py 零 config 值字面量由 lint 全绿反证）

基建复用（from test_runner import ...，fixture 随模块收集，同 test_acceptance）：
db_engine（async fixture，嵌入式 PG）+ _clean_engine_tables（autouse 清库）+
FakeAgent / make_agent_factory（R12 构造注入桩）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
样本无 URL/IP/sk- 字面量、不给敏感名赋字面量、不读 os.environ
（环境变量只经 monkeypatch.setenv 写入）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel
from pytest_asyncio import fixture as async_fixture

from engine.core.context import EngineContext
from engine.core.db import get_audit, get_step, get_task
from engine.core.llm import load_models
from engine.core.runner import TaskRunner
from engine.lint.p1 import P1BusinessTermsRule
from engine.registry import load_registry, validate
from models.workers import (
    ChatTranslateInput,
    ChatTranslateResult,
    DemoEchoEventPayload,
    DemoEchoInput,
    DemoEchoResult,
    DemoGreeting,
    DemoGreetingParams,
)
from test_runner import (
    FakeAgent,
    _clean_engine_tables,  # noqa: F401  # autouse 清库（跨模块 import，随模块收集）
    db_engine,
    make_agent_factory,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """models.yaml 的 env: 引用所需环境变量（R20：值只进测试环境变量）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_ctx(
    *,
    worker_id: str,
    domain: str,
    inputs: BaseModel,
    llm_output: object = None,
) -> EngineContext:
    """最小 EngineContext（T12a 契约字段齐备；测试直构不依赖 runner）。"""
    return EngineContext(
        worker_id=worker_id,
        domain=domain,
        inputs=inputs,
        config={},
        context_data={},
        llm_output=llm_output,
    )


# ==== 模型层：pydantic 往返与缺省值 ====


def test_models_pydantic_roundtrip() -> None:
    """全部 Model：构造 -> model_dump -> 重新校验，往返一致（R2 输出即类型）。"""
    cases: list[BaseModel] = [
        DemoEchoInput(text="hi"),
        DemoEchoResult(text="hi"),
        ChatTranslateInput(text="hello"),
        ChatTranslateInput(text="hello", source_lang="en", target_lang="de"),
        ChatTranslateResult(translated="你好"),
        ChatTranslateResult(translated="你好", source_lang="en", target_lang="zh"),
        DemoGreetingParams(name="刘全"),
        DemoGreeting(message="你好，刘全"),
        DemoEchoEventPayload(text="hi"),
        DemoEchoEventPayload(text="hi", created_at="2026-08-28T00:00:00Z"),
    ]
    for model in cases:
        cls = type(model)
        dumped = model.model_dump(mode="json")
        rebuilt = cls.model_validate(dumped)
        assert rebuilt == model


def test_model_defaults() -> None:
    """缺省值：ChatTranslateInput 源/目标语言；ChatTranslateResult 语言可空；payload 时间可空。"""
    inputs = ChatTranslateInput(text="hello")
    assert inputs.source_lang == "en"
    assert inputs.target_lang == "zh"

    result = ChatTranslateResult(translated="你好")
    assert result.source_lang is None
    assert result.target_lang is None

    payload = DemoEchoEventPayload(text="hi")
    assert payload.created_at is None


def test_models_export_names_match_worker_yaml() -> None:
    """Model 名与 worker.yaml / context / events 声明的引用严格一致（loader L3 会校验）。"""
    import models.workers as mw

    expected = {
        "DemoEchoInput",
        "DemoEchoResult",
        "ChatTranslateInput",
        "ChatTranslateResult",
        "DemoGreetingParams",
        "DemoGreeting",
        "DemoEchoEventPayload",
    }
    assert expected <= set(mw.__all__)
    for name in expected:
        assert hasattr(mw, name)


# ==== demo_echo run()：直接调真模块函数 ====


def test_demo_echo_run_returns_input_text() -> None:
    """echo run()：确定性回显（ACT = 回显入参；不消费 ctx.llm_output）。"""
    import importlib

    run_mod = importlib.import_module("engine.registry.workers.demo.demo_echo.run")
    inputs = DemoEchoInput(text="hi")
    ctx = _make_ctx(worker_id="demo_echo", domain="demo", inputs=inputs)
    result = run_mod.run(inputs, ctx)
    assert isinstance(result, DemoEchoResult)
    assert result.text == "hi"  # EchoResult(text=inputs.text) 原样


def test_demo_echo_run_ignores_llm_output() -> None:
    """echo run()：即使 REASON 结果存在也不消费（职责分离，§2.1）。"""
    import importlib

    run_mod = importlib.import_module("engine.registry.workers.demo.demo_echo.run")
    inputs = DemoEchoInput(text="hello")
    ctx = _make_ctx(
        worker_id="demo_echo",
        domain="demo",
        inputs=inputs,
        llm_output=DemoEchoResult(text="echoed"),
    )
    assert run_mod.run(inputs, ctx).text == "hello"  # 回显入参而非 LLM 结果


# ==== crm_translate run()：消费 ctx.llm_output ====


def test_translate_run_returns_llm_output_unchanged() -> None:
    """翻译 run()：REASON 结果可用 -> 原样返回（校验 + 交由 runner 落库）。"""
    import importlib

    run_mod = importlib.import_module("engine.registry.workers.crm.crm_translate.run")
    inputs = ChatTranslateInput(text="hello")
    llm_output = ChatTranslateResult(translated="你好")
    ctx = _make_ctx(
        worker_id="crm_translate", domain="crm", inputs=inputs, llm_output=llm_output
    )
    result = run_mod.run(inputs, ctx)
    assert result is llm_output  # 原样返回（不重算不复制）
    assert result.translated == "你好"
    assert result.source_lang is None  # 缺省字段透传


def test_translate_run_rejects_missing_or_wrong_llm_output() -> None:
    """翻译 run()：llm_output 为空 / 非 ChatTranslateResult -> ValueError（宁失败不假成功）。"""
    import importlib

    run_mod = importlib.import_module("engine.registry.workers.crm.crm_translate.run")
    inputs = ChatTranslateInput(text="hello")
    bad_outputs: list[object] = [
        None,  # REASON 未产出
        ChatTranslateResult(translated=""),  # 空译文（translated 非空判定）
        DemoEchoResult(text="x"),  # 类型不符（其他工序 Model）
        "not-a-model",  # 非 Model
        {"translated": "你好"},  # 裸 dict 不是 RegisteredModel（R2/R22）
    ]
    for bad in bad_outputs:
        ctx = _make_ctx(
            worker_id="crm_translate", domain="crm", inputs=inputs, llm_output=bad
        )
        with pytest.raises(ValueError):
            run_mod.run(inputs, ctx)


# ==== 真声明全链路：load_registry(仓库根) L1-L9 全过 ====


def test_real_registry_loads_zero_violations(monkeypatch: pytest.MonkeyPatch) -> None:
    """真仓库声明全链路：validate 0 违规 + load_registry 成功（L1-L9 全过）。"""
    _set_env(monkeypatch)
    assert validate(REPO_ROOT) == []
    registry = load_registry(REPO_ROOT)

    assert set(registry.workers) == {
        "demo_echo",
        "crm_translate",
        "crm_follow_up_reminder",
        "demo_propose",
        "chat_translate",
        "snapshot_update",
        "todo_generate",
        "customer_reply_draft",
        "tm_intent",
        "keyword_research",
        "seo_optimize",
        "listing_healthcheck",
        "image_download",
        "image_inspect",
        "product_suggestion",
        "link_record_create",  # v0.6 批 4：拆两链新工序
        "batch_image_download",  # v0.6 批 4：拆两链新工序
        "crm_image_scan",
        "crm_image_download",
        "crm_image_caption",
        "crm_image_save",
    }
    assert set(registry.chains) == {
        "demo_echo_chain",
        "crm_translate_chain",
        "tm_demo_chain",
        "crm_chat_chain",
        "crm_reply_chain",
        "crm_reminder_chain",
        "tm_intent_chain",
        "seo_keyword_chain",
        "seo_optimize_chain",
        "seo_healthcheck_chain",
        "scrape_suggest_chain",
        "scrape_download_chain",  # v0.6 批 4：拆两链新链
        "crm_image_chain",
    }
    assert set(registry.context_providers) == {
        "demo_greeting",
        "crm_chat_context",
        "crm_overdue_context",
        "crm_message_images",
        "tm_task_context",
        "seo_metric_history",
        "scrape_image_context",
        "scrape_link_context",  # v0.6 批 4
        "scrape_link_queue",  # v0.6 批 4
    }
    assert set(registry.events) == {"demo.echo_done"}
    # v0.4：tm.schedule Action；v0.5 批 3：seo.report / seo.optimize / seo.healthcheck；
    # 批 4：scrape.suggest；批 5：crm.image；v0.6 批 4：+ scrape.download_done
    assert set(registry.actions) == {
        "demo_echo_record", "tm.proposal", "crm.candidate", "tm.schedule",
        "seo.report", "seo.optimize", "seo.healthcheck",
        "scrape.suggest", "scrape.download_done", "crm.image",
    }

    # 工序声明字段（id/domain/risk/model 别名/retry/输入输出 Model 引用）
    echo = registry.workers["demo_echo"]
    assert echo.domain.value == "demo"
    assert echo.version == 1
    assert echo.risk == "read"
    assert echo.model == "default"  # models.yaml 已注册别名（L4）
    assert echo.input.model == "DemoEchoInput"
    assert echo.output.model == "DemoEchoResult"
    assert echo.retry is not None and echo.retry.max_attempts == 2
    assert echo.retry.timeout_s == 30
    assert echo.context == []  # 不声明 context（v0.1 真实运行不依赖 provider）

    translate = registry.workers["crm_translate"]
    assert translate.domain.value == "crm"
    assert translate.risk == "read"
    assert translate.input.model == "ChatTranslateInput"
    assert translate.output.model == "ChatTranslateResult"

    # v0.2 T4：demo_propose 纯代码工序（reason: none，无 LLM 调用），
    # suggest 风险产出 TaskProposal 契约（详设-v0.2 §7 演示链）
    propose = registry.workers["demo_propose"]
    assert propose.domain.value == "demo"
    assert propose.risk == "suggest"
    assert propose.reason == "none"
    assert propose.input.model == "DemoProposeInput"
    assert propose.output.model == "TaskProposal"
    assert propose.version == 1

    # 事件 trigger_chain 引用已登记链（R22：引用断裂 = 拒载）
    assert registry.events["demo.echo_done"].trigger_chain == "demo_echo_chain"
    # Action target 锁死 tm.proposal（loader L10）；v0.2 T4 增 tm.proposal Action
    assert registry.actions["demo_echo_record"].target == "tm.proposal"
    assert registry.actions["tm.proposal"].target == "tm.proposal"
    assert registry.actions["tm.proposal"].risk == "suggest"
    assert registry.actions["tm.proposal"].output_model == "TaskProposal"
    # Context provider：id 为 snake_case（L1），实现标识为点分形态（§4.3 provider 字段）
    provider = registry.context_providers["demo_greeting"]
    assert provider.provider == "demo.greeting"
    assert provider.domain.value == "demo"
    assert provider.params.model == "DemoGreetingParams"
    assert provider.returns.model == "DemoGreeting"


# ==== P1 生效验证（本任务做）：config/ 值出现在 run.py 应被拦 ====


def test_p1_blocks_real_config_value_in_run_py(tmp_path: Path) -> None:
    """真 config/ 的业务专名出现在 run.py 字符串常量 -> P1 拦（R10 机器层）。"""
    _write(
        tmp_path,
        "engine/registry/workers/demo/w1/config/style.yaml",
        "label: 回显标记\n",
    )
    _write(tmp_path, "engine/registry/workers/demo/w1/run.py", 'MSG = "回显标记"\n')
    violations = P1BusinessTermsRule().check(tmp_path)
    assert any(v.rule_id == "P1" and "回显标记" in v.message for v in violations)


def test_p1_real_repo_still_green() -> None:
    """真仓库 run.py/worker.yaml 无 config 值字面量（P1 反证：全绿）。"""
    assert P1BusinessTermsRule().check(REPO_ROOT) == []


# ==== 真 worker 全链路：嵌入式 PG + FakeAgent + 真注册表 + TaskRunner ====


async def _run_real_chain(db_engine, chain_id: str, input_: dict, output_factory) -> tuple:
    """真注册表 + 真 prompt/config + FakeAgent 桩跑 TaskRunner（零网络）。"""
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
    )
    result = await runner.run(chain_id, input_)
    return result, agents, registry


@pytest.mark.asyncio
async def test_real_demo_echo_chain_done(
    monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    """真 worker 全链路（demo_echo_chain）：全相位到 DONE + 审计 1 条 + 回显落库。"""
    _set_env(monkeypatch)
    result, agents, _ = await _run_real_chain(
        db_engine,
        "demo_echo_chain",
        {"text": "hi"},
        lambda ot: ot(text="echoed"),
    )
    assert result.status == "done"
    assert result.error is None
    # A1：全相位流水（INIT/REASON/ACT/OBSERVE/VERIFY/DONE）
    assert [line.phase for line in result.phase_lines] == [
        "INIT", "REASON", "ACT", "OBSERVE", "VERIFY", "DONE",
    ]
    assert any("policy ok" in line.message for line in result.phase_lines)
    assert agents[0].calls == 1  # 只调一次 LLM（无二次调，R4）

    task = await get_task(db_engine, result.task_id)
    assert task is not None and task.status == "done"
    assert task.finished_at is not None
    row = await get_step(db_engine, task.current_step_row)
    assert row is not None and row.status == "done"
    assert row.output == {"text": "hi"}  # ACT 回显入参落库（非 LLM 结果）
    audit = await get_audit(db_engine, result.task_id)
    assert len(audit) == 1 and audit[0].result == "ok"


@pytest.mark.asyncio
async def test_real_crm_translate_chain_done(
    monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    """真 worker 全链路（crm_translate_chain）：DONE + 审计 1 条 + 译文落库。"""
    _set_env(monkeypatch)
    result, agents, _ = await _run_real_chain(
        db_engine,
        "crm_translate_chain",
        # prompt 模板引用 {input.source_lang}/{input.target_lang}，链入参需显式给出
        # （runner 传原始 task input 给 prompt 渲染，Model 缺省值不反填，T12a 行为）
        {"text": "hello", "source_lang": "en", "target_lang": "zh"},
        lambda ot: ot(translated="你好"),
    )
    assert result.status == "done"
    assert result.error is None
    assert [line.phase for line in result.phase_lines] == [
        "INIT", "REASON", "ACT", "OBSERVE", "VERIFY", "DONE",
    ]
    assert any("policy ok" in line.message for line in result.phase_lines)
    assert agents[0].calls == 1

    task = await get_task(db_engine, result.task_id)
    assert task.status == "done"
    row = await get_step(db_engine, task.current_step_row)
    assert row.status == "done"
    # ACT 原样消费 ctx.llm_output：REASON 的译文（含缺省字段）落库
    assert row.output == {
        "translated": "你好",
        "source_lang": None,
        "target_lang": None,
    }
    audit = await get_audit(db_engine, result.task_id)
    assert len(audit) == 1 and audit[0].result == "ok"
    assert audit[0].output_full == {
        "translated": "你好",
        "source_lang": None,
        "target_lang": None,
    }
    assert audit[0].worker_id == "crm_translate"
