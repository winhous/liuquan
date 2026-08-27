"""P2 凭据端点零容忍：一正一反测试（详设-v0.1 §9；规范 R10 机器层）。

正 = 干净迷你仓库零违规；反 = 故意违规样本必须被拦（防校验器改坏，规范 R10 守门层）。
全部用 tmp_path 构造迷你仓库树，不污染真仓库。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
样本中的违规字面量一律运行期拼接构造，保证本文件的任何单一字符串常量
不含完整 URL scheme / IPv4 四段形态 / sk- 前缀，否则 lint 扫真仓库时会
把测试文件自身报红。
"""

from __future__ import annotations

from pathlib import Path

from engine.lint.p2 import P2CredentialsRule
from engine.lint.rules import Violation

REPO_ROOT = Path(__file__).resolve().parents[1]


def _mini_repo(tmp_path: Path) -> Path:
    """迷你仓库骨架（engine/ models/ tests/ 三个源目录）。"""
    for sub in ("engine", "models", "tests"):
        (tmp_path / sub).mkdir()
    return tmp_path


# ---- 违规样本构造（运行期拼接，见模块 docstring）----

def _url_sample() -> str:
    scheme = "ht" + "tps://"
    return f'BASE = "{scheme}api.example.com/v1"\n'


def _ip_sample() -> str:
    octets = ".".join(("34", "228", "44", "95"))
    return f'HOST = "{octets}"\n'


def _sk_sample() -> str:
    prefix = "s" + "k-"
    return f'KEY = "{prefix}0123456789abcdef"\n'


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---- 正向：干净树通过 ----

def test_p2_clean_tree_passes(tmp_path: Path) -> None:
    repo = _mini_repo(tmp_path)
    _write(repo, "engine/ok.py", '"""正常模块。"""\n\nNAME = "liuquan"\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n')
    assert P2CredentialsRule().check(repo) == []


def test_p2_empty_repo_passes(tmp_path: Path) -> None:
    assert P2CredentialsRule().check(tmp_path) == []


# ---- 反向：各类违规必须被拦 ----

def test_p2_url_literal_flagged(tmp_path: Path) -> None:
    repo = _mini_repo(tmp_path)
    _write(repo, "engine/bad.py", _url_sample())
    violations = P2CredentialsRule().check(repo)
    assert len(violations) == 1
    v = violations[0]
    assert isinstance(v, Violation)
    assert v.rule_id == "P2"
    assert v.file == "engine/bad.py"
    assert v.line == 1
    assert "URL" in v.message


def test_p2_http_scheme_and_docstring_also_flagged(tmp_path: Path) -> None:
    """http 冒双斜杠（另一 scheme）与文档字符串里的 URL 同样被拦。"""
    repo = _mini_repo(tmp_path)
    scheme = "ht" + "tp://"
    content = f'"""文档：详见 {scheme}example.com 首页。"""\n\nHELP = "{scheme}docs.example.com"\n'
    _write(repo, "engine/bad.py", content)
    violations = P2CredentialsRule().check(repo)
    assert len(violations) == 2  # docstring 常量 + HELP 常量
    assert all(v.rule_id == "P2" for v in violations)


def test_p2_ip_literal_flagged(tmp_path: Path) -> None:
    repo = _mini_repo(tmp_path)
    _write(repo, "engine/bad.py", _ip_sample())
    violations = P2CredentialsRule().check(repo)
    assert len(violations) == 1
    assert "IPv4" in violations[0].message
    assert violations[0].file == "engine/bad.py"


def test_p2_ip_embedded_in_string_flagged(tmp_path: Path) -> None:
    """字符串中段含 IPv4 形态（连接串）也算。"""
    repo = _mini_repo(tmp_path)
    octets = ".".join(("112", "124", "33", "142"))
    _write(repo, "engine/bad.py", f'PG = "host={octets} port=5432"\n')
    violations = P2CredentialsRule().check(repo)
    assert len(violations) == 1
    assert "IPv4" in violations[0].message


def test_p2_sk_prefix_flagged(tmp_path: Path) -> None:
    repo = _mini_repo(tmp_path)
    _write(repo, "engine/bad.py", _sk_sample())
    violations = P2CredentialsRule().check(repo)
    assert len(violations) == 1
    assert "密钥" in violations[0].message


def test_p2_sensitive_name_literal_assignment_flagged(tmp_path: Path) -> None:
    """api_key/password/token 名目标赋非空字面量 -> 违规（含属性与下标目标）。"""
    repo = _mini_repo(tmp_path)
    _write(
        repo,
        "engine/bad.py",
        'API_KEY = "raw-secret-1"\n'
        'DEEPSEEK_API_KEY = "raw-secret-2"\n'
        'db_password = "raw-secret-3"\n'
        'class C:\n'
        '    def __init__(self) -> None:\n'
        '        self.auth_token = "raw-secret-4"\n'
        '        self.cfg = {}\n'
        '        self.cfg["password"] = "raw-secret-5"\n',
    )
    violations = P2CredentialsRule().check(repo)
    assert len(violations) == 5
    lines = sorted(v.line for v in violations)
    assert lines == [1, 2, 3, 6, 8]


def test_p2_os_environ_flagged(tmp_path: Path) -> None:
    repo = _mini_repo(tmp_path)
    _write(repo, "engine/bad.py", 'import os\n\n\ndef load() -> str:\n    return os.environ["DEEPSEEK_API_KEY"]\n')
    violations = P2CredentialsRule().check(repo)
    assert len(violations) == 1
    assert "环境变量" in violations[0].message


def test_p2_os_getenv_flagged(tmp_path: Path) -> None:
    repo = _mini_repo(tmp_path)
    _write(repo, "engine/bad.py", 'import os\n\n\ndef load() -> str:\n    return os.getenv("DEEPSEEK_API_KEY", "")\n')
    violations = P2CredentialsRule().check(repo)
    assert len(violations) == 1
    assert "环境变量" in violations[0].message


def test_p2_from_os_import_environ_flagged(tmp_path: Path) -> None:
    """绕路形态：from os import environ 后直接用名字。"""
    repo = _mini_repo(tmp_path)
    _write(repo, "engine/bad.py", 'from os import environ\n\nVALUE = environ["X"]\n')
    violations = P2CredentialsRule().check(repo)
    assert len(violations) == 1
    assert "环境变量" in violations[0].message


def test_p2_unparseable_file_fail_closed(tmp_path: Path) -> None:
    """语法坏文件按违规上报（fail-closed：解析不了就无法证明干净）。"""
    repo = _mini_repo(tmp_path)
    _write(repo, "engine/broken.py", "def broken(:\n")
    violations = P2CredentialsRule().check(repo)
    assert len(violations) == 1
    assert "解析" in violations[0].message


# ---- 误杀防护：合法形态不拦 ----

def test_p2_param_passing_not_killed(tmp_path: Path) -> None:
    """参数声明与传参（变量）不是赋字面量，不拦（详设 §9：只拦赋值字面量）。"""
    repo = _mini_repo(tmp_path)
    _write(
        repo,
        "engine/ok.py",
        'def call(api_key: str, token: str) -> None:\n'
        '    print(api_key, token)\n'
        '\n'
        'key_from_db = "placeholder"\n'
        'call(api_key=key_from_db, token=key_from_db)\n',
    )
    assert P2CredentialsRule().check(repo) == []


def test_p2_none_placeholder_not_killed(tmp_path: Path) -> None:
    """None / 空串占位不是泄漏，不拦。"""
    repo = _mini_repo(tmp_path)
    _write(repo, "engine/ok.py", 'current_token = None\npassword = ""\n')
    assert P2CredentialsRule().check(repo) == []


def test_p2_exempt_subtrees_not_scanned(tmp_path: Path) -> None:
    """豁免三子树（详设 §9 写死在规则配置）：engine/core/llm/、tests/fixtures/、scripts/。"""
    repo = _mini_repo(tmp_path)
    for rel in ("engine/core/llm", "tests/fixtures", "scripts"):
        _write(repo, f"{rel}/bad.py", _url_sample() + _ip_sample() + _sk_sample())
    assert P2CredentialsRule().check(repo) == []


# ---- 真仓库：lint 扫自己（含本测试文件）必须全绿 ----

def test_p2_real_repo_green() -> None:
    assert P2CredentialsRule().check(REPO_ROOT) == []
