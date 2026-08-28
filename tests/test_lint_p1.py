"""P1 业务词黑名单（详设-v0.1 §9；规范 R10 硬编码治理机器层）。

正（违规样本必拦，规范 R10 守门层防校验器改坏）：config/ 登记的专名
出现在扫描对象 .py 字符串常量 / .yaml 标量值必拦；基础词表词出现在
对象目录必拦；解析不了的扫描对象 fail-closed 必拦。
反（误杀防护）：词表外普通词放行、config/ 目录自身豁免、非对象目录
（engine/core/、tests/）不扫、扫描目录不存在跳过、真仓库 0 违规。

全部用 tmp_path 构造迷你仓库树，不污染真仓库。
注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：样本
内容只用普通业务词，不出现 URL / IPv4 形态 / 密钥前缀形态，否则 lint
扫真仓库时会把本测试文件自身报红。
"""

from __future__ import annotations

from pathlib import Path

from engine.lint.p1 import P1BusinessTermsRule
from engine.lint.rules import Violation

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _worker_repo(tmp_path: Path, config_yaml: str | None, run_py: str) -> Path:
    """engine/registry/workers/demo/echo/ 下 config/ + run.py 的迷你仓库。"""
    if config_yaml is not None:
        _write(
            tmp_path,
            "engine/registry/workers/demo/echo/config/biz.yaml",
            config_yaml,
        )
    _write(tmp_path, "engine/registry/workers/demo/echo/run.py", run_py)
    return tmp_path


def _assert_single(violations: list[Violation], term: str, *, line: int) -> Violation:
    """断言恰好一条 P1 违规且词条/行号匹配；返回该违规供继续断言。"""
    assert len(violations) == 1
    v = violations[0]
    assert isinstance(v, Violation)
    assert v.rule_id == "P1"
    assert v.line == line
    assert term in v.message
    return v


# ---- 正：违规样本必须被拦（R10 守门层）----

def test_p1_config_value_in_run_py_flagged(tmp_path: Path) -> None:
    """config/ 登记的专名出现在扫描对象 run.py 字符串常量 -> 拦。"""
    config = "fields:\n  - 飞书列名\n  - 店铺名\n"
    run = '"""demo 工序。"""\n\n\ndef run() -> None:\n    label = "飞书列名"\n    print(label)\n'
    repo = _worker_repo(tmp_path, config, run)
    violations = P1BusinessTermsRule().check(repo)
    assert len(violations) == 1
    v = violations[0]
    assert isinstance(v, Violation)
    assert v.rule_id == "P1"
    assert v.file == "engine/registry/workers/demo/echo/run.py"
    assert v.line == 5
    assert "飞书列名" in v.message


def test_p1_config_numeric_spec_in_run_py_flagged(tmp_path: Path) -> None:
    """数量规格（详设 §9 样例「13 个标签」）字面量出现在 run.py -> 拦。"""
    config = "tags_count: 13\n"
    run = 'SPEC = "13 个标签"  # 数量规格必须住 config/\n'
    repo = _worker_repo(tmp_path, config, run)
    _assert_single(P1BusinessTermsRule().check(repo), "13", line=1)


def test_p1_config_key_in_run_py_flagged(tmp_path: Path) -> None:
    """config/ 的键也入自动词表：键名出现在 run.py 字符串常量 -> 拦。"""
    config = "tags_count: 13\n"
    run = 'MSG = "tags_count 已外置到配置"\n'
    repo = _worker_repo(tmp_path, config, run)
    violations = P1BusinessTermsRule().check(repo)
    assert len(violations) == 1
    assert "tags_count" in violations[0].message


def test_p1_config_value_in_yaml_scalar_flagged(tmp_path: Path) -> None:
    """config/ 登记的专名出现在扫描对象 .yaml 标量值 -> 拦（带行号）。"""
    config = "label: 飞书列名\n"
    _write(tmp_path, "engine/registry/workers/demo/echo/config/biz.yaml", config)
    _write(
        tmp_path,
        "engine/registry/chains/demo/chain.yaml",
        "name: 飞书列名清单\nsteps: []\n",
    )
    violations = P1BusinessTermsRule().check(tmp_path)
    assert len(violations) == 1
    v = violations[0]
    assert v.rule_id == "P1"
    assert v.file == "engine/registry/chains/demo/chain.yaml"
    assert v.line == 1
    assert "飞书列名" in v.message


def test_p1_base_term_in_scan_object_flagged(tmp_path: Path) -> None:
    """基础词表词出现在对象目录（无 config/，自动词表为空）-> 拦。"""
    repo = _worker_repo(
        tmp_path,
        None,
        '"""本工序处理店铺名。"""\n\nVALUE = "普通值"\n',
    )
    rule = P1BusinessTermsRule(base_terms=("店铺名",))
    violations = rule.check(repo)
    assert len(violations) == 1
    v = violations[0]
    assert v.rule_id == "P1"
    assert v.line == 1  # docstring 属字符串常量，同样被扫（与 P2 同口径）
    assert "店铺名" in v.message


def test_p1_base_term_union_with_auto_table(tmp_path: Path) -> None:
    """词表 = 基础 ∪ 自动：两类词同时命中 -> 各自一条违规。"""
    config = "label: 飞书列名\n"
    run = 'A = "飞书列名"\nB = "店铺名"\n'
    repo = _worker_repo(tmp_path, config, run)
    rule = P1BusinessTermsRule(base_terms=("店铺名",))
    violations = rule.check(repo)
    assert len(violations) == 2
    messages = " ".join(v.message for v in violations)
    assert "飞书列名" in messages
    assert "店铺名" in messages


def test_p1_unparseable_scanned_py_fail_closed(tmp_path: Path) -> None:
    """扫描对象 .py 语法坏 -> 按违规上报（解析不了就无法证明干净）。"""
    repo = _worker_repo(tmp_path, None, "def broken(:\n")
    violations = P1BusinessTermsRule().check(repo)
    assert len(violations) == 1
    assert "解析" in violations[0].message


def test_p1_unparseable_scanned_yaml_fail_closed(tmp_path: Path) -> None:
    """扫描对象 .yaml 解析失败 -> 按违规上报。"""
    _write(tmp_path, "engine/registry/chains/demo/chain.yaml", "a: [unclosed\n")
    violations = P1BusinessTermsRule().check(tmp_path)
    assert len(violations) == 1
    assert "解析失败" in violations[0].message
    assert "chain.yaml" in violations[0].file


def test_p1_unparseable_config_yaml_fail_closed(tmp_path: Path) -> None:
    """config/ yaml 解析失败 -> 词表不完整，按违规上报。"""
    repo = _worker_repo(tmp_path, "a: [unclosed\n", "MSG = 'x'\n")
    violations = P1BusinessTermsRule().check(repo)
    assert len(violations) == 1
    assert "config/" in violations[0].message
    assert violations[0].file.endswith("config/biz.yaml")


# ---- 反：误杀防护（合法形态不拦）----

def test_p1_unknown_words_pass(tmp_path: Path) -> None:
    """词表外普通词放行。"""
    config = "fields:\n  - 飞书列名\n"
    run = 'TITLE = "普通提示语"\nNAME = "echo"\n'
    repo = _worker_repo(tmp_path, config, run)
    assert P1BusinessTermsRule().check(repo) == []


def test_p1_config_dir_self_exempt(tmp_path: Path) -> None:
    """config/ 目录自身豁免：业务值合法住处，登记词不因自身而红。"""
    config = "fields:\n  - 飞书列名\n  - 店铺名\n"
    run = 'VALUE = "x"\n'
    repo = _worker_repo(tmp_path, config, run)
    assert P1BusinessTermsRule().check(repo) == []


def test_p1_non_scan_roots_not_scanned(tmp_path: Path) -> None:
    """非对象目录不扫：engine/core/、tests/ 出现词表词 -> 不拦。"""
    _write(tmp_path, "engine/core/bad.py", 'X = "飞书列名"\n')
    _write(tmp_path, "tests/bad.py", 'X = "飞书列名"\n')
    rule = P1BusinessTermsRule(base_terms=("飞书列名",))
    assert rule.check(tmp_path) == []


def test_p1_scan_root_absent_passes(tmp_path: Path) -> None:
    """engine/workers/ 与 engine/registry/ 都不存在 -> 跳过记绿。"""
    assert P1BusinessTermsRule().check(tmp_path) == []


def test_p1_auto_table_empty_without_config(tmp_path: Path) -> None:
    """无 config/ 目录 + 空基础词表 -> 自动词表为空，任何业务词不拦。"""
    repo = _worker_repo(tmp_path, None, 'X = "飞书列名"\n')
    assert P1BusinessTermsRule().check(repo) == []


def test_p1_short_scalar_value_filtered(tmp_path: Path) -> None:
    """自动词表过滤长度 < 2 的值（防单字符噪声），单数字不产生词。"""
    config = "retry: 1\n"
    run = 'MSG = "重试 1 次"\n'
    repo = _worker_repo(tmp_path, config, run)
    assert P1BusinessTermsRule().check(repo) == []


# ---- 真仓库：本规则扫真仓库（含 engine/registry/__init__.py）必须全绿 ----

def test_p1_real_repo_green() -> None:
    assert P1BusinessTermsRule().check(REPO_ROOT) == []


def test_p1_run_all_real_repo_green() -> None:
    from engine.lint import run_all

    assert run_all(REPO_ROOT) == []
