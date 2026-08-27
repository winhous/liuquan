"""P2 凭据端点零容忍（详设-v0.1 §9；规范 R10 机器层、安全红线的静态执法）。

规则配置（对象与豁免写死于此，详设 §9 原文「对象与豁免写死在规则配置里」）：
- 对象：repo_root 下全部 .py（engine/ models/ tests/ migrations/ 及顶层 .py；
  tests/ 只有 fixtures/ 子树豁免）
- 豁免子树：engine/core/llm/（T8 落地，全仓唯一合法持有 base_url/密钥
  形态处）、tests/fixtures/（测试样本）、scripts/（运维脚本）

检测方式（T2 选型报告：AST，理由：判据精确落在「字面量」上且不误伤注释
文本；正则会把说明性注释一起拉红）：逐文件解析后检查字符串常量内容
（含文档字符串与 f-string 的字面部分）与赋值/环境变量结构；# 注释不进
AST，不在检测范围（判据边界：注释中的泄漏不拦，留待 P1 词表/后续规则评估）。

四类判定（对应详设 §9 P2 的四行）：
1. URL/IP 字面量：字符串常量含 http 或 https 的 scheme 子串，或含 IPv4
   四段点分形态（数字.数字.数字.数字）
2. 密钥前缀串：字符串常量以 sk- 开头（详设原文「sk- 前缀字符串」，前缀判据）
3. 敏感名赋字面量：赋值目标名字含 api_key/apikey/password/token
   （不分大小写，子串匹配，覆盖 DEEPSEEK_API_KEY/db_password/auth_token
   命名变体）被赋非空字面量 -> 违规；参数声明与传参不拦（详设 §9
   「只拦赋值字面量」，防误杀）。判据边界：None/空串占位不拦；
   tokenizer 类含 token 子段的命名可能误报，属零容忍取舍（此类代码应住
   engine/core/llm/ 豁免区）
4. .env 之外读环境变量：os.environ / os.getenv 的使用（含
   from os import environ/getenv 形态）。唯一合法来源是 models.yaml 的
   env: 前缀（T8 机制）与 .env；.py 里读环境变量即违规

本模块自身在扫描对象内（详设 §9 扫全仓，lint 扫自己也要过）：
检测用的 scheme / sk- 前缀常量以运行期拼接构造（"ht" 加 "tp://" 形态），
源码中不存在任何单一字符串常量含完整 scheme 或以 sk- 开头。
注意：本包所有文件的文档字符串同属字符串常量--文档里写 scheme/IP 时
必须写成分段形态，不得连写完整字面量（连写即自检红，测试可复现）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .rules import Rule, Violation, iter_py_files, parse_module, rel_posix

RULE_ID = "P2"

# ---- 豁免子树（规则配置，详设 §9）----
EXEMPT_SUBTREES = (
    "engine/core/llm",  # T8：PydanticAI 封装，唯一合法持有 base_url/密钥形态
    "tests/fixtures",  # 测试样本数据
    "scripts",  # 运维脚本（§9 P2 对象原文豁免；P3-2/P3-4 仍扫 scripts/）
)

# 检测前缀运行期拼接（见模块 docstring 自检说明）
_SCHEMES = ("ht" + "tp://", "ht" + "tps://")
_SK_PREFIX = "s" + "k-"

_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_SENSITIVE_NAME_RE = re.compile(r"api[_-]?key|password|token", re.IGNORECASE)
_ENV_ATTRS = frozenset({"environ", "getenv"})
_ENV_NAME_IDS = frozenset({"environ", "getenv"})
_ENV_MSG = (
    "读取环境变量（os.environ/os.getenv 绕路）--"
    "唯一合法来源是 models.yaml 的 env: 前缀（T8）与 .env 文件"
)


def _literal_violation(text: str) -> str | None:
    """字符串常量内容检查：URL scheme / IPv4 / sk- 前缀。"""
    for scheme in _SCHEMES:
        if scheme in text:
            return f"URL 字面量（{text[:60]!r} 含 {scheme}）"
    match = _IPV4_RE.search(text)
    if match is not None:
        return f"IPv4 字面量（{text[:60]!r} 含 {match.group(0)}）"
    if text.startswith(_SK_PREFIX):
        return f"疑似密钥直值（sk- 前缀）：{text[:60]!r}"
    return None


def _assignment_violations(
    node: ast.Assign | ast.AnnAssign, rel: str
) -> list[Violation]:
    """敏感名赋「字面量」检查（详设 §9：只拦赋值字面量，防误杀传参形态）。"""
    if isinstance(node, ast.Assign):
        targets: list[ast.expr] = list(node.targets)
        value = node.value
    else:
        targets = [node.target]
        value = node.value
    if not isinstance(value, ast.Constant):
        return []  # 传参/变量/调用结果不是字面量
    literal = value.value
    if literal is None or (isinstance(literal, (str, bytes)) and not literal):
        return []  # 空占位（None/空串）不构成泄漏
    out: list[Violation] = []
    for target in targets:
        names: list[str] = []
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
        elif isinstance(target, ast.Subscript):
            sl = target.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                names.append(sl.value)
        for name in names:
            if _SENSITIVE_NAME_RE.search(name):
                out.append(
                    Violation(
                        RULE_ID,
                        rel,
                        target.lineno,
                        f"敏感名 {name!r} 赋字面量（密钥/密码/令牌只许来自"
                        " models.yaml env: 前缀或 .env，规范 R10/R20）",
                    )
                )
    return out


class P2CredentialsRule(Rule):
    """P2 凭据端点零容忍：URL/IP/sk-/敏感名赋值/环境变量绕路五路检测。"""

    rule_id = RULE_ID
    title = "凭据端点零容忍"

    def check(self, repo_root: Path) -> list[Violation]:
        violations: list[Violation] = []
        for path in iter_py_files(repo_root, skip_subtrees=EXEMPT_SUBTREES):
            rel = rel_posix(repo_root, path)
            tree, error = parse_module(path)
            if tree is None:
                violations.append(
                    Violation(RULE_ID, rel, 0, f"无法解析（fail-closed）：{error}")
                )
                continue
            violations.extend(self._scan_module(tree, rel))
        return violations

    @staticmethod
    def _scan_module(tree: ast.Module, rel: str) -> list[Violation]:
        violations: list[Violation] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literal_msg = _literal_violation(node.value)
                if literal_msg is not None:
                    violations.append(Violation(RULE_ID, rel, node.lineno, literal_msg))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                violations.extend(_assignment_violations(node, rel))
            elif isinstance(node, ast.Attribute):
                if node.attr in _ENV_ATTRS:
                    violations.append(Violation(RULE_ID, rel, node.lineno, _ENV_MSG))
            elif isinstance(node, ast.Name):
                if node.id in _ENV_NAME_IDS and isinstance(node.ctx, ast.Load):
                    violations.append(Violation(RULE_ID, rel, node.lineno, _ENV_MSG))
        return violations
