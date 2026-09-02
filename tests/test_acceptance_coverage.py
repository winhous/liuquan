"""B1 断言覆盖检查器正反测试（tests/test_acceptance_coverage.py）。

用 subprocess 调用 scripts/check_acceptance.py CLI，避免跨包 import 问题（P3-2 不限
tests/ 导入 scripts/，但 subprocess 更可靠且与 check.sh 调用方式一致）。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check_acceptance.py"
TESTS_DIR = REPO_ROOT / "tests"


def _run_checker(
    manifest_path: Path, tests_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """以 CLI 方式调用 check_acceptance.py，返回 CompletedProcess。"""
    cmd = [sys.executable, str(CHECKER), str(manifest_path)]
    if tests_dir is not None:
        cmd.append(str(tests_dir))
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)


def _build_full_manifest(
    overrides: dict[int, dict],
    fake_filename: str = "_b1_fake_acceptance.py",
) -> tuple[str, str]:
    """构造完整 A1-A69 manifest YAML，返回 (yaml_text, fake_filename)。

    overrides: {编号数字: 额外字段 dict}，用于覆盖默认值。
    """
    entries = []
    for i in range(1, 73):  # A1-A72（批 7 起含 A70-A72）
        aid = f"A{i}"
        if i in overrides:
            ov = overrides[i]
            fields = [f"  - id: {aid}"]
            for k, v in ov.items():
                fields.append(f"    {k}: {repr(v) if isinstance(v, str) else v}")
            entries.append("\n".join(fields))
        elif i in (47, 48, 51) or i >= 57:
            entries.append(
                f'  - id: {aid}\n'
                f'    version: {"v0.6" if i >= 57 else "v0.5"}\n'
                f'    status: planned\n'
                f'    plan: "后续补"\n    desc: "planned gap"'
            )
        elif i == 11:
            entries.append(
                f'  - id: {aid}\n    version: v0.1\n    status: manual\n    desc: "manual"'
            )
        else:
            entries.append(
                f'  - id: {aid}\n    version: v0.1\n    status: implemented\n'
                f'    test: "tests/{fake_filename}::test_b1_sample"\n    desc: "sample"'
            )
    return 'version: "1"\nassertions:\n' + "\n".join(entries) + "\n", fake_filename


@pytest.fixture()
def _fake_test_file():
    """在 tests/ 目录创建临时假测试文件，测试结束后清理。"""
    fake = TESTS_DIR / "_b1_fake_acceptance.py"
    fake.write_text("def test_b1_sample(): pass\n", encoding="utf-8")
    yield fake
    if fake.exists():
        fake.unlink()


@pytest.mark.version_acceptance
def test_positive_manifest_passes(_fake_test_file: Path) -> None:
    """正向：implemented 指向真实函数 + planned 带 plan + manual → 退出 0，无红。"""
    manifest = TESTS_DIR / "_b1_fake_manifest.yaml"
    content, fname = _build_full_manifest(overrides={})
    manifest.write_text(content, encoding="utf-8")
    try:
        result = _run_checker(manifest, TESTS_DIR)
        assert result.returncode == 0, (
            f"期望退出码 0，实际 {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
        assert "红: " not in result.stdout, f"不应有红:\n{result.stdout}"
    finally:
        if manifest.exists():
            manifest.unlink()


@pytest.mark.version_acceptance
def test_implemented_missing_func_fails() -> None:
    """反向 1：implemented 的 test 指向不存在函数 → 退出 1 / 有红。"""
    manifest = TESTS_DIR / "_b1_fake_manifest.yaml"
    content, _ = _build_full_manifest(
        overrides={1: {"version": "v0.1", "status": "implemented", "test": "_no_such_file.py::test_no_such_func", "desc": "missing"}}
    )
    manifest.write_text(content, encoding="utf-8")
    try:
        result = _run_checker(manifest, TESTS_DIR)
        assert result.returncode == 1, f"期望退出码 1，实际 {result.returncode}"
        assert "红" in result.stdout, f"应有红输出:\n{result.stdout}"
    finally:
        if manifest.exists():
            manifest.unlink()


@pytest.mark.version_acceptance
def test_missing_id_fails() -> None:
    """反向 2：缺 A 编号（缺 A56）→ 退出 1。"""
    # 构造缺 A56 的 manifest
    entries = []
    for i in range(1, 56):  # 只到 A55，缺 A56
        aid = f"A{i}"
        if i in (47, 48, 51):
            entries.append(
                f'  - id: {aid}\n    version: v0.5\n    status: planned\n'
                f'    plan: "后续补"\n    desc: "planned gap"'
            )
        elif i == 11:
            entries.append(
                f'  - id: {aid}\n    version: v0.1\n    status: manual\n    desc: "manual"'
            )
        else:
            entries.append(
                f'  - id: {aid}\n    version: v0.1\n    status: implemented\n'
                f'    test: "tests/_b1_fake_acceptance.py::test_b1_sample"\n    desc: "sample"'
            )
    manifest = TESTS_DIR / "_b1_fake_manifest.yaml"
    manifest.write_text('version: "1"\nassertions:\n' + "\n".join(entries) + "\n", encoding="utf-8")
    fake = TESTS_DIR / "_b1_fake_acceptance.py"
    fake.write_text("def test_b1_sample(): pass\n", encoding="utf-8")
    try:
        result = _run_checker(manifest, TESTS_DIR)
        assert result.returncode == 1, f"期望退出码 1（缺 A56），实际 {result.returncode}"
        assert "A56" in result.stdout, f"应提示缺 A56:\n{result.stdout}"
    finally:
        if manifest.exists():
            manifest.unlink()
        if fake.exists():
            fake.unlink()


@pytest.mark.version_acceptance
def test_planned_without_plan_fails() -> None:
    """反向 3：planned 无 plan 字段 → 退出 1 / 有红。"""
    manifest = TESTS_DIR / "_b1_fake_manifest.yaml"
    content, fname = _build_full_manifest(
        overrides={47: {"version": "v0.5", "status": "planned", "desc": "gap without plan"}}
    )
    manifest.write_text(content, encoding="utf-8")
    fake = TESTS_DIR / "_b1_fake_acceptance.py"
    fake.write_text("def test_b1_sample(): pass\n", encoding="utf-8")
    try:
        result = _run_checker(manifest, TESTS_DIR)
        assert result.returncode == 1, f"期望退出码 1（A47 planned 无 plan），实际 {result.returncode}"
        assert "红" in result.stdout, f"应有红输出:\n{result.stdout}"
        assert "A47" in result.stdout, f"应提示 A47:\n{result.stdout}"
    finally:
        if manifest.exists():
            manifest.unlink()
        if fake.exists():
            fake.unlink()
