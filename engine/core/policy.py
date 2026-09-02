"""T5 域策略：风险四级门禁 + §5.2 域权限矩阵（engine/core/policy.py）。

纯逻辑模块（详设-v0.1 §5）：不碰 DB、不碰 LLM。
- §5.1 风险四级：read / suggest / write / transaction；
  transaction 恒拒（loader 拒载，一期无对外事务）。
- §5.2 域权限矩阵：六行全定义（demo/crm/tm/erp/seo/scrape），
  v0.1 实际启用 demo/crm 两行，其余域运行时拒。
- check_policy 三路拒：transaction 恒拒 / 越域读（provider 域 != 工序域，
  v0.1 直接拒）/ 风险超域上限；未启用域也拒。
- 域的可读前缀（如 crm.）、可写目标（如 tm.proposal）为声明性元数据，
  loader 侧校验同源使用；运行时越域判据 = 工序声明的 Context provider
  必须落在本域可读前缀内。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Risk(str, Enum):
    """动作风险四级（详设 §5.1）：read < suggest < write < transaction（恒拒）。"""

    READ = "read"  # 只分析不落库（翻译、摘要），Policy 直过
    SUGGEST = "suggest"  # 产出 TaskProposal，全进审核页人工批
    WRITE = "write"  # 写业务库，写前过业务规则代码校验 + 幂等 upsert
    TRANSACTION = "transaction"  # 对外部系统真实事务（一期不存在，loader 拒载）


class Domain(str, Enum):
    """工序/链域枚举（详设 §4.1 原文：crm/tm/erp/seo/scrape/demo）。"""

    DEMO = "demo"
    CRM = "crm"
    TM = "tm"
    ERP = "erp"
    SEO = "seo"
    SCRAPE = "scrape"


@dataclass(frozen=True)
class DomainPermission:
    """一行域权限（详设 §5.2）：可读 Context 前缀 / 可写目标 / 风险上限。"""

    read_prefix: str  # 可读 Context 前缀（如 "crm."）
    write_target: str | None  # 可写目标（None = 无写目标，输出只进引擎库 output）
    risk_ceiling: Risk  # 风险上限


# 域权限矩阵（详设 §5.2 六行全定义；demo/crm 之外的行 v0.1 未启用）
# v0.2 T4 修订：demo 域风险上限 read -> suggest——演示链 tm_demo_chain 的
# demo_propose 工序以 suggest 风险产出 TaskProposal（详设-v0.2 §7），
# 超出 v0.1 的 read 上限；demo 域为演示域，无真实业务写，suggest 不引入风险。
DOMAIN_PERMISSIONS: dict[Domain, DomainPermission] = {
    Domain.DEMO: DomainPermission("demo.", None, Risk.SUGGEST),
    Domain.CRM: DomainPermission("crm.", "crm schema", Risk.WRITE),
    Domain.TM: DomainPermission("tm.", "tm.proposal", Risk.SUGGEST),
    Domain.ERP: DomainPermission("erp.", "erp 分析结果表", Risk.SUGGEST),
    Domain.SEO: DomainPermission("seo.", "seo schema", Risk.WRITE),
    Domain.SCRAPE: DomainPermission("scrape.", "scrape schema", Risk.WRITE),
}

# v0.1 实际启用 demo/crm 两行（详设 §5.2 原文）；v0.3 增启用 tm——
# tm_intent 工序（流转自然语言入口，决策 28）与 tm_task_context provider 属 tm 域，
# 需要 tm 域放行（详设-v0.3 §5.1；tm 域风险上限 suggest，无真实业务写）。
# v0.6 批 4 增启用 scrape——下载链/选品链要在引擎 runner 真跑（链路修通，
# 详设-v0.6 §5/§10 A57-A68；scrape 域上限 write 已含链接/图片写接口）。
ENABLED_DOMAINS: frozenset[Domain] = frozenset(
    {Domain.DEMO, Domain.CRM, Domain.TM, Domain.SCRAPE}
)

# 风险序：read < suggest < write（transaction 恒拒，不进序表）
_RISK_LEVEL: dict[Risk, int] = {
    Risk.READ: 0,
    Risk.SUGGEST: 1,
    Risk.WRITE: 2,
}


@dataclass(frozen=True)
class PolicyResult:
    """check_policy 结果：ok 是否放行 + reason 判定依据（拒时必为显式原因）。"""

    ok: bool
    reason: str


def risk_allowed(risk: Risk) -> bool:
    """风险门禁（详设 §5.1）：transaction 恒 False，其余级放行（域上限另判）。"""
    return risk is not Risk.TRANSACTION


def _coerce_domain(value: Domain | str) -> Domain:
    return value if isinstance(value, Domain) else Domain(value)


def _coerce_risk(value: Risk | str) -> Risk:
    return value if isinstance(value, Risk) else Risk(value)


def check_policy(
    domain: Domain | str,
    risk: Risk | str,
    context_domains: Iterable[str] | None = None,
) -> PolicyResult:
    """域权限门禁（详设 §5.2）：transaction 恒拒 / 域未启用 / 越域读 / 风险越限。

    context_domains：本工序声明的 Context provider 标识（如 "crm.customer_history"）
    或裸域 token（如 "crm"）；任一不在本域可读前缀内 = 越域读，v0.1 直接拒
    （详设 §5.2 原文：越域访问 v0.1 直接拒，L2 校验 + Policy 运行时双保险）。
    """
    dom = _coerce_domain(domain)
    act = _coerce_risk(risk)
    if not risk_allowed(act):
        return PolicyResult(
            False,
            f"transaction 恒拒：域 {dom.value} 声明动作 transaction"
            "（详设 §5.1 一期无对外真实事务，loader 拒载）",
        )
    if dom not in ENABLED_DOMAINS:
        return PolicyResult(
            False, f"域 {dom.value} 在 v0.1 未启用（详设 §5.2 实际启用 demo/crm 两行）"
        )
    perm = DOMAIN_PERMISSIONS[dom]
    for provider in context_domains or ():
        if not (provider == dom.value or provider.startswith(perm.read_prefix)):
            return PolicyResult(
                False,
                f"越域读：工序域 {dom.value} 引用了 Context provider {provider}，"
                f"不在本域可读前缀 {perm.read_prefix}* 内（详设 §5.2 越域直接拒）",
            )
    if _RISK_LEVEL[act] > _RISK_LEVEL[perm.risk_ceiling]:
        return PolicyResult(
            False,
            f"风险越限：域 {dom.value} 风险上限 {perm.risk_ceiling.value}，"
            f"请求 {act.value}（详设 §5.2）",
        )
    return PolicyResult(True, f"policy ok (domain={dom.value}, risk={act.value})")
