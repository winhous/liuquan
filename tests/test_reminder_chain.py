"""crm_reminder_chain 端到端测试（v0.4 详设 §10.1/§10.2/§10.3；决策 37 纯代码提醒）。

链 crm_reminder_chain 有一工序：crm_follow_up_reminder（纯代码，reason: none，零 token）。
测试覆盖：
1. worker 从超期客户列表生成 ReminderItem（title/detail/evidence 正确）
2. 空 provider 数据 -> 空 reminders（合法）
3. title 模板格式验证
4. Evidence 白名单：ref_id = str(customer_id)（来自 provider）
5. 链端到端：trigger -> TaskRunner -> DONE + 阶段流水

注意：EvidenceRef.kind 详设 §10.1 标注 type:'customer'，但 EvidenceRef.kind 枚举
为 message/metric/order_view/listing/image；worker 使用 'metric' 作为占位（待
详设补丁将 'customer' 加入 EvidenceRef.kind 枚举后改回）。
"""

from __future__ import annotations

import pytest
from engine.core.context import EngineContext
from engine.registry.workers.crm.crm_follow_up_reminder import run as reminder_run
from models.workers import (
    ReminderChainInput,
    ReminderContextData,
    ReminderCustomer,
    ReminderItem,
    ReminderResult,
)


# ---- 辅助 ----


def _make_overdue_customers() -> list[dict]:
    """构造超期客户 dict 列表（模拟 provider 返回序列化后的数据）。"""
    return [
        {
            "customer_id": 1,
            "nickname": "Alice",
            "days_since": 10,
            "latest_summary": "买家想定制花束",
        },
        {
            "customer_id": 2,
            "nickname": "Bob",
            "days_since": 25,
            "latest_summary": "询问物流时效",
        },
    ]


def _make_ctx(
    customers: list[dict] | None = None,
    config: dict | None = None,
) -> EngineContext:
    """构造工序运行上下文（fake provider 数据注入 context_data）。"""
    if customers is None:
        customers = _make_overdue_customers()
    return EngineContext(
        worker_id="crm_follow_up_reminder",
        domain="crm",
        inputs=ReminderChainInput(trigger_date="2026-09-01"),
        config=config or {"max_detail_length": 200},
        context_data={
            "crm_overdue_context": {
                "customers": customers,
            }
        },
    )


# ==== 1. 正常超期客户 -> 生成提醒项 ====


def test_reminder_worker_overdue_customers() -> None:
    """worker 从超期客户 dict 列表生成 ReminderItem（title/detail/evidence）。"""
    ctx = _make_ctx()
    inputs = ReminderChainInput(trigger_date="2026-09-01")
    result = reminder_run.run(inputs, ctx)

    assert isinstance(result, ReminderResult)
    assert len(result.reminders) == 2

    # 第一个客户
    r1 = result.reminders[0]
    assert r1.customer_id == 1
    assert r1.title == "跟进客户：Alice（已 10 天未跟进）"
    assert "买家想定制花束" in r1.detail
    assert "Alice" in r1.detail
    assert r1.days_since == 10
    assert len(r1.evidence) == 1
    assert r1.evidence[0].ref_id == "1"
    assert r1.evidence[0].quote == "Alice"

    # 第二个客户
    r2 = result.reminders[1]
    assert r2.customer_id == 2
    assert r2.title == "跟进客户：Bob（已 25 天未跟进）"
    assert "询问物流时效" in r2.detail
    assert r2.days_since == 25
    assert r2.evidence[0].ref_id == "2"


def test_reminder_worker_overdue_model_injection() -> None:
    """provider 直接注入 ReminderContextData Model（非 dict）也能正确遍历。"""
    ctx = EngineContext(
        worker_id="crm_follow_up_reminder",
        domain="crm",
        inputs=ReminderChainInput(trigger_date="2026-09-01"),
        config={"max_detail_length": 200},
        context_data={
            "crm_overdue_context": ReminderContextData(
                customers=[
                    ReminderCustomer(
                        customer_id=10,
                        nickname="ModelUser",
                        days_since=15,
                        latest_summary="Model 注入测试",
                    )
                ]
            )
        },
    )
    inputs = ReminderChainInput(trigger_date="2026-09-01")
    result = reminder_run.run(inputs, ctx)

    assert len(result.reminders) == 1
    assert result.reminders[0].customer_id == 10
    assert "ModelUser" in result.reminders[0].title


# ==== 2. 空 provider 数据 -> 空 reminders ====


def test_reminder_worker_empty_list() -> None:
    """无超期客户 -> reminders 空数组（合法）。"""
    ctx = _make_ctx(customers=[])
    inputs = ReminderChainInput(trigger_date="2026-09-01")
    result = reminder_run.run(inputs, ctx)
    assert isinstance(result, ReminderResult)
    assert result.reminders == []


def test_reminder_worker_no_provider_data() -> None:
    """provider 未注入（context_data 无 crm_overdue_context）-> 空数组。"""
    ctx = EngineContext(
        worker_id="crm_follow_up_reminder",
        domain="crm",
        inputs=ReminderChainInput(trigger_date="2026-09-01"),
        config={"max_detail_length": 200},
        context_data={},
    )
    inputs = ReminderChainInput(trigger_date="2026-09-01")
    result = reminder_run.run(inputs, ctx)
    assert result.reminders == []


# ==== 3. title 模板格式验证 ====


def test_reminder_worker_title_format() -> None:
    """title 模板格式严格为「跟进客户：{nickname}（已 {days_since} 天未跟进）」。"""
    ctx = _make_ctx(customers=[
        {"customer_id": 42, "nickname": "测试用户", "days_since": 7, "latest_summary": "无摘要"},
    ])
    inputs = ReminderChainInput(trigger_date="2026-09-01")
    result = reminder_run.run(inputs, ctx)
    assert result.reminders[0].title == "跟进客户：测试用户（已 7 天未跟进）"


def test_reminder_worker_detail_truncation() -> None:
    """detail 按 config max_detail_length 截断 + 追加客户标识。"""
    long_summary = "A" * 300  # 超过 max_detail_length=50
    ctx = _make_ctx(
        customers=[{"customer_id": 1, "nickname": "Mia", "days_since": 5, "latest_summary": long_summary}],
        config={"max_detail_length": 50},
    )
    inputs = ReminderChainInput(trigger_date="2026-09-01")
    result = reminder_run.run(inputs, ctx)
    detail = result.reminders[0].detail
    # 截断后 detail 长度 = 50 + 客户标识行
    assert "A" * 50 in detail
    assert "A" * 51 not in detail
    assert "Mia" in detail
    assert "ID: 1" in detail


def test_reminder_worker_empty_summary_detail() -> None:
    """latest_summary 为空时 detail 只包含客户标识。"""
    ctx = _make_ctx(customers=[
        {"customer_id": 99, "nickname": "Empty", "days_since": 1, "latest_summary": ""},
    ])
    inputs = ReminderChainInput(trigger_date="2026-09-01")
    result = reminder_run.run(inputs, ctx)
    assert result.reminders[0].detail == "客户：Empty（ID: 99）"


# ==== 4. Evidence 白名单验证 ====


def test_reminder_worker_evidence_whitelist() -> None:
    """evidence.ref_id = str(customer_id)（来自 provider 数据，白名单内）。

    白名单 = provider 返回的 customer_id 集合（{1, 2}）；
    每个客户的 evidence ref_id 必须命中白名单，否则消费者层拒绝。
    """
    ctx = _make_ctx()
    inputs = ReminderChainInput(trigger_date="2026-09-01")
    result = reminder_run.run(inputs, ctx)

    # 白名单 = provider 返回的 customer_id 集合
    whitelist = {"1", "2"}

    for item in result.reminders:
        # 每个客户恰好一条 evidence
        assert len(item.evidence) == 1
        ev = item.evidence[0]
        # ref_id = str(customer_id)
        assert ev.ref_id == str(item.customer_id)
        # ref_id 必须在白名单内
        assert ev.ref_id in whitelist, (
            f"evidence ref_id={ev.ref_id} 不在白名单 {whitelist} 内"
        )
        # quote = nickname
        assert ev.quote is not None


def test_reminder_worker_evidence_rejects_non_whitelist_ref() -> None:
    """反向验证：provider 返回 customer_id=7 的客户，其 evidence ref_id='7'
    只在白名单 {7} 内——模拟白名单隔离（不同 provider 数据集互不引用）。
    """
    ctx = _make_ctx(customers=[
        {"customer_id": 7, "nickname": "Solo", "days_since": 3, "latest_summary": "独立客户"},
    ])
    inputs = ReminderChainInput(trigger_date="2026-09-01")
    result = reminder_run.run(inputs, ctx)

    # 白名单只含 7
    whitelist_solo = {"7"}
    whitelist_other = {"99"}  # 其他数据集的白名单

    ev_ref = result.reminders[0].evidence[0].ref_id
    assert ev_ref in whitelist_solo, "ref_id 应在自身白名单内"
    assert ev_ref not in whitelist_other, "ref_id 不应出现在其他数据集白名单内"


# ==== 5. 链端到端测试 ====


@pytest.mark.asyncio
async def test_reminder_chain_end_to_end(engine_pg_cluster, monkeypatch: pytest.MonkeyPatch) -> None:
    """链 crm_reminder_chain 端到端：fake provider 注入 context_data -> worker 产出 -> DONE。

    使用 TaskRunner 走完整链路（INIT/REASON/skipped/ACT/OBSERVE/VERIFY/DONE），
    reason: none = 无 LLM 调用，worker 直接产出。
    """
    from pathlib import Path

    from engine.core.db import create_engine, dispose_engine
    from engine.core.llm.models_config import load_models
    from engine.core.runner import TaskRunner
    from engine.registry import load_registry
    from tests.test_runner import FakeAgent, make_agent_factory

    # models.yaml 的 env: 引用所需环境变量（R20：值只进测试环境变量）
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")

    REPO_ROOT = Path(__file__).resolve().parents[1]

    db_engine = create_engine(engine_pg_cluster.url)
    try:
        registry = load_registry(REPO_ROOT)
        model_registry = load_models(REPO_ROOT / "models.yaml")

        # 纯代码工序不需要 FakeAgent（reason: none），但 runner 需要 factory
        factory, agents = make_agent_factory(FakeAgent)

        # fake provider 注入超期数据（providers 需要可调用对象，runner 执行 await impl(params)）
        overdue_data = ReminderContextData(
            customers=[
                ReminderCustomer(
                    customer_id=1,
                    nickname="Alice",
                    days_since=10,
                    latest_summary="买家想定制花束",
                ),
                ReminderCustomer(
                    customer_id=2,
                    nickname="Bob",
                    days_since=25,
                    latest_summary="询问物流时效",
                ),
            ]
        )

        async def _fake_overdue_provider(params=None):
            return overdue_data

        providers = {
            "crm.overdue_context": _fake_overdue_provider,
        }

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

        result = await runner.run(
            "crm_reminder_chain",
            {"trigger_date": "2026-09-01"},
        )

        assert result.status == "done", f"chain failed: {result.error}"
        assert result.error is None
        # 纯代码工序：无 LLM 调用
        assert len(agents) == 0

        # phase_lines 应包含全相位（reason: none 跳过 REASON）
        phases = [line.phase for line in result.phase_lines]
        assert "INIT" in phases
        assert "DONE" in phases
        # REASON 被标记为 skipped（reason: none 纯代码工序）
        reason_lines = [l for l in result.phase_lines if l.phase == "REASON"]
        assert len(reason_lines) == 1
        assert "skipped" in reason_lines[0].message.lower()

    finally:
        await dispose_engine(db_engine)
