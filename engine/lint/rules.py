"""lint 规则框架（详设-v0.1 §9）：Rule 统一接口 + Violation + 全仓扫描助手。

框架形态（为 T11 的 P1 业务词黑名单留位，插入新规则不改框架）：
- 每条规则一个类（p2.py / p3.py / 未来的 p1.py），实现
  ``Rule.check(repo_root) -> list[Violation]``
- 主入口（本包 ``__init__.py`` 的 RULES 元组）聚合注册，
  新增规则 = 新文件 + 注册一行
- 「对象与豁免写死在规则配置里」（详设 §9 原文）：各规则模块顶部的
  常量元组即规则配置，豁免调整 = 改配置 + 变更日志留痕

公共助手约定：
- ``iter_py_files``：全仓 .py 收集（隐藏目录与缓存/打包产物剪枝，
  规则对象内的豁免子树经 skip_subtrees 排除）
- ``parse_module``：AST 解析；语法坏/读不了的文件 fail-closed
  （返回错误摘要，调用方按违规上报--解析不了就无法证明干净）
- ``rel_posix``：相对仓库根的 posix 路径（Violation.file 的口径）
"""

from __future__ import annotations

import ast
import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Violation:
    """一条违规：file 为相对仓库根的 posix 路径；line 为 1 起始行号（文件级违规 = 0）。"""

    rule_id: str
    file: str
    line: int
    message: str

    def format(self) -> str:
        if self.line > 0:
            return f"[{self.rule_id}] {self.file}:{self.line} {self.message}"
        return f"[{self.rule_id}] {self.file} {self.message}"


class Rule(ABC):
    """规则统一接口：rule_id/title 为类属性，check 扫描 repo_root 返回违规清单。

    目录/文件不存在的规则对象 = 跳过记绿（返回空表），对应任务落地后自然生效
    （详设 §9 P3 的 v0.1 过渡态）。
    """

    rule_id: str
    title: str

    @abstractmethod
    def check(self, repo_root: Path) -> list[Violation]:
        """扫描 repo_root，干净 = 空表。"""


# ---- 全仓 .py 收集的剪枝配置（「扫全仓」= 仓库自身源码；环境/缓存产物不进）----
# v0.5：加 vendor（第三方库目录 XHS-Downloader 186MB 不入库但部署时落盘，非刘全源码）
_PRUNED_DIR_NAMES = frozenset(
    {"__pycache__", "node_modules", "dist", "build", ".pgdata", "vendor"}
)
_PRUNED_DIR_SUFFIXES = (".egg-info",)


def iter_py_files(
    repo_root: Path, *, skip_subtrees: tuple[str, ...] = ()
) -> Iterator[Path]:
    """递归收集 repo_root 下全部 .py。

    剪枝：隐藏目录（.git/.venv/.pytest_cache/.claude 等）、__pycache__、
    node_modules、dist、build、*.egg-info、.pgdata、vendor（第三方库）；
    豁免：skip_subtrees 指定的子树（相对 repo_root 的 posix 路径，
    如 "engine/core/llm"）整体不进扫描对象。
    """
    skip_paths = [repo_root.joinpath(part) for part in skip_subtrees]
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".")
            and name not in _PRUNED_DIR_NAMES
            and not name.endswith(_PRUNED_DIR_SUFFIXES)
        )
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            if any(path == skip or skip in path.parents for skip in skip_paths):
                continue
            yield path


def parse_module(path: Path) -> tuple[ast.Module | None, str | None]:
    """解析 .py 为 AST；失败返回 (None, 错误摘要) 由调用方 fail-closed。"""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"读取失败：{exc}"
    try:
        return ast.parse(source, filename=str(path)), None
    except SyntaxError as exc:
        return None, f"语法错误：{exc.msg}（行 {exc.lineno}）"


def rel_posix(repo_root: Path, path: Path) -> str:
    """path 相对 repo_root 的 posix 路径（Violation.file 口径）。"""
    return path.relative_to(repo_root).as_posix()
