"""``python -m engine.lint`` 入口（scripts/check.sh 四绿之绿 1 的探针）。

退出码：0 = 全仓 0 违规；1 = 有违规（逐行打印明细）。
仓库根按本文件位置上溯两级（engine/lint/__main__.py -> 仓库根）取，
与 cwd 无关；main(repo_root) 可传参供测试复用。
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import RULES, run_all


def main(repo_root: Path | None = None) -> int:
    """扫描全仓并打印结果；返回进程退出码（0 绿 / 1 红）。"""
    root = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
    violations = run_all(root)
    rule_ids = "、".join(rule.rule_id for rule in RULES)
    if violations:
        print(f"engine.lint：{len(violations)} 处违规（规则 {rule_ids}）")
        for violation in violations:
            print(violation.format())
        return 1
    print(f"engine.lint：0 违规（规则 {rule_ids} 全绿）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
