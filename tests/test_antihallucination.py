"""v0.2 T7 禁幻觉三件套反向测试（详设-v0.2 §7.5 样本 A/B/C/D + §3.2/§5；
对应详设 §9 文件清单的 tests/test_antihallucination.py）。

用 tests/fixtures/tm 假样本（加载即类型校验）驱动转交器
（engine/actions/tm_proposal.py 的 consume_task_proposal）：
- 样本 A（合法）  : ref_id 命中 fake provider 数据集合 + audit_id 可查 -> 落库
- 样本 B（幻觉）  : 集合外 ref_id -> 拒落 + 「幻觉证据: ref_id=...」（决策 16）
- 样本 C（空证据）: evidence=[] -> 拒落（无依据不出建议，详设 v0.1 §6.4）
- 样本 D（断链）  : audit_ids 指向不存在的引擎审计记录 -> 拒落（追溯保证）

白名单来源 = tests/fake_providers.py 的 FakeDemoInbox（demo 域「链喂给 AI
的数据集合」provider，兼作决策 16 禁幻觉校验白名单来源，§7.5）；注册按
环境加载 tests/fake_providers.yaml（详设 v0.1 §13 模式）。

基建复用（from test_runner import ...，fixture 随模块收集）：db_engine +
_clean_engine_tables（autouse 清引擎库）；业务库走 tm_pg_cluster（conftest
会话级）+ 本模块 tm_engine + _clean_tm_tables（每测试后清 tm 三表）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：零 URL/IP/
sk- 字面量、不给敏感名赋非空字面量、不读 os.environ。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from engine.actions import consume_task_proposal
from engine.core.llm import load_models
from engine.registry import load_registry
from fake_providers import FakeDemoInbox, load_providers, whitelist_from
from fixtures.tm.load import (
    evidence_sample,
    task_model,
    task_proposal_sample,
)
from models.tm import TaskProposal as TmTaskProposalRow
from test_runner import (  # noqa: F401  # 跨模块 fixture 随模块收集（autouse 清引擎库）
    _clean_engine_tables,
    db_engine,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """models.yaml 的 env: 引用所需环境变量（R20：值只进测试环境变量）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


@async_fixture
async def tm_engine(tm_pg_cluster):
    """业务库 AsyncEngine（嵌入式 PG，NullPool 防跨循环串连接）。"""
    engine = create_async_engine(tm_pg_cluster.url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@async_fixture
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


@pytest.fixture
def inbox() -> FakeDemoInbox:
    """白名单来源：demo 域「链喂给 AI 的数据集合」桩（决策 16，§7.5）。"""
    return FakeDemoInbox()


async def _count(tm_engine) -> int:
    async with AsyncSession(tm_engine) as session:
        return len(
            (await session.execute(select(TmTaskProposalRow.id))).scalars().all()
        )


# ==== 假数据工具落地：加载即类型校验（结构漂移测试自然红）====


def test_fixture_samples_load_with_type_validation() -> None:
    """fixtures/tm 全样本过契约校验（evidence 五类 / 提案 A-D / 任务六形态）。"""
    for name in ("message", "metric", "order_view", "listing", "image"):
        ev = evidence_sample(name)
        assert ev.ref_id  # EvidenceRef 契约：kind/ref_id/quote 齐
    for name in ("valid", "hallucinated", "empty_evidence", "broken_audit"):
        prop = task_proposal_sample(name)
        assert prop.action_id == "tm.proposal"
        assert prop.source.worker_id  # SourceTrace 追溯字段齐
    for name in (
        "open_ai_task", "in_progress_manual_task", "done_task",
        "void_task", "overdue_task", "derived_task",
    ):
        task = task_model(name)
        assert task.title and task.domain and task.due  # tm.task 必填列齐
    # 结构漂移自然红：未知列 / 必填空值被加载器拒（R22 结构漂移活不过启动）
    from fixtures.tm.load import task_sample

    with pytest.raises(ValueError):
        task_sample("open_ai_task", ghost_column="x")
    with pytest.raises(ValueError):
        task_sample("open_ai_task", source_type=None)


def test_fake_providers_yaml_registers_demo_domain() -> None:
    """fake_providers.yaml 注册 demo 域两 provider + v0.3 crm/tm 两 provider；
    inbox 数据集合 = 白名单来源（demo 域语义不变）。"""
    providers = load_providers()
    assert set(providers) == {
        "demo.greeting", "demo.inbox", "crm.chat_context", "tm.task_context",
    }
    whitelist = whitelist_from(providers)
    assert set(FakeDemoInbox().whitelist()) <= whitelist
    assert "msg-001" in whitelist  # 集合内对象 id
    assert "msg-999" not in whitelist  # 集合外（样本 B 构造）
    assert "1" in whitelist  # FakeCrmChatContext 消息 id（v0.3）


# ==== 禁幻觉三件套反向（详设 §7.5 样本 A/B/C/D，A21 落点）====


@pytest.mark.asyncio
async def test_sample_a_valid_lands_with_provider_whitelist(
    tm_engine, _clean_tm_tables, registry, inbox
) -> None:
    """样本 A：ref_id 命中 fake provider 集合 + audit_id 可查 -> 落库成功。"""
    outcome = await consume_task_proposal(
        task_proposal_sample("valid"),
        tm_engine=tm_engine,
        registry=registry,
        whitelist=inbox.whitelist(),  # 白名单来源 = fake provider 数据集合
        audit_lookup=lambda ids: True,  # 可查（测试注入桩 lookup）
    )
    assert outcome.status == "inserted"
    assert outcome.proposal_id is not None
    assert await _count(tm_engine) == 1


@pytest.mark.asyncio
async def test_sample_b_hallucinated_ref_id_rejected(
    tm_engine, _clean_tm_tables, registry, inbox
) -> None:
    """样本 B：集合外 ref_id（fake provider 没喂过的对象）-> 拒落 + 幻觉证据。"""
    outcome = await consume_task_proposal(
        task_proposal_sample("hallucinated"),
        tm_engine=tm_engine,
        registry=registry,
        whitelist=inbox.whitelist(),
        audit_lookup=lambda ids: True,
    )
    assert outcome.status == "rejected"
    assert (outcome.reason or "").startswith("幻觉证据: ref_id=msg-999")
    assert await _count(tm_engine) == 0


@pytest.mark.asyncio
async def test_sample_c_empty_evidence_rejected(
    tm_engine, _clean_tm_tables, registry, inbox
) -> None:
    """样本 C：evidence=[] -> 拒落（无依据不出建议，详设 v0.1 §6.4）。"""
    outcome = await consume_task_proposal(
        task_proposal_sample("empty_evidence"),
        tm_engine=tm_engine,
        registry=registry,
        whitelist=inbox.whitelist(),
        audit_lookup=lambda ids: True,
    )
    assert outcome.status == "rejected"
    assert "evidence 为空" in (outcome.reason or "")
    assert await _count(tm_engine) == 0


@pytest.mark.asyncio
async def test_sample_d_broken_audit_rejected(
    tm_engine, _clean_tm_tables, registry, inbox
) -> None:
    """样本 D：audit_ids 指向不存在的引擎审计记录 -> 拒落（追溯保证）。"""
    outcome = await consume_task_proposal(
        task_proposal_sample("broken_audit"),
        tm_engine=tm_engine,
        registry=registry,
        whitelist=inbox.whitelist(),
        audit_lookup=lambda ids: False,  # 不可查（引擎库查不到）
    )
    assert outcome.status == "rejected"
    assert "audit_ids 不可查" in (outcome.reason or "")
    assert await _count(tm_engine) == 0
