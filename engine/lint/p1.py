"""P1 业务词黑名单（详设-v0.1 §9；规范 R10 硬编码治理机器层）。

规则语义：业务规格值（专名/字段清单/数量规格）不得以字面量出现在
工序与注册表代码里，必须住进工序 config/（R10 外置优先级：任务参数 >
工序 config/ > 注册表声明 > 链声明）。本规则是 R10 四层防线的机器层。

规则配置（对象与豁免写死于此，详设 §9 原文）：
- 对象：engine/workers/、engine/registry/ 下的 .py 与 .yaml
  （当前仓库实际为 engine/registry/...；engine/workers/ 不存在即跳过，
  与 p3.py 的 WORKER_ROOTS 同口径两处都扫）
- 豁免：工序 config/ 目录——业务值的合法住处，自身不扫，且是自动词表
  的生成来源（见下）
- 判定：扫描对象 .py 的字符串常量（含 docstring，与 P2 同口径）或
  .yaml 的标量值中出现词表词 -> Violation（rule_id "P1"）

词表 = 基础词表（本模块常量 BASE_TERMS，人工维护）∪ 自动生成
（每次 check 重扫各 config/ 的 yaml：键与标量值入表；重复扫 = 词表
自动生长，无需手工维护——config/ 由 T10 工序任务落地后自动生效）。

已知判据边界（防「引用不存在之物」，后续任务可收紧并留变更日志）：
- **T11 收紧（2026-08-28，T10 工序落地前置）**：config 的键不入自动词表
  （工序按键读取配置是 R10 设计机制，键字面量不许打成违规）；纯 ASCII 词
  按整词匹配（前后非词字符）——config 值 "echo" 出现在 chain.yaml 的
  worker id "demo_echo" 里不算命中（结构标识符合法）；含非 ASCII 词（中文
  专名）仍按子串匹配（CJK 无词边界）。若后续仍噪声过大可再收紧（如最短
  长度上调），收紧必须留变更日志
- 匹配方式：整词（ASCII）/子串（CJK）对业务词零容忍侧保守，宁可误伤不放过
- 自动词表过滤：布尔/None 值不入表（不是业务规格专名，且 isinstance
  (True, int) 陷阱需先判 bool）；长度 < 2 的词不入表（防单字符噪声，
  详设 §9 样例「13 个标签」为两位数仍入表）
- 被扫 .yaml 只检查标量「值」，不检查键（键是结构，值是业务内容）
- 无法解析的文件 fail-closed：扫描对象 .py/.yaml 与 config/ 的 yaml
  解析失败都按违规上报（解析不了就无法证明对象干净/词表完整）

自检说明：本文件不在 P1 扫描对象内（engine/lint/ 非 engine/workers|
engine/registry），但本包全文件在 P2 扫描对象内（扫全仓）——本文件
不出现完整 URL/IP/密钥前缀字面量形态；示例业务专名只进注释与测试，
不写进真仓库的 engine/registry/、engine/workers/（这两个目录保持干净）。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import yaml

from .rules import Rule, Violation, parse_module, rel_posix

RULE_ID = "P1"

# ---- 规则配置（对象与豁免写死于此，详设 §9）----
# 扫描对象：§9 原文两目录（engine/registry/ 已含 registry/workers/...；
# engine/workers/ 不存在即跳过，T10 落地后自然生效）
SCAN_ROOTS = ("engine/workers", "engine/registry")
# 工序 config/ 目录名：业务值的合法住处、自动词表生成来源、自身豁免扫描
CONFIG_DIR_NAME = "config"

# ---- 基础词表（人工维护；扩展方式：往此元组追加业务专名）----
# 例：BASE_TERMS = ("飞书", "13")  # 飞书列名、标签数量等长期稳定的业务专名
# 说明：基础词表是长期稳定的业务专名；工序 config/ 出现后的专名由自动
# 词表覆盖，无需在此重复登记。空表 = 本期无历史业务专名，T10 工序
# config/ 落地后自动词表开始生长（详设 §9：重复扫 = 词表自动生长）。
BASE_TERMS: tuple[str, ...] = ()

# ---- 自动词表提取过滤（见模块 docstring 判据边界）----
_MIN_TERM_LEN = 2

# 文件收集剪枝（与 rules.py 同口径：环境/缓存产物不进扫描对象）
_PRUNE_DIR_NAMES = frozenset(
    {"__pycache__", "node_modules", "dist", "build", ".pgdata"}
)
_PRUNE_DIR_SUFFIXES = (".egg-info",)
_YAML_SUFFIXES = (".yaml", ".yml")


def _keep_term(text: str) -> bool:
    """词表词过滤：去空白后长度 >= _MIN_TERM_LEN。"""
    return len(text.strip()) >= _MIN_TERM_LEN


def _iter_scan_files(repo_root: Path, suffixes: tuple[str, ...]):
    """扫描对象内、config/ 豁免外的指定后缀文件（绝对路径）。"""
    for root in SCAN_ROOTS:
        base = repo_root / root
        if not base.is_dir():
            continue  # 目录不存在的规则对象 = 跳过记绿（框架约定）
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not name.startswith(".")
                and name not in _PRUNE_DIR_NAMES
                and not name.endswith(_PRUNE_DIR_SUFFIXES)
            )
            for filename in sorted(filenames):
                if not filename.endswith(suffixes):
                    continue
                path = Path(dirpath) / filename
                if CONFIG_DIR_NAME in path.relative_to(repo_root).parts:
                    continue  # config/ 自身豁免扫描
                yield path


def _iter_config_yamls(repo_root: Path):
    """各工序 config/ 下的 yaml 文件（自动词表生成来源）。"""
    for root in SCAN_ROOTS:
        base = repo_root / root
        if not base.is_dir():
            continue
        for config_dir in base.rglob(CONFIG_DIR_NAME):
            if not config_dir.is_dir():
                continue
            for path in sorted(config_dir.rglob("*")):
                if path.is_file() and path.suffix in _YAML_SUFFIXES:
                    yield path


def _extract_terms(data: object) -> set[str]:
    """config yaml 数据的**标量值** -> 词表词集合（布尔/None/短词过滤）。

    T11 收紧（2026-08-28，T10 落地前置）：config 的**键**不入表——
    工序按键读取配置是 R10 设计机制（run.py 的 ctx.config["tone"] 含键
    字面量，键入表会把设计机制打成违规）。业务规格值（专名/数量规格）仍
    在表内（这是 P1 的本意）。
    """
    terms: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, bool):
            return  # 布尔值不是业务规格专名（isinstance(True, int) 陷阱先判）
        elif isinstance(node, str):
            if _keep_term(node):
                terms.add(node.strip())
        elif isinstance(node, (int, float)):
            text = str(node)
            if _keep_term(text):
                terms.add(text)

    walk(data)
    return terms


def _collect_auto_table(repo_root: Path) -> tuple[set[str], list[Violation]]:
    """扫各 config/ yaml 的键与值自动生成词表；解析失败 fail-closed 上报。"""
    terms: set[str] = set()
    errors: list[Violation] = []
    for path in _iter_config_yamls(repo_root):
        rel = rel_posix(repo_root, path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            errors.append(
                Violation(
                    RULE_ID, rel, 0, f"config/ 解析失败（fail-closed）：{exc}"
                )
            )
            continue
        if data is not None:  # 空文件不入表
            terms.update(_extract_terms(data))
    return terms, errors


def _term_violations(
    rel: str, lineno: int, text: str, terms: set[str]
) -> list[Violation]:
    """text 中出现词表词 -> 每词一条违规（词序确定：排序后逐个判）。

    T11 收紧（2026-08-28，T10 落地前置）：纯 ASCII 词（^[A-Za-z0-9_]+$）
    按**整词**匹配（前后都不能是词字符）——config 值如 "echo" 出现在
    "demo_echo"（chain.yaml 的 worker id）里不算命中（结构标识符合法）；
    含非 ASCII 的词（中文专名等）仍按子串匹配（CJK 无词边界，\b 失效）。
    """
    out: list[Violation] = []
    for term in sorted(terms):
        if _term_in_text(term, text):
            out.append(
                Violation(
                    RULE_ID,
                    rel,
                    lineno,
                    f'业务词字面量 "{term}"'
                    "（业务规格值必须住进工序 config/，规范 R10 机器层）",
                )
            )
    return out


def _term_in_text(term: str, text: str) -> bool:
    """词表词命中判定：纯 ASCII 词整词匹配（词边界），其余子串匹配。"""
    if re.fullmatch(r"[A-Za-z0-9_]+", term):
        # 前后都不能是词字符（\w 含下划线）：demo_echo 里的 echo 不算命中
        pattern = f"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])"
        return re.search(pattern, text) is not None
    return term in text


def _iter_scalar_values(node: yaml.Node):
    """走 yaml compose 节点树，只产出标量「值」节点（键不产，见判据边界）。"""
    if isinstance(node, yaml.MappingNode):
        for _key_node, value_node in node.value:
            yield from _iter_scalar_values(value_node)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _iter_scalar_values(item)
    elif isinstance(node, yaml.ScalarNode):
        yield node


class P1BusinessTermsRule(Rule):
    """业务词黑名单：扫描对象出现基础词表/config 自动词表词 -> 违规。"""

    rule_id = RULE_ID
    title = "业务词黑名单（R10 硬编码治理）"

    def __init__(self, base_terms: tuple[str, ...] = BASE_TERMS) -> None:
        # 基础词表经构造注入可覆盖（测试注入样本；生产用模块常量 BASE_TERMS）
        self._base_terms = base_terms

    def check(self, repo_root: Path) -> list[Violation]:
        auto_terms, violations = _collect_auto_table(repo_root)
        terms = {
            term.strip()
            for term in (*self._base_terms, *auto_terms)
            if _keep_term(term)
        }
        violations.extend(self._scan_py(repo_root, terms))
        violations.extend(self._scan_yaml(repo_root, terms))
        violations.sort(key=lambda v: (v.file, v.line, v.message))
        return violations

    def _scan_py(self, repo_root: Path, terms: set[str]) -> list[Violation]:
        out: list[Violation] = []
        for path in _iter_scan_files(repo_root, (".py",)):
            rel = rel_posix(repo_root, path)
            tree, error = parse_module(path)
            if tree is None:
                out.append(
                    Violation(RULE_ID, rel, 0, f"无法解析（fail-closed）：{error}")
                )
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    out.extend(
                        _term_violations(rel, node.lineno, node.value, terms)
                    )
        return out

    def _scan_yaml(self, repo_root: Path, terms: set[str]) -> list[Violation]:
        out: list[Violation] = []
        for path in _iter_scan_files(repo_root, _YAML_SUFFIXES):
            rel = rel_posix(repo_root, path)
            try:
                root_node = yaml.compose(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                out.append(
                    Violation(
                        RULE_ID, rel, 0, f"YAML 解析失败（fail-closed）：{exc}"
                    )
                )
                continue
            if root_node is None:  # 空文件无标量
                continue
            for node in _iter_scalar_values(root_node):
                out.extend(
                    _term_violations(
                        rel, node.start_mark.line + 1, str(node.value), terms
                    )
                )
        return out
