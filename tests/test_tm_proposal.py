"""v0.2 T3 TM 转交器（engine/actions/tm_proposal.py）测试（详设-v0.2 §3.2/§5）。

覆盖（验收 A18/A21 在转交器层的落点；禁幻觉三件套反向 + 幂等 + 防重 +
正常落库 + 纯代码工序 audit_ids 豁免 + 端到端链->转交器落库）：
- 消费者注册表：CONSUMERS 仅 tm.proposal 一条，指向转交器
- 正常落库：合法提案（evidence 非空 + ref_id 命中白名单 + demo_propose
  纯代码工序 audit_ids 空豁免）-> tm.task_proposal 落 pending，risk 由
  Action 声明代码标注（suggest），evidence/source JSONB 对齐契约
- 三件套反向各一条：空 evidence 拒落；audit_ids 不可查（lookup False）
  拒落；ref_id 白名单外拒落（error 记「幻觉证据: ref_id=...」）
- 技术决策：audit_ids 为空 = 纯代码工序产出，reason:none 豁免；工序
  reason=llm 却空 audit_ids 拒落；whitelist/audit_lookup 缺注入 fail-closed
- 幂等（A18）：同链同工序重复交付只落一条（第二次 skipped_idempotent）
- 防重（A25）：同 ref_id + action_id 已有待审提案 / 未完成任务 -> 本次
  不落库（skipped_duplicate）；不同 ref_id 照常落库
- 端到端：真声明 tm_demo_chain（FakeAgent 桩，零网络）DONE 后，末步
  TaskProposal 经转交器落 tm.task_proposal(pending)

基建复用（from test_runner import ...，fixture 随模块收集）：
db_engine（async fixture，嵌入式引擎库）+ _clean_engine_tables（autouse
清引擎库）+ FakeAgent / make_agent_factory（R12 构造注入桩）；业务库走
本模块 tm_engine（tm_pg_cluster 嵌入式业务库）+ _clean_tm_tables
（autouse 清 tm 三表）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
样本无 URL/IP/sk- 字面量、不给敏感名赋字面量、不读 os.environ
（环境变量只经 monkeypatch.setenv 写入）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engine.actions import CONSUMERS, ConsumeOutcome, consume_task_proposal
from engine.actions.tm_proposal import _REASON_HALLUCINATED
from engine.core.llm import load_models
from engine.core.runner import TaskRunner
from engine.registry import load_registry
from models.contract.task import TaskProposal
from models.tm import Task as TmTask
from models.tm import TaskProposal as TmTaskProposalRow
from test_runner import (  # noqa: F401  # 跨模块 fixture 随模块收集（autouse 清引擎库）
    _clean_engine_tables,
    db_engine,
    make_agent_factory,
)
from test_llm import FakeAgent

REPO_ROOT = Path(__file__).resolve().parents[1]

# 默认白名单（决策 16：链喂给 AI 的数据集合）与默认审计 lookup（可查）
_DEFAULT_WHITELIST = {"msg-001"}
_DEFAULT_LOOKUP = lambda ids: True  # noqa: E731  # 桩 lookup：非空即放行（测试注入）


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """models.yaml 的 env: 引用所需环境变量（R20：值只进测试环境变量）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


# ---- fixtures（业务库嵌入式 PG：tm_pg_cluster 会话级 + 本模块 function 级）----


@async_fixture
async def tm_engine(tm_pg_cluster):
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture(autouse=True)
async def _clean_tm_tables(tm_engine):
    """每测试后 TRUNCATE tm 三表（RESTART IDENTITY 重置自增、CASCADE 破外键）。"""
    yield
    async with AsyncSession(tm_engine) as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE tm.task_event, tm.task_proposal, tm.task "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    """真注册表（demo_propose reason:none / tm.proposal Action risk=suggest）。"""
    _set_env(monkeypatch)
    return load_registry(REPO_ROOT)


# ---- 样本构造 ----

VALID_EVIDENCE = [{"kind": "message", "ref_id": "msg-001", "quote": "买家询问物流时效"}]
VALID_SOURCE = {
    "chain_id": "tm_demo_chain",
    "engine_task_id": "e-000042",
    "worker_id": "demo_propose",  # 纯代码工序（reason:none），audit_ids 空豁免
    "audit_ids": [],
}


def _proposal(**overrides: object) -> TaskProposal:
    """合法提案样本（demo_propose 纯代码产出形态；可覆盖字段构造反向用例）。"""
    data: dict[str, object] = {
        "title": "跟进买家物流时效咨询",
        "detail": "买家询问物流时效，建议运营跟进确认并回复",
        "domain": "demo",
        "action_id": "tm.proposal",
        "suggested_role": "运营",
        "suggested_due_days": 1,
        "evidence": VALID_EVIDENCE,
        "source": VALID_SOURCE,
    }
    data.update(overrides)
    return TaskProposal(**data)


async def _consume(tm_engine, proposal: TaskProposal, registry, **inject: object) -> ConsumeOutcome:
    """转交器调用（默认注入：registry/白名单/audit lookup；可覆盖）。"""
    kwargs: dict[str, object] = {
        "registry": registry,
        "whitelist": _DEFAULT_WHITELIST,
        "audit_lookup": _DEFAULT_LOOKUP,
    }
    kwargs.update(inject)
    return await consume_task_proposal(proposal, tm_engine=tm_engine, **kwargs)


async def _count(tm_engine) -> int:
    async with AsyncSession(tm_engine) as session:
        return len(
            (await session.execute(select(TmTaskProposalRow.id))).scalars().all()
        )


async def _get(tm_engine, row_id: int) -> TmTaskProposalRow:
    async with AsyncSession(tm_engine) as session:
        row = await session.get(TmTaskProposalRow, row_id)
        assert row is not None
        return row


# ==== 消费者注册表 ====


def test_consumer_registry_maps_tm_proposal() -> None:
    """注册表 v0.2 仅一条：tm.proposal -> TM 转交器（详设 §5）。"""
    assert set(CONSUMERS) == {"tm.proposal"}
    assert CONSUMERS["tm.proposal"] is consume_task_proposal


# ==== 正常落库路径 ====


@pytest.mark.asyncio
async def test_consume_inserts_pending_proposal(tm_engine, registry) -> None:
    """合法提案落 tm.task_proposal(status=pending)，字段映射 + risk 代码标注。"""
    outcome = await _consume(tm_engine, _proposal(), registry)
    assert outcome.status == "inserted"
    assert outcome.proposal_id is not None
    assert await _count(tm_engine) == 1

    row = await _get(tm_engine, outcome.proposal_id)
    assert row.status == "pending"  # 决策 12：全部人工审，落库即待审
    assert row.title == "跟进买家物流时效咨询"
    assert row.detail == "买家询问物流时效，建议运营跟进确认并回复"
    assert row.domain == "demo"
    assert row.action_id == "tm.proposal"
    assert row.risk == "suggest"  # 代码规则标注：查 Action 声明（非 AI 自评，R9）
    assert row.suggested_role == "运营"
    assert row.suggested_due_days == 1
    assert row.evidence == VALID_EVIDENCE  # EvidenceRef 数组 JSONB（对齐契约 §6.4）
    assert row.source == VALID_SOURCE  # SourceTrace JSONB（§3.4）
    assert row.reviewed_by is None and row.reviewed_at is None  # 未审
    assert row.task_id is None  # 未批准，无生成任务


@pytest.mark.asyncio
async def test_consume_accepts_dict_form_proposal(tm_engine, registry) -> None:
    """转交器输入兼容 dict 形态（链末步 output 落库即 dict；R2 归一校验）。"""
    outcome = await _consume(tm_engine, _proposal().model_dump(mode="json"), registry)
    assert outcome.status == "inserted"
    row = await _get(tm_engine, outcome.proposal_id)
    assert row.status == "pending"


# ==== 禁幻觉三件套：反向测试（A21）====


@pytest.mark.asyncio
async def test_consume_rejects_empty_evidence(tm_engine, registry) -> None:
    """反向 1：evidence 空 -> 拒落（无依据不出建议，详设 v0.1 §6.4）。"""
    outcome = await _consume(tm_engine, _proposal(evidence=[]), registry)
    assert outcome.status == "rejected"
    assert "evidence 为空" in (outcome.reason or "")
    assert await _count(tm_engine) == 0


@pytest.mark.asyncio
async def test_consume_rejects_unfindable_audit_ids(tm_engine, registry) -> None:
    """反向 2：audit_ids 非空但引擎库查不到 -> 拒落（追溯保证）。"""
    proposal = _proposal(
        source={**VALID_SOURCE, "worker_id": "demo_echo", "audit_ids": ["a-404"]}
    )
    outcome = await _consume(tm_engine, proposal, registry, audit_lookup=lambda ids: False)
    assert outcome.status == "rejected"
    assert "audit_ids 不可查" in (outcome.reason or "")
    assert await _count(tm_engine) == 0


@pytest.mark.asyncio
async def test_consume_passes_audit_ids_to_lookup(tm_engine, registry) -> None:
    """audit_lookup 收到的是 source.audit_ids 原样（注入契约，validate_audit_ids 同源）。"""
    seen: list[list[str]] = []

    def capture(ids: list[str]) -> bool:
        seen.append(ids)
        return True

    proposal = _proposal(
        source={**VALID_SOURCE, "worker_id": "demo_echo", "audit_ids": ["a-7"]}
    )
    outcome = await _consume(tm_engine, proposal, registry, audit_lookup=capture)
    assert outcome.status == "inserted"
    assert seen == [["a-7"]]


@pytest.mark.asyncio
async def test_consume_rejects_ref_id_outside_whitelist(tm_engine, registry) -> None:
    """反向 3：evidence 引用白名单外 ref_id -> 拒落 + 报告「幻觉证据: ref_id=...」（决策 16）。"""
    outcome = await _consume(
        tm_engine, _proposal(), registry, whitelist={"other-obj-001"}
    )
    assert outcome.status == "rejected"
    assert (outcome.reason or "").startswith(_REASON_HALLUCINATED + "msg-001")
    assert "msg-001" in (outcome.reason or "")
    assert await _count(tm_engine) == 0


@pytest.mark.asyncio
async def test_consume_rejects_partial_whitelist_violation(tm_engine, registry) -> None:
    """多证据仅一条越界也拒落（全量封闭，非「至少一条命中」）。"""
    proposal = _proposal(
        evidence=[
            {"kind": "message", "ref_id": "msg-001", "quote": "物流时效"},
            {"kind": "order_view", "ref_id": "ord-999", "quote": "订单"},
        ]
    )
    outcome = await _consume(
        tm_engine, proposal, registry, whitelist={"msg-001"}
    )
    assert outcome.status == "rejected"
    assert _REASON_HALLUCINATED + "ord-999" in (outcome.reason or "")
    assert await _count(tm_engine) == 0


# ==== 技术决策：audit_ids 空 = 纯代码工序产出（reason:none 豁免）====


@pytest.mark.asyncio
async def test_consume_exempts_empty_audit_ids_for_reason_none(tm_engine, registry) -> None:
    """demo_propose（reason:none）audit_ids 空 -> 豁免落库（任务书技术决策）。"""
    outcome = await _consume(tm_engine, _proposal(), registry)
    assert outcome.status == "inserted"
    assert outcome.reason is None
    assert await _count(tm_engine) == 1


@pytest.mark.asyncio
async def test_consume_rejects_empty_audit_ids_for_llm_worker(tm_engine, registry) -> None:
    """audit_ids 空但工序 reason=llm（demo_echo）-> 拒落（无审计追溯）。"""
    proposal = _proposal(source={**VALID_SOURCE, "worker_id": "demo_echo"})
    outcome = await _consume(tm_engine, proposal, registry)
    assert outcome.status == "rejected"
    assert "reason=llm" in (outcome.reason or "")
    assert await _count(tm_engine) == 0


@pytest.mark.asyncio
async def test_consume_rejects_empty_audit_ids_unknown_worker(tm_engine, registry) -> None:
    """audit_ids 空且工序未登记 -> 拒落（无法确认纯代码豁免，fail-closed）。"""
    proposal = _proposal(source={**VALID_SOURCE, "worker_id": "ghost_worker"})
    outcome = await _consume(tm_engine, proposal, registry)
    assert outcome.status == "rejected"
    assert "未登记" in (outcome.reason or "")


# ==== fail-closed：缺注入即拒落 ====


@pytest.mark.asyncio
async def test_consume_rejects_missing_whitelist(tm_engine, registry) -> None:
    """whitelist 未注入 -> 拒落（数据引用封闭性无法校验，宁失败不假成功）。"""
    outcome = await _consume(tm_engine, _proposal(), registry, whitelist=None)
    assert outcome.status == "rejected"
    assert "whitelist 未注入" in (outcome.reason or "")
    assert await _count(tm_engine) == 0


@pytest.mark.asyncio
async def test_consume_rejects_missing_audit_lookup(tm_engine, registry) -> None:
    """audit_ids 非空但 audit_lookup 未注入 -> 拒落（无法校验可查性）。"""
    proposal = _proposal(
        source={**VALID_SOURCE, "worker_id": "demo_echo", "audit_ids": ["a-1"]}
    )
    outcome = await _consume(tm_engine, proposal, registry, audit_lookup=None)
    assert outcome.status == "rejected"
    assert "audit_lookup 未注入" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_consume_rejects_unknown_action(tm_engine, registry) -> None:
    """action_id 未登记 -> 拒落（risk 必须由 Action 声明代码标注，R9）。"""
    proposal = _proposal(action_id="no.such.action")
    outcome = await _consume(tm_engine, proposal, registry)
    assert outcome.status == "rejected"
    assert "Action 声明缺失" in (outcome.reason or "")
    assert await _count(tm_engine) == 0


# ==== 幂等（A18）：同链同工序重复交付只落一条 ====


@pytest.mark.asyncio
async def test_consume_idempotent_same_delivery_once(tm_engine, registry) -> None:
    """同 source.engine_task_id + worker_id 重复交付 -> 第二次 skipped_idempotent。"""
    first = await _consume(tm_engine, _proposal(), registry)
    assert first.status == "inserted"
    second = await _consume(tm_engine, _proposal(), registry)  # 同链重跑（同 eid+wid）
    assert second.status == "skipped_idempotent"
    assert "幂等" in (second.reason or "")
    assert await _count(tm_engine) == 1  # 只落一条


@pytest.mark.asyncio
async def test_consume_same_worker_new_task_lands(tm_engine, registry) -> None:
    """同 worker 不同 engine_task_id（新链新任务）-> 正常落库（幂等键含任务 id）。"""
    first = await _consume(tm_engine, _proposal(), registry)
    assert first.status == "inserted"
    second = await _consume(
        tm_engine,
        _proposal(source={**VALID_SOURCE, "engine_task_id": "e-000043"}),
        registry,
    )
    assert second.status == "skipped_duplicate"  # 同事件防重拦（同 ref_id+action_id）
    assert await _count(tm_engine) == 1


# ==== 同事件防重（A25，决策 17）：同 ref_id + action_id 不重复建议 ====


@pytest.mark.asyncio
async def test_consume_duplicate_pending_proposal_skipped(tm_engine, registry) -> None:
    """已有待审提案（status=pending）同 ref_id + action_id -> 本次不落库。"""
    first = await _consume(tm_engine, _proposal(), registry)
    assert first.status == "inserted"
    second = await _consume(
        tm_engine,
        _proposal(
            source={**VALID_SOURCE, "engine_task_id": "e-000099"},
            detail="同事件另一次触发，标题文案可不同",
        ),
        registry,
    )
    assert second.status == "skipped_duplicate"
    assert "防重" in (second.reason or "")
    assert await _count(tm_engine) == 1


@pytest.mark.asyncio
async def test_consume_duplicate_unfinished_task_skipped(tm_engine, registry) -> None:
    """已有未完成任务（status 非 done/void，经批准提案带同 ref_id+action_id）-> 跳过。"""
    maker = async_sessionmaker(tm_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        task = TmTask(
            title="跟进买家物流时效",
            detail="已批准生成的任务（open）",
            domain="demo",
            role="运营",
            due=date(2026, 9, 10),
            status="open",
            source_type="ai",
            source={**VALID_SOURCE, "proposal_id": "p-000001"},
            created_by="运营",
        )
        session.add(task)
        await session.flush()
        session.add(
            TmTaskProposalRow(
                title="跟进买家物流时效",
                detail="已批准",
                domain="demo",
                action_id="tm.proposal",
                risk="suggest",
                suggested_role="运营",
                suggested_due_days=1,
                evidence=VALID_EVIDENCE,
                source={**VALID_SOURCE, "engine_task_id": "e-000001"},
                status="approved",
                task_id=task.id,
            )
        )
    outcome = await _consume(tm_engine, _proposal(), registry)
    assert outcome.status == "skipped_duplicate"
    assert await _count(tm_engine) == 1  # 只有那条 approved，新提案未落


@pytest.mark.asyncio
async def test_consume_done_task_allows_new_proposal(tm_engine, registry) -> None:
    """同 ref_id + action_id 的任务已 done -> 防重放行（未完成任务才挂起）。"""
    maker = async_sessionmaker(tm_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        task = TmTask(
            title="已完成跟进",
            detail="已结束",
            domain="demo",
            role="运营",
            due=date(2026, 9, 1),
            status="done",
            result_note="已回复买家",
            source_type="ai",
            source={**VALID_SOURCE, "proposal_id": "p-000002"},
            created_by="运营",
        )
        session.add(task)
        await session.flush()
        session.add(
            TmTaskProposalRow(
                title="已完成跟进",
                detail="已批准并完成",
                domain="demo",
                action_id="tm.proposal",
                risk="suggest",
                suggested_role="运营",
                suggested_due_days=1,
                evidence=VALID_EVIDENCE,
                source={**VALID_SOURCE, "engine_task_id": "e-000002"},
                status="approved",
                task_id=task.id,
            )
        )
    outcome = await _consume(tm_engine, _proposal(), registry)
    assert outcome.status == "inserted"
    assert await _count(tm_engine) == 2  # 旧 approved + 新 pending


@pytest.mark.asyncio
async def test_consume_different_ref_id_lands(tm_engine, registry) -> None:
    """防重键 = (ref_id, action_id)：不同业务对象不拦。"""
    first = await _consume(tm_engine, _proposal(), registry)
    assert first.status == "inserted"
    second = await _consume(
        tm_engine,
        _proposal(
            evidence=[
                {"kind": "message", "ref_id": "msg-002", "quote": "另一条买家消息"}
            ],
            source={**VALID_SOURCE, "engine_task_id": "e-000044"},
        ),
        registry,
        whitelist={"msg-001", "msg-002"},
    )
    assert second.status == "inserted"
    assert await _count(tm_engine) == 2


# ==== 端到端（验收 A12 转交器层）：真声明链 DONE -> 转交器落库 ====


@pytest.mark.asyncio
async def test_e2e_chain_done_then_consume_lands_proposal(
    monkeypatch: pytest.MonkeyPatch, db_engine, tm_engine
) -> None:
    """tm_demo_chain（FakeAgent 桩）DONE 后，末步 TaskProposal 经转交器落 pending。

    零网络零真 token：demo_echo 用标准桩（全链仅 1 次桩调用），demo_propose
    为纯代码工序（reason:none）跳过 REASON；转交器按链数据白名单（ref_id=
    链输入 m-001）校验通过 -> tm.task_proposal 落 pending。
    """
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
        {"text": "买家询问物流时效", "ref_id": "m-001", "kind": "message"},
    )
    assert result.status == "done"
    assert agents[0].calls == 1  # 全链仅 demo_echo 调 1 次 LLM（桩）

    # 末步（demo_propose）输出落库 = TaskProposal（链产物）
    from engine.core.db import get_step, get_task

    task = await get_task(db_engine, result.task_id)
    assert task is not None
    row = await get_step(db_engine, task.current_step_row)
    assert row is not None and row.status == "done"
    proposal = TaskProposal.model_validate(row.output)
    assert proposal.source.worker_id == "demo_propose"

    # 转交器落库：白名单 = 本链喂给 AI 的数据集合（链输入 ref_id）
    outcome = await _consume(
        tm_engine, proposal, registry, whitelist={"m-001"}
    )
    assert outcome.status == "inserted"
    landed = await _get(tm_engine, outcome.proposal_id)
    assert landed.status == "pending"
    assert landed.source["engine_task_id"] == f"e-{result.task_id:06d}"
    assert landed.evidence[0]["ref_id"] == "m-001"

    # 链重跑同任务再交付 -> 幂等跳过（A18）
    again = await _consume(tm_engine, proposal, registry, whitelist={"m-001"})
    assert again.status == "skipped_idempotent"
    assert await _count(tm_engine) == 1
