"""v0.2 T4 demo_propose 工序测试（详设-v0.2 §7 演示链 tm_demo_chain）。

demo_propose 是纯代码工序（worker.yaml 的 reason: none，无 LLM 调用）：

- 模型层：DemoProposeInput 的 pydantic 往返（text/ref_id/kind，kind 契约枚举）
- run()：直接调真模块函数（P3-1 契约 import 绑定），桩 EngineContext 驱动
  （不真调 LLM——本工序本来就不调）：
  - 输出是合法 TaskProposal（Pydantic 校验）：domain/action_id/默认角色/
    文案模板/evidence/source 逐字段断言
  - evidence 引用链输入的对象 id（决策 16 数据引用封闭性）：
    ref_id/quote/kind 全部来自输入
  - 无依据输入被拒：空 ref_id / 空文本 -> ValueError（详设 v0.1 §6.4
    无依据不出建议，空 evidence 走不通）
- 真声明全链路（嵌入式 PG + FakeAgent + 真注册表 + TaskRunner）：
  tm_demo_chain DONE，全链只调 1 次 LLM（demo_echo；demo_propose 跳过
  REASON 零 token），末步输出落库为合法 TaskProposal，source 追溯完整

基建复用（from test_runner import ...，fixture 随模块收集）：
db_engine（async fixture，嵌入式 PG）+ _clean_engine_tables（autouse 清库）+
FakeAgent / make_agent_factory（R12 构造注入桩）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
样本无 URL/IP/sk- 字面量、不给敏感名赋字面量、不读 os.environ
（环境变量只经 monkeypatch.setenv 写入）。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel
from pytest_asyncio import fixture as async_fixture

from engine.core.context import EngineContext
from engine.core.db import get_audit, get_step, get_task
from engine.core.llm import load_models
from engine.core.runner import TaskRunner
from engine.registry import load_registry
from models.contract.task import TaskProposal
from models.workers import DemoProposeInput
from test_runner import (
    FakeAgent,
    _clean_engine_tables,  # noqa: F401  # autouse 清库（跨模块 import，随模块收集）
    db_engine,
    make_agent_factory,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKER_REL = "engine/registry/workers/demo/demo_propose"


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """models.yaml 的 env: 引用所需环境变量（R20：值只进测试环境变量）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


def _load_worker_config() -> dict:
    """读 demo_propose 真 config/（与 runner _load_config_dir 同口径：顶层合并）。"""
    cfg: dict = {}
    base = REPO_ROOT / _WORKER_REL / "config"
    for path in sorted(base.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update(data)
    return cfg


def _make_ctx(
    *,
    worker_id: str,
    domain: str,
    inputs: BaseModel,
    config: dict | None = None,
    llm_output: object = None,
    task_id: int | None = None,
    chain_id: str | None = None,
) -> EngineContext:
    """最小 EngineContext（T12a 契约字段齐备；v0.2 T4 增 chain_id）。"""
    return EngineContext(
        worker_id=worker_id,
        domain=domain,
        inputs=inputs,
        config=config if config is not None else {},
        context_data={},
        llm_output=llm_output,
        task_id=task_id,
        chain_id=chain_id,
    )


def _run(inputs: DemoProposeInput, **ctx_kw: object) -> TaskProposal:
    """import 真 run 模块并以桩 ctx 调用（P3-1 契约）。"""
    run_mod = importlib.import_module(
        "engine.registry.workers.demo.demo_propose.run"
    )
    ctx = _make_ctx(
        worker_id="demo_propose", domain="demo", inputs=inputs, **ctx_kw
    )
    result = run_mod.run(inputs, ctx)
    assert isinstance(result, TaskProposal)
    return result


# ==== 模型层：pydantic 往返 ====


def test_demo_propose_input_roundtrip() -> None:
    """DemoProposeInput：构造 -> model_dump -> 重新校验，往返一致（R2 输出即类型）。"""
    cases: list[BaseModel] = [
        DemoProposeInput(text="hi", ref_id="m-1", kind="message"),
        DemoProposeInput(text="hi", ref_id="o-1", kind="order_view"),
        DemoProposeInput(text="hi", ref_id="l-1", kind="listing"),
        DemoProposeInput(text="hi", ref_id="i-1", kind="image"),
        DemoProposeInput(text="hi", ref_id="x-1", kind="metric"),
    ]
    for model in cases:
        cls = type(model)
        dumped = model.model_dump(mode="json")
        assert cls.model_validate(dumped) == model


def test_demo_propose_input_rejects_bad_kind() -> None:
    """kind 必须是契约枚举（message/metric/order_view/listing/image），其余拒。"""
    with pytest.raises(Exception):
        DemoProposeInput(text="hi", ref_id="m-1", kind="chat")


# ==== run()：桩 ctx 直接调真模块函数（无 LLM，纯代码）====


def test_run_produces_valid_task_proposal() -> None:
    """run()：输出是合法 TaskProposal，字段全部对齐契约与 config（R10 规格外置）。"""
    cfg = _load_worker_config()
    inputs = DemoProposeInput(text="买家说缺货", ref_id="m-001", kind="message")
    proposal = _run(
        inputs,
        config=cfg,
        task_id=7,
        chain_id="tm_demo_chain",
    )
    assert isinstance(proposal, TaskProposal)  # 已过 Pydantic 类型校验
    assert proposal.domain == "demo"
    assert proposal.action_id == "tm.proposal"  # 关联 tm.proposal Action 登记
    assert proposal.suggested_role == cfg["default_role"]  # 默认角色按工序声明（config）
    assert proposal.suggested_due_days == cfg["default_due_days"]
    assert proposal.title == cfg["title_template"].format(text="买家说缺货")
    assert proposal.detail == cfg["detail_template"].format(
        text="买家说缺货", ref_id="m-001"
    )
    # source 追溯（详设-v0.2 §3.4）：chain_id/engine_task_id/worker_id 全填
    assert proposal.source.chain_id == "tm_demo_chain"
    assert proposal.source.worker_id == "demo_propose"
    assert proposal.source.engine_task_id == "e-000007"
    assert proposal.source.audit_ids == []  # 纯代码工序无 LLM 调用


def test_run_evidence_references_chain_input() -> None:
    """evidence 引用链喂给的数据（决策 16 数据引用封闭性）：ref_id/quote/kind 全来自输入。"""
    cfg = _load_worker_config()
    inputs = DemoProposeInput(text="买家问运费", ref_id="m-042", kind="message")
    proposal = _run(inputs, config=cfg, chain_id="tm_demo_chain")
    assert len(proposal.evidence) == 1
    ev = proposal.evidence[0]
    assert ev.ref_id == inputs.ref_id  # 链输入的业务对象 id
    assert ev.quote == inputs.text  # 原文摘录 = 链输入文本
    assert ev.kind == inputs.kind


def test_run_title_truncated_to_contract_limit() -> None:
    """超长文本：title 截断到契约上限 80（title Field max_length=80），输出仍合法。"""
    cfg = _load_worker_config()
    long_text = "长" * 200
    proposal = _run(
        DemoProposeInput(text=long_text, ref_id="m-001", kind="message"),
        config=cfg,
        chain_id="tm_demo_chain",
    )
    assert len(proposal.title) <= 80
    assert proposal.evidence[0].quote == long_text  # 详情/quote 不截断（detail 无上限）


def test_run_rejects_no_basis_input() -> None:
    """无依据输入被拒（空 evidence 走不通）：空/空白 ref_id 或文本 -> ValueError。"""
    cfg = _load_worker_config()
    bad_inputs: list[DemoProposeInput] = [
        DemoProposeInput(text="", ref_id="m-001", kind="message"),  # 空文本
        DemoProposeInput(text="   ", ref_id="m-001", kind="message"),  # 纯空白文本
        DemoProposeInput(text="hi", ref_id="", kind="message"),  # 空 ref_id
        DemoProposeInput(text="hi", ref_id="  ", kind="message"),  # 纯空白 ref_id
    ]
    for inputs in bad_inputs:
        ctx = _make_ctx(
            worker_id="demo_propose",
            domain="demo",
            inputs=inputs,
            config=cfg,
            chain_id="tm_demo_chain",
        )
        with pytest.raises(ValueError):
            importlib.import_module(
                "engine.registry.workers.demo.demo_propose.run"
            ).run(inputs, ctx)


# ==== 真声明全链路：tm_demo_chain（嵌入式 PG + FakeAgent + TaskRunner）====


@pytest.mark.asyncio
async def test_tm_demo_chain_done(
    monkeypatch: pytest.MonkeyPatch, db_engine
) -> None:
    """真 worker 全链路（tm_demo_chain）：DONE + 全链只调 1 次 LLM（demo_echo，
    demo_propose reason: none 跳过 REASON）+ 末步输出落库为合法 TaskProposal。"""
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
        {"text": "买家说缺货", "ref_id": "m-001", "kind": "message"},
    )
    assert result.status == "done"
    assert result.error is None
    # demo_echo 调 1 次 LLM；demo_propose 为纯代码工序，REASON 跳过 —— 全链 1 次
    assert agents[0].calls == 1
    assert any(
        line.phase == "REASON" and "skipped" in line.message
        for line in result.phase_lines
    )

    task = await get_task(db_engine, result.task_id)
    assert task is not None and task.status == "done"
    row = await get_step(db_engine, task.current_step_row)
    assert row is not None and row.status == "done"
    # 末步（demo_propose）输出落库 = TaskProposal，过契约校验
    proposal = TaskProposal.model_validate(row.output)
    assert proposal.domain == "demo"
    assert proposal.action_id == "tm.proposal"
    assert proposal.evidence[0].ref_id == "m-001"
    # 文本来自 demo_echo 输出（echo 入参原样回显，不消费 LLM 结果）
    assert proposal.evidence[0].quote == "买家说缺货"
    assert proposal.source.chain_id == "tm_demo_chain"
    assert proposal.source.engine_task_id == f"e-{result.task_id:06d}"
    assert proposal.source.worker_id == "demo_propose"
    # 审计只 1 条（demo_echo 的 LLM 调用；demo_propose 零审计）
    audit = await get_audit(db_engine, result.task_id)
    assert len(audit) == 1 and audit[0].worker_id == "demo_echo"
