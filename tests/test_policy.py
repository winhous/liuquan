"""T5 域权限门禁测试（详设-v0.1 §5.1 风险四级 + §5.2 域权限矩阵）。

覆盖（任务 T5 指定 + v0.2 T4 修订）：
- demo 域 read 直过；demo 域 suggest 直过（v0.2 T4：上限 read -> suggest）；
  demo 域 write 拒（上限 suggest）
- crm 域 write 过
- transaction 恒拒（risk_allowed 恒 False + check_policy 拒）
- crm 工序引用 demo.* provider（越域）拒
- 补充：六行矩阵全定义断言 / 未启用域拒 / 字符串入参同效
"""

from __future__ import annotations

from engine.core.policy import (
    DOMAIN_PERMISSIONS,
    Domain,
    PolicyResult,
    Risk,
    check_policy,
    risk_allowed,
)


def test_demo_read_passes() -> None:
    """demo 域 read 直过（§5.2；Policy 直过，§5.1）。"""
    res = check_policy(Domain.DEMO, Risk.READ, ())
    assert isinstance(res, PolicyResult)
    assert res.ok
    assert res.reason


def test_demo_read_own_context_passes() -> None:
    """demo 域可读 demo.* Context（同域 provider 放行）。"""
    res = check_policy(Domain.DEMO, Risk.READ, ("demo.greeting",))
    assert res.ok


def test_demo_suggest_passes_v02() -> None:
    """v0.2 T4 修订：demo 域风险上限 suggest——demo_propose 产出 TaskProposal 直过。"""
    res = check_policy(Domain.DEMO, Risk.SUGGEST, ())
    assert res.ok


def test_demo_write_rejected_by_ceiling() -> None:
    """demo 域 write 拒：上限 suggest（v0.2 T4 修订），write 越限（§5.2）。"""
    res = check_policy(Domain.DEMO, Risk.WRITE, ())
    assert not res.ok
    assert "suggest" in res.reason


def test_crm_read_passes() -> None:
    """crm 域 read 直过（上限 write，read 自然允许）。"""
    res = check_policy(Domain.CRM, Risk.READ, ())
    assert res.ok


def test_crm_write_passes() -> None:
    """crm 域 write 过：上限 write，同域 Context（§5.2）。"""
    res = check_policy(Domain.CRM, Risk.WRITE, ("crm.customer_history",))
    assert res.ok


def test_transaction_always_rejected() -> None:
    """transaction 恒拒：门禁函数恒 False；check_policy 任何域都拒（§5.1）。"""
    assert risk_allowed(Risk.TRANSACTION) is False
    for domain in (Domain.DEMO, Domain.CRM):
        res = check_policy(domain, Risk.TRANSACTION, ())
        assert not res.ok
        assert "transaction" in res.reason


def test_non_transaction_risk_allowed() -> None:
    """read/suggest/write 门禁放行（域上限另由 check_policy 判）。"""
    for risk in (Risk.READ, Risk.SUGGEST, Risk.WRITE):
        assert risk_allowed(risk) is True


def test_crm_worker_referencing_demo_provider_rejected() -> None:
    """crm 工序引用 demo.* provider = 越域读，v0.1 直接拒（§5.2 原文）。"""
    res = check_policy(Domain.CRM, Risk.WRITE, ("demo.greeting",))
    assert not res.ok
    assert "越域" in res.reason


def test_unenabled_domain_rejected_in_v01() -> None:
    """六行矩阵全定义，但 v0.1 只启用 demo/crm：其余域运行时拒（§5.2 原文）。"""
    for domain in (Domain.TM, Domain.ERP, Domain.SEO, Domain.SCRAPE):
        res = check_policy(domain, Risk.READ, ())
        assert not res.ok
        assert "未启用" in res.reason


def test_matrix_defines_all_six_domains() -> None:
    """§5.2 矩阵六行全定义：可读前缀 / 可写目标 / 风险上限逐行核对。"""
    assert set(DOMAIN_PERMISSIONS) == {
        Domain.DEMO,
        Domain.CRM,
        Domain.TM,
        Domain.ERP,
        Domain.SEO,
        Domain.SCRAPE,
    }
    assert DOMAIN_PERMISSIONS[Domain.DEMO].read_prefix == "demo."
    assert DOMAIN_PERMISSIONS[Domain.DEMO].write_target is None
    assert DOMAIN_PERMISSIONS[Domain.DEMO].risk_ceiling is Risk.SUGGEST  # v0.2 T4 修订
    assert DOMAIN_PERMISSIONS[Domain.CRM].read_prefix == "crm."
    assert DOMAIN_PERMISSIONS[Domain.CRM].write_target == "crm schema"
    assert DOMAIN_PERMISSIONS[Domain.CRM].risk_ceiling is Risk.WRITE
    assert DOMAIN_PERMISSIONS[Domain.TM].write_target == "tm.proposal"
    assert DOMAIN_PERMISSIONS[Domain.TM].risk_ceiling is Risk.SUGGEST
    assert DOMAIN_PERMISSIONS[Domain.ERP].risk_ceiling is Risk.SUGGEST
    assert DOMAIN_PERMISSIONS[Domain.SEO].risk_ceiling is Risk.WRITE
    assert DOMAIN_PERMISSIONS[Domain.SCRAPE].risk_ceiling is Risk.WRITE


def test_string_inputs_coerced() -> None:
    """入参允许字符串（YAML 值），与枚举同效。"""
    assert check_policy("crm", "write", ("crm.customer_history",)).ok
    assert not check_policy("demo", "write", ()).ok
