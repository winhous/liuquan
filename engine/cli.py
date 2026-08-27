"""刘全引擎 CLI 入口（详设-v0.1 §10）。

五个规划命令 registry-check / run / resume / audit / verify 由 v0.1 后续任务
逐个落地；本文件 T1 骨架期仅提供：
- ``--version``：打印版本退出 0
- 无命令：打印用法退出 0

CLI 属引擎自身（engine/cli.py），不放 scripts/（开发规范 R24：scripts/ 只放
运维脚本、不得 import engine）。

退出码约定（scripts/check.sh 依赖此约定探测命令是否已实现）：
- 0  成功；1 业务失败（校验不合规/执行失败）；2 argparse usage 错
  （子命令未实现时即此态）；后续任务实现子命令时沿用本约定。
"""

from __future__ import annotations

import argparse
import sys

# 与 pyproject.toml 的 project.version 保持同步（v0.1 内均为 0.1.0）
__version__ = "0.1.0"

USAGE = """\
用法: liuquan-engine <命令> [参数]

v0.1 规划命令（详设 §10，后续任务落地）:
  registry-check       全册 YAML+Model 校验，打印工序/链/事件清单
  run <chain_id>       建任务入队并同步执行到终态（--input 传 JSON）
  resume <task_id>     从最后检查点续跑（崩溃/暂停后）
  audit <task_id>      查该任务全部 LLM 调用明细
  verify               lint + registry + 单测，四绿报告
"""


def build_parser() -> argparse.ArgumentParser:
    """构建参数解析器（骨架期无子命令，后续任务按 §10 逐个加）。"""
    parser = argparse.ArgumentParser(
        prog="liuquan-engine",
        description="刘全 AI 引擎 CLI（v0.1 地基）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（console_scripts ``liuquan-engine`` 指向此处）。

    骨架期行为：``--version`` 打印版本退出 0；无命令打印用法退出 0。
    未知参数交给 argparse 报错退出 2。
    """
    parser = build_parser()
    parser.parse_args(argv)
    # 骨架期无已实现命令：打印规划中的用法，正常退出
    print(USAGE, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
