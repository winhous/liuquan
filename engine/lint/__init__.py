"""engine.lint：静态执法 lint（详设-v0.1 §9；scripts/check.sh 四绿之绿 1）。

T2 已落地：P2 凭据端点零容忍 + P3 契约纪律（四条子规则）。
T11 已落地：P1 业务词黑名单（词表 = 基础词表 ∪ config/ 自动生成，
重复扫 = 词表自动生长）。新增规则 = 新规则文件 + 本模块 RULES 注册
一行，规则框架（rules.py）不动。

公共 API：``run_all(repo_root) -> list[Violation]``；
CLI：``python -m engine.lint``（0 违规退出 0，有违规打印明细退出 1）。

本包所有文件自身也在 P2 扫描对象内（详设 §9「扫全仓」）：
检测用的 scheme / 密钥前缀常量在各规则文件以运行期拼接构造；
本包文档字符串不得出现完整 scheme 或 IPv4 形态（docstring 属字符串
常量，会被自身规则扫描，测试有对应断言）。
"""

from __future__ import annotations

from pathlib import Path

from .p1 import P1BusinessTermsRule
from .p2 import P2CredentialsRule
from .p3 import (
    P3Rule1WorkerSignature,
    P3Rule2CouplingImports,
    P3Rule3ModelsYamlSecrets,
    P3Rule4StubLeak,
)
from .rules import Rule, Violation

__all__ = ["RULES", "Rule", "Violation", "run_all"]

# 规则注册表（新增规则 = 新规则文件 + 此处一行，框架不改）
RULES: tuple[Rule, ...] = (
    P1BusinessTermsRule(),
    P2CredentialsRule(),
    P3Rule1WorkerSignature(),
    P3Rule2CouplingImports(),
    P3Rule3ModelsYamlSecrets(),
    P3Rule4StubLeak(),
)


def run_all(repo_root: Path) -> list[Violation]:
    """跑全部规则；违规按 (file, line, rule_id) 排序（输出确定序）。"""
    violations: list[Violation] = []
    for rule in RULES:
        violations.extend(rule.check(repo_root))
    violations.sort(key=lambda v: (v.file, v.line, v.rule_id))
    return violations
