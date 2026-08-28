"""P3 契约纪律（详设-v0.1 §9；规范 R6/R12/R21-R24 的静态执法）。

四条子规则各一个类（T11 的 P1 插入同样是「新文件 + 注册一行」，不改框架）：

- P3-1 工序签名：run(inputs: RegisteredModel, ctx: EngineContext) -> RegisteredModel
- P3-2 低耦合 import（R24）：web/ 与 scripts/ 不得 import engine.workers.* /
  engine.core.*；工序 run.py 不得 import 其他 worker 的模块
- P3-3 models.yaml 密钥零直值：api_key 值必须 env: 前缀引用；另拦任意 sk- 直值
- P3-4 桩泄漏（R12）：生产目录出现 Fake*/Stub* 前缀类 -> 违规；tests/ 豁免

已知判据边界（防「引用不存在之物」，均待后续任务收紧并留变更日志）：
- 工序目录写法：T10 已定型为 engine/registry/workers/（详设 §4.1/§11 口径，
  §9/规范 R24 旧口径 engine/workers/ 已同步废弃）；本规则仍双扫两目录、
  拦两种 import 前缀（兼容存量），后续任务可收敛为单目录并留变更日志
- P3-1 宽松判据（T2）：AST 签名解析 + 裸 dict/内建注解拦截；注解名必须由
  本文件 import 绑定（「模块可解析」的静态代理）；「未登记 Model」的真校验
  归 T4 registry-check；EngineContext 精确类型 T9 定型后收紧
- P3-3 只拦 api_key 键（详设 §9 原文）+ 全树密钥前缀直值兜底；其他键名待 T8 扩
- P3-4 以类名前缀 Fake*/Stub* 为主判据（与规范 R12 判定式同款，区分大小写）；
  「固定假交付数据」的非类形态难穷尽，由 P1 词表（T11）与人工 review 兜底
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from .rules import Rule, Violation, parse_module, rel_posix

# ---- 规则配置（对象与豁免写死于此，详设 §9 / 规范 R12 / R24）----
# 工序目录两处都扫（T10 定型为 registry/workers；双扫兼容旧口径，见 docstring）
WORKER_ROOTS = ("engine/workers", "engine/registry/workers")
# web/ 与 scripts/ 禁止 import 的模块前缀（R24；registry.workers 为目录别名写法，同拦）
FORBIDDEN_IMPORT_PREFIXES = ("engine.workers", "engine.core", "engine.registry.workers")
# P3-4 桩泄漏的生产目录（R12：tests/ 豁免；web/ 不存在即跳过）
PROD_STUB_DIRS = ("engine", "models", "web", "scripts")
# P3-4 桩类名前缀（规范 R12 判定式同款，区分大小写）
STUB_CLASS_PREFIXES = ("Fake", "Stub")

# 密钥前缀运行期拼接构造：p3.py 自身在 P2 扫描对象内，
# 常量直接写前缀会自检红（见 p2 模块 docstring 说明）
_SK_PREFIX = "s" + "k-"
_YAML_FILE = "models.yaml"
_ENV_PREFIX = "env:"

# P3-1：注解不得是裸内建/Any（R22：入参出参必须是 Model）
_PRIMITIVE_ANNOTATIONS = frozenset(
    {
        "dict",
        "list",
        "set",
        "tuple",
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "Any",
        "Optional",
    }
)
_WORKERS_NS_PREFIXES = ("engine.workers", "engine.registry.workers")


# ==== P3-1 工序签名 ====


def _annotation_root(node: ast.expr | None) -> str | None:
    """取注解的根名：Name 直取；Attribute 取最左根（a.b.C -> a）；
    Subscript 取容器名（list[X] -> list）；X | None 取左支；字符串注解取首段。"""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _annotation_root(node.value)
    if isinstance(node, ast.Subscript):
        return _annotation_root(node.value)
    if isinstance(node, ast.BinOp):
        return _annotation_root(node.left)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        first = node.value.strip().lstrip("[")
        return first.split(".")[0].split("[")[0] or None
    return None


def _import_bound_names(tree: ast.Module) -> set[str]:
    """本文件 import 语句绑定的名字集合（「模块可解析」静态代理判据）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _annotation_violation(
    rel: str,
    node: ast.expr | None,
    label: str,
    lineno: int,
    bound: set[str],
) -> list[Violation]:
    if node is None:
        return [
            Violation(
                "P3-1", rel, lineno, f"{label}缺失（R6/R22：入参出参必须是具体 Model）"
            )
        ]
    root = _annotation_root(node)
    if root is None:
        return [Violation("P3-1", rel, lineno, f"{label}不可判定（复杂注解表达式）")]
    if root in _PRIMITIVE_ANNOTATIONS:
        return [
            Violation(
                "P3-1",
                rel,
                lineno,
                f"{label}为内建类型 {root}（裸 dict/内建不是 RegisteredModel，R22）",
            )
        ]
    if root not in bound:
        return [
            Violation(
                "P3-1",
                rel,
                lineno,
                f"{label} {root} 未由本文件 import 绑定（模块不可解析；"
                "宽松判据，T9 定型后收紧）",
            )
        ]
    return []


class P3Rule1WorkerSignature(Rule):
    """工序契约签名检查（对象目录不存在 = 跳过记绿，T10 落地后生效）。"""

    rule_id = "P3-1"
    title = "工序契约签名"

    def check(self, repo_root: Path) -> list[Violation]:
        violations: list[Violation] = []
        for root in WORKER_ROOTS:
            base = repo_root / root
            if not base.is_dir():
                continue
            for run_path in sorted(base.rglob("run.py")):
                violations.extend(self._check_run_file(repo_root, run_path))
        return violations

    def _check_run_file(self, repo_root: Path, run_path: Path) -> list[Violation]:
        rel = rel_posix(repo_root, run_path)
        tree, error = parse_module(run_path)
        if tree is None:
            return [Violation(self.rule_id, rel, 0, f"无法解析（fail-closed）：{error}")]
        runs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"]
        if not runs:
            return [Violation(self.rule_id, rel, 1, "缺少顶层 run 函数（R6 工序契约）")]
        if len(runs) > 1:
            return [
                Violation(self.rule_id, rel, runs[0].lineno, "顶层 run 函数重复定义")
            ]
        fn = runs[0]
        args = fn.args
        positional = [*args.posonlyargs, *args.args]
        out: list[Violation] = []
        if args.vararg or args.kwarg or args.kwonlyargs:
            out.append(
                Violation(
                    self.rule_id,
                    rel,
                    fn.lineno,
                    "run 签名必须恰好 inputs/ctx 两个参数"
                    "（不允许 *args/**kwargs/仅关键字参数）",
                )
            )
        if [p.arg for p in positional] != ["inputs", "ctx"]:
            actual = ", ".join(p.arg for p in positional) or "无参数"
            out.append(
                Violation(
                    self.rule_id,
                    rel,
                    fn.lineno,
                    "run 参数必须是 (inputs: RegisteredModel, ctx: EngineContext)，"
                    f"实际 ({actual})",
                )
            )
        if len(positional) == 2:
            bound = _import_bound_names(tree)
            out.extend(
                _annotation_violation(
                    rel,
                    positional[0].annotation,
                    "inputs 参数注解",
                    positional[0].lineno,
                    bound,
                )
            )
            out.extend(
                _annotation_violation(
                    rel,
                    positional[1].annotation,
                    "ctx 参数注解（EngineContext）",
                    positional[1].lineno,
                    bound,
                )
            )
            out.extend(
                _annotation_violation(rel, fn.returns, "返回注解", fn.lineno, bound)
            )
        return out


# ==== P3-2 低耦合 import ====


def _is_prefix_match(target: str, prefixes: tuple[str, ...]) -> bool:
    return any(target == p or target.startswith(p + ".") for p in prefixes)


def _within(own: str, target: str) -> bool:
    """target 是否落在自身 worker 包内（自身包内的 schema 等模块合法）。"""
    return target == own or target.startswith(own + ".")


class P3Rule2CouplingImports(Rule):
    """低耦合 import 纪律（R24）：web/scripts 越界 import + 工序间互 import。"""

    rule_id = "P3-2"
    title = "低耦合 import 纪律（R24）"

    def check(self, repo_root: Path) -> list[Violation]:
        violations: list[Violation] = []
        for sub in ("web", "scripts"):
            base = repo_root / sub
            if not base.is_dir():
                continue  # web/ v0.2 出现（现在跳过记绿）；scripts/ 存在即真扫
            for path in sorted(base.rglob("*.py")):
                violations.extend(self._check_outside(repo_root, path, sub))
        for root in WORKER_ROOTS:
            base = repo_root / root
            if not base.is_dir():
                continue
            for run_path in sorted(base.rglob("run.py")):
                violations.extend(self._check_worker(repo_root, run_path))
        return violations

    def _check_outside(
        self, repo_root: Path, path: Path, sub: str
    ) -> list[Violation]:
        rel = rel_posix(repo_root, path)
        tree, error = parse_module(path)
        if tree is None:
            return [Violation(self.rule_id, rel, 0, f"无法解析（fail-closed）：{error}")]
        out: list[Violation] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_prefix_match(alias.name, FORBIDDEN_IMPORT_PREFIXES):
                        out.append(
                            Violation(
                                self.rule_id,
                                rel,
                                node.lineno,
                                f"{sub}/ 不得 import {alias.name}（R24：web 只能走"
                                "契约/引擎三接口，scripts/ 只放运维脚本）",
                            )
                        )
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if _is_prefix_match(node.module, FORBIDDEN_IMPORT_PREFIXES):
                    out.append(
                        Violation(
                            self.rule_id,
                            rel,
                            node.lineno,
                            f"{sub}/ 不得 from {node.module} import（R24：web 只能走"
                            "契约/引擎三接口，scripts/ 只放运维脚本）",
                        )
                    )
        return out

    def _check_worker(self, repo_root: Path, run_path: Path) -> list[Violation]:
        rel = rel_posix(repo_root, run_path)
        tree, error = parse_module(run_path)
        if tree is None:
            return [Violation(self.rule_id, rel, 0, f"无法解析（fail-closed）：{error}")]
        own_parts = run_path.relative_to(repo_root).with_suffix("").parts[:-1]
        own = ".".join(own_parts)
        out: list[Violation] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = alias.name
                    if (
                        _is_prefix_match(target, _WORKERS_NS_PREFIXES)
                        and not _within(own, target)
                    ):
                        out.append(self._worker_violation(rel, node.lineno, target))
            elif isinstance(node, ast.ImportFrom):
                target = _resolve_from(node, own_parts)
                if (
                    target
                    and _is_prefix_match(target, _WORKERS_NS_PREFIXES)
                    and not _within(own, target)
                ):
                    out.append(self._worker_violation(rel, node.lineno, target))
        return out

    @staticmethod
    def _worker_violation(rel: str, lineno: int, target: str) -> Violation:
        return Violation(
            "P3-2",
            rel,
            lineno,
            f"工序不得 import 其他 worker：{target}"
            "（R21：工序间耦合只许走链 YAML 的 steps，自身包内模块合法）",
        )


def _resolve_from(
    node: ast.ImportFrom, own_parts: tuple[str, ...]
) -> str | None:
    """把 ImportFrom 解析为绝对模块点路径（相对 import 按所在包回退）。"""
    if node.level == 0:
        return node.module
    drop = node.level - 1
    if drop >= len(own_parts):
        return None  # 相对层级越过包根，不可能指向 workers 命名空间
    base = own_parts[: len(own_parts) - drop]
    if node.module:
        return ".".join((*base, node.module))
    return ".".join(base)


# ==== P3-3 models.yaml 密钥 ====


class P3Rule3ModelsYamlSecrets(Rule):
    """models.yaml 密钥零直值（文件不存在 = 跳过记绿，T8 落地后生效）。"""

    rule_id = "P3-3"
    title = "models.yaml 密钥零直值"

    def check(self, repo_root: Path) -> list[Violation]:
        path = repo_root / _YAML_FILE
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8")
        try:
            root_node: Any = yaml.compose(text)
        except yaml.YAMLError as exc:
            return [
                Violation(
                    self.rule_id, _YAML_FILE, 0, f"YAML 解析失败（fail-closed）：{exc}"
                )
            ]
        if root_node is None:  # 空文件
            return []
        return list(_walk_yaml_secrets(root_node))


def _walk_yaml_secrets(node: Any) -> Iterator[Violation]:
    """走 YAML compose 节点树（带行号）：api_key 非 env: 前缀 + 任意密钥直值。"""
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode) and key_node.value == "api_key":
                value_text = (
                    value_node.value if isinstance(value_node, yaml.ScalarNode) else None
                )
                if not (
                    isinstance(value_text, str) and value_text.startswith(_ENV_PREFIX)
                ):
                    # api_key 违规即报（同一值不再重复报密钥直值）
                    yield Violation(
                        "P3-3",
                        _YAML_FILE,
                        key_node.start_mark.line + 1,
                        "api_key 值必须以 env: 前缀引用环境变量（详设 §7.1），不得直值",
                    )
                    yield from _walk_yaml_secrets(value_node)
                    continue
            if (
                isinstance(value_node, yaml.ScalarNode)
                and isinstance(value_node.value, str)
                and value_node.value.startswith(_SK_PREFIX)
            ):
                yield Violation(
                    "P3-3",
                    _YAML_FILE,
                    value_node.start_mark.line + 1,
                    "疑似密钥直值（sk- 前缀）--models.yaml 只许 env: 前缀引用",
                )
            yield from _walk_yaml_secrets(value_node)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _walk_yaml_secrets(item)


# ==== P3-4 桩泄漏 ====


class P3Rule4StubLeak(Rule):
    """桩泄漏（R12：桩只住 tests/；生产目录 Fake*/Stub* 前缀类零容忍）。"""

    rule_id = "P3-4"
    title = "桩泄漏（R12：桩只住 tests/）"

    def check(self, repo_root: Path) -> list[Violation]:
        violations: list[Violation] = []
        for sub in PROD_STUB_DIRS:
            base = repo_root / sub
            if not base.is_dir():
                continue  # web/ v0.2 才出现（现在跳过记绿）
            for path in sorted(base.rglob("*.py")):
                rel = rel_posix(repo_root, path)
                tree, error = parse_module(path)
                if tree is None:
                    violations.append(
                        Violation(self.rule_id, rel, 0, f"无法解析（fail-closed）：{error}")
                    )
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name.startswith(
                        STUB_CLASS_PREFIXES
                    ):
                        violations.append(
                            Violation(
                                self.rule_id,
                                rel,
                                node.lineno,
                                f"桩类泄漏：{node.name}（R12：桩只许住 tests/，"
                                "生产目录零桩）",
                            )
                        )
        return violations
