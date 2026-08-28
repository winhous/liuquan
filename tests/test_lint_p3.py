"""P3 契约纪律（四条子规则）：一正一反测试（详设-v0.1 §9；规范 R6/R12/R21-R24）。

正 = 干净迷你仓库零违规（含目录不存在 = 规则跳过记绿）；
反 = 故意违规样本必须被拦（防校验器改坏，规范 R10 守门层）。
全部用 tmp_path 构造迷你仓库树，不污染真仓库。

注意：本文件自身在 P2 扫描对象内，样本中的密钥形态字面量只出现在
「非 sk- 前缀开头」的常量里（P2 的 sk- 判据是前缀匹配，样本字符串
以 models.yaml 的缩进/键名打头，不构成违规）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from engine.lint import RULES, run_all
from engine.lint.p3 import (
    P3Rule1WorkerSignature,
    P3Rule2CouplingImports,
    P3Rule3ModelsYamlSecrets,
    P3Rule4StubLeak,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ==== P3-1 工序签名 ====

GOOD_RUN = (
    "from engine.core.context import EngineContext\n"
    "from models.workers.demo import EchoInput, EchoOutput\n"
    "\n"
    "\n"
    "def run(inputs: EchoInput, ctx: EngineContext) -> EchoOutput:\n"
    "    return EchoOutput(text=inputs.text)\n"
)


def _worker_repo(tmp_path: Path, run_content: str, *, root: str = "engine/workers") -> Path:
    _write(tmp_path, f"{root}/demo/echo/run.py", run_content)
    return tmp_path


def test_p3_1_good_signature_passes(tmp_path: Path) -> None:
    repo = _worker_repo(tmp_path, GOOD_RUN)
    assert P3Rule1WorkerSignature().check(repo) == []


def test_p3_1_dir_absent_skips_green(tmp_path: Path) -> None:
    """engine/workers/ 不存在（T10 才建）= 规则跳过记绿。"""
    assert P3Rule1WorkerSignature().check(tmp_path) == []


def test_p3_1_bare_dict_return_flagged(tmp_path: Path) -> None:
    content = (
        "from engine.core.context import EngineContext\n"
        "from models.workers.demo import EchoInput\n"
        "\n"
        "def run(inputs: EchoInput, ctx: EngineContext) -> dict:\n"
        "    return {}\n"
    )
    repo = _worker_repo(tmp_path, content)
    violations = P3Rule1WorkerSignature().check(repo)
    assert len(violations) == 1
    assert "dict" in violations[0].message


def test_p3_1_missing_annotations_flagged(tmp_path: Path) -> None:
    """缺 inputs/ctx/返回注解（裸签名）-> 三处违规。"""
    repo = _worker_repo(tmp_path, "def run(inputs, ctx):\n    return None\n")
    violations = P3Rule1WorkerSignature().check(repo)
    assert len(violations) == 3


def test_p3_1_unresolvable_ctx_annotation_flagged(tmp_path: Path) -> None:
    """ctx 注解名未被本文件 import 绑定（宽松判据：模块可解析）-> 违规。"""
    content = (
        "from models.workers.demo import EchoInput, EchoOutput\n"
        "\n"
        "def run(inputs: EchoInput, ctx: EngineContext) -> EchoOutput:\n"
        "    return EchoOutput()\n"
    )
    repo = _worker_repo(tmp_path, content)
    violations = P3Rule1WorkerSignature().check(repo)
    assert len(violations) == 1
    assert "EngineContext" in violations[0].message


def test_p3_1_wrong_param_names_flagged(tmp_path: Path) -> None:
    content = (
        "from engine.core.context import EngineContext\n"
        "from models.workers.demo import EchoInput, EchoOutput\n"
        "\n"
        "def run(data: EchoInput, context: EngineContext) -> EchoOutput:\n"
        "    return EchoOutput()\n"
    )
    repo = _worker_repo(tmp_path, content)
    violations = P3Rule1WorkerSignature().check(repo)
    assert len(violations) == 1
    assert "inputs" in violations[0].message and "ctx" in violations[0].message


def test_p3_1_no_run_function_flagged(tmp_path: Path) -> None:
    repo = _worker_repo(tmp_path, "def helper() -> int:\n    return 1\n")
    violations = P3Rule1WorkerSignature().check(repo)
    assert len(violations) == 1
    assert "run" in violations[0].message


def test_p3_1_registry_workers_path_also_scanned(tmp_path: Path) -> None:
    """详设 §4.1/§11 工序目录写 engine/registry/workers/，与 §9/R24 的
    engine/workers/ 不一致——两处目录都扫，T10 定型后收敛。"""
    content = (
        "from engine.core.context import EngineContext\n"
        "from models.workers.crm import TranslateInput, TranslateOutput\n"
        "\n"
        "def run(inputs, ctx):\n"
        "    return {}\n"
    )
    repo = _worker_repo(tmp_path, content, root="engine/registry/workers")
    violations = P3Rule1WorkerSignature().check(repo)
    assert len(violations) == 3  # 缺三处注解


# ==== P3-2 低耦合 import ====

def test_p3_2_web_import_engine_core_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "web/page.py", "from engine.core.runner import execute\n")
    violations = P3Rule2CouplingImports().check(tmp_path)
    assert len(violations) == 1
    assert violations[0].file == "web/page.py"
    assert "engine.core" in violations[0].message


def test_p3_2_web_import_engine_workers_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "web/page.py", "import engine.workers.demo.echo.run\n")
    violations = P3Rule2CouplingImports().check(tmp_path)
    assert len(violations) == 1


def test_p3_2_scripts_import_engine_workers_flagged(tmp_path: Path) -> None:
    """scripts/ 存在要真扫（现在只有 .sh；.py 出现即受检）。"""
    _write(tmp_path, "scripts/ops.py", "from engine.workers.crm.translate import run\n")
    violations = P3Rule2CouplingImports().check(tmp_path)
    assert len(violations) == 1
    assert violations[0].file == "scripts/ops.py"


def test_p3_2_web_allowed_imports_pass(tmp_path: Path) -> None:
    """web 只能走契约（R21）：import models.contract 合法。"""
    _write(tmp_path, "web/page.py", "from models.contract.task import TaskProposal\n")
    assert P3Rule2CouplingImports().check(tmp_path) == []


def test_p3_2_dirs_absent_skip_green(tmp_path: Path) -> None:
    """web/ 不存在（v0.2 出现）、engine/workers/ 不存在（T10）= 跳过记绿。"""
    (tmp_path / "scripts").mkdir()  # scripts 存在但无 .py
    assert P3Rule2CouplingImports().check(tmp_path) == []


def test_p3_2_worker_imports_other_worker_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "engine/workers/demo/echo/run.py",
        "from engine.workers.crm.translate import run\n",
    )
    violations = P3Rule2CouplingImports().check(tmp_path)
    assert len(violations) == 1
    assert "engine/workers/demo/echo/run.py" == violations[0].file


def test_p3_2_worker_imports_own_modules_pass(tmp_path: Path) -> None:
    """自身 worker 包内的 schema 等合法（§11：run.py + schema.py 同包）。"""
    _write(
        tmp_path,
        "engine/workers/demo/echo/run.py",
        "from . import schema\n"
        "from .schema import EchoOutput\n"
        "import engine.workers.demo.echo.schema\n"
        "from models.workers.demo import EchoInput\n"
        "from engine.core.context import EngineContext\n",
    )
    assert P3Rule2CouplingImports().check(tmp_path) == []


def test_p3_2_worker_relative_parent_import_flagged(tmp_path: Path) -> None:
    """from ..xxx 跨出自身 worker 包（指向域包/兄弟工序）-> 违规。"""
    _write(
        tmp_path,
        "engine/workers/demo/echo/run.py",
        "from ..greeting import helper\n",
    )
    violations = P3Rule2CouplingImports().check(tmp_path)
    assert len(violations) == 1


# ==== P3-3 models.yaml 密钥 ====

def test_p3_3_models_yaml_absent_skips_green(tmp_path: Path) -> None:
    assert P3Rule3ModelsYamlSecrets().check(tmp_path) == []


def test_p3_3_env_prefix_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "models.yaml",
        "models:\n"
        "  default:\n"
        "    provider: deepseek\n"
        "    model: deepseek-chat\n"
        "    base_url: env:DEEPSEEK_BASE_URL\n"
        "    api_key: env:DEEPSEEK_API_KEY\n"
        "    timeout_s: 30\n",
    )
    assert P3Rule3ModelsYamlSecrets().check(tmp_path) == []


def test_p3_3_real_secret_value_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "models.yaml",
        "models:\n"
        "  default:\n"
        "    provider: deepseek\n"
        "    api_key: sk-realsecret123\n",
    )
    violations = P3Rule3ModelsYamlSecrets().check(tmp_path)
    assert len(violations) == 1
    assert violations[0].rule_id == "P3-3"
    assert violations[0].file == "models.yaml"


def test_p3_3_non_env_plain_value_flagged(tmp_path: Path) -> None:
    """api_key 直值不以 env: 前缀引用 -> 违规（即使长得不像密钥）。"""
    _write(
        tmp_path,
        "models.yaml",
        "models:\n"
        "  default:\n"
        "    provider: deepseek\n"
        "    api_key: plain-string\n",
    )
    violations = P3Rule3ModelsYamlSecrets().check(tmp_path)
    assert len(violations) == 1
    assert "env:" in violations[0].message


def test_p3_3_sk_direct_value_anywhere_flagged(tmp_path: Path) -> None:
    """任意字段出现 sk- 直值 -> 违规（兜底扫描，不限于 api_key 键）。"""
    _write(
        tmp_path,
        "models.yaml",
        "models:\n"
        "  default:\n"
        "    provider: deepseek\n"
        "    api_key: env:DEEPSEEK_API_KEY\n"
        "    fallback_key: sk-inline-leak\n",
    )
    violations = P3Rule3ModelsYamlSecrets().check(tmp_path)
    assert len(violations) == 1


def test_p3_3_malformed_yaml_fail_closed(tmp_path: Path) -> None:
    _write(tmp_path, "models.yaml", "models: [unclosed\n")
    violations = P3Rule3ModelsYamlSecrets().check(tmp_path)
    assert len(violations) == 1
    assert "解析" in violations[0].message


# ==== P3-4 桩泄漏 ====

def test_p3_4_fake_class_in_production_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "engine/leak.py", "class FakeAgent:\n    pass\n")
    violations = P3Rule4StubLeak().check(tmp_path)
    assert len(violations) == 1
    assert violations[0].rule_id == "P3-4"
    assert "FakeAgent" in violations[0].message


def test_p3_4_stub_class_in_models_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "models/leak.py", "class StubClient:\n    pass\n")
    violations = P3Rule4StubLeak().check(tmp_path)
    assert len(violations) == 1


def test_p3_4_web_stub_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "web/leak.py", "class StubRenderer:\n    pass\n")
    violations = P3Rule4StubLeak().check(tmp_path)
    assert len(violations) == 1


def test_p3_4_tests_exempt(tmp_path: Path) -> None:
    """桩只住 tests/（规范 R12：tests/ 豁免）。"""
    _write(tmp_path, "tests/conftest.py", "class FakeAgent:\n    pass\n")
    assert P3Rule4StubLeak().check(tmp_path) == []


def test_p3_4_clean_and_dirs_absent_pass(tmp_path: Path) -> None:
    for sub in ("engine", "models", "scripts"):
        (tmp_path / sub).mkdir()
    _write(tmp_path, "engine/ok.py", "class EchoRunner:\n    pass\n")
    assert P3Rule4StubLeak().check(tmp_path) == []


# ==== 聚合入口与真仓库 ====

def test_rules_registry_shape() -> None:
    """框架形态：每条规则一个类、统一 rule_id；P1（T11）插入不改框架。"""
    assert [r.rule_id for r in RULES] == ["P1", "P2", "P3-1", "P3-2", "P3-3", "P3-4"]


def test_run_all_real_repo_green() -> None:
    """lint 扫真仓库（含 lint 自身与测试文件）必须 0 违规。"""
    assert run_all(REPO_ROOT) == []


def test_main_real_repo_exit_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 入口对真仓库：0 违规 -> 退出码 0。"""
    from engine.lint.__main__ import main

    assert main(REPO_ROOT) == 0
    out = capsys.readouterr().out
    assert "0 违规" in out


def test_main_bad_repo_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI 入口对违规仓库：打印明细 -> 退出码 1（check.sh 据此判红）。"""
    from engine.lint.__main__ import main

    _write(tmp_path, "engine/bad.py", "class FakeAgent:\n    pass\n")
    assert main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "P3-4" in out
    assert "engine/bad.py" in out
    assert "FakeAgent" in out


def test_module_invocation_end_to_end() -> None:
    """python -m engine.lint 端到端（check.sh 绿 1 的同一探针）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "engine.lint"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "0 违规" in proc.stdout
