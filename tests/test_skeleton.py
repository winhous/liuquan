"""T1 骨架冒烟测试。

覆盖（详设-v0.1 §10/§11 的骨架期子集）：
- engine 包可导入（目录骨架就位）
- CLI --version 可用（console_scripts 入口指向 engine.cli:main）
- CLI 无命令时打印用法并退出 0（registry-check/run/resume/audit/verify
  由 v0.1 后续任务落地）

本文件是 scripts/check.sh 四绿之「绿 3 单测」的实体，
保证 pytest 真绿而非空跑（开发流程 ④）。
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_engine_package_importable() -> None:
    """骨架冒烟：engine 包存在且可导入。"""
    import engine

    assert engine is not None
    assert engine.__doc__ is not None


def test_cli_version_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI --version：打印程序名与版本号，退出码 0。"""
    from engine import cli

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "liuquan-engine" in out
    assert cli.__version__ in out


def test_cli_no_command_prints_usage_exit_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """无命令：打印用法（含 §10 规划命令名）、退出 0。"""
    from engine import cli

    rc = cli.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "用法" in out
    # §10 五个规划命令都应在用法里露脸（真实现由后续任务落地）
    for cmd in ("registry-check", "run", "resume", "audit", "verify"):
        assert cmd in out, f"用法中应提到规划命令 {cmd}"


def test_cli_module_invocation_end_to_end() -> None:
    """python -m engine.cli --version 端到端可用（console_scripts 同源）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "engine.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "liuquan-engine" in proc.stdout
