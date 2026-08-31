"""T12b CLI 五命令测试（详设-v0.1 §10；engine/cli.py）。

覆盖：
- registry-check：真仓库空册 0 违规退出 0 + 清单为空 + 无 [L4]；tmp 仓库反向
  （models.yaml 缺必填/缺失 -> 退出 1 打印违规）；--write-hashes 落 L9 登记文件
- run / resume：TaskRunner 用测试桩注入（monkeypatch cli._runner_module），
  断言参数传递（chain_id / input JSON / trigger 缺省）与输出格式
  （task e-000042 created / [INIT] 相位行 / [DONE] 或 [FAILED]）与退出码；
  真 runner 端到端由 T12a 的 test_runner/test_acceptance 覆盖
- audit：嵌入式 PG（engine_pg_cluster）直插审计后跑命令，断言摘要格式与退出码
- verify：monkeypatch cli._run_cmd（不真跑 pytest 嵌套），断言绿/红报告与退出码
- --version / 无命令用法 / 坏 --input JSON / 未知命令的退出码

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- 违规样本字面量（sk- 形态等）一律运行期拼接，任何单一字符串常量不得含
  URL scheme / 完整 IPv4 / sk- 前缀（判据见 engine/lint/p2.py 模块 docstring）
- 不读 os.environ（P2 规则4）：环境变量只经 monkeypatch.setenv 写入；
  连接串来自嵌入式 PG fixture 的运行期值，不写进本文件
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engine import cli
from engine.core import db

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_MODELS_YAML = (
    "models:\n"
    "  default:\n"
    "    provider: deepseek\n"
    "    model: deepseek-chat\n"
    "    base_url: env:DEEPSEEK_BASE_URL\n"
    "    api_key: env:DEEPSEEK_API_KEY\n"
    "    timeout_s: 30\n"
    "    reask_limit: 2\n"
)


# ---- 通用助手 ----


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """models.yaml 的 env: 引用所需环境变量（R20：值只进测试环境变量）。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "local-test-endpoint")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")


def _bad_models_yaml() -> str:
    """api_key 直值形态（运行期拼接构造，防 P2 自检红）。"""
    raw = "s" + "k-secret"
    return VALID_MODELS_YAML.replace("api_key: env:DEEPSEEK_API_KEY", f"api_key: {raw}")


def _make_tmp_repo(tmp_path: Path, *, models_yaml: str | None) -> Path:
    """迷你仓库：engine/ 骨架 + 可选 models.yaml（registry-check 的 repo 形态）。"""
    (tmp_path / "engine").mkdir()
    if models_yaml is not None:
        (tmp_path / "models.yaml").write_text(models_yaml, encoding="utf-8")
    return tmp_path


# ---- registry-check ----


class TestRegistryCheck:
    def test_real_repo_registry_check_exit_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """真仓库（T10 + v0.2 T4 后：3 工序 3 链 1 provider 1 事件 2 Action）-> 0 违规退出 0。"""
        _set_env(monkeypatch)
        rc = cli.main(["registry-check"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[L4]" not in out
        assert "0 违规" in out
        # 清单随声明：v0.4 批 3b 后工序 9（+crm_follow_up_reminder）、链 7（+crm_reminder_chain）、
        # provider 4（+crm_overdue_context）、事件 1、Action 4（批 4 +tm.schedule）
        for label in ("工序 9", "链 7", "Context provider 4", "事件 1", "Action 4"):
            assert label in out, f"清单应包含 {label}"
        assert "demo_echo" in out and "crm_translate" in out and "demo_propose" in out
        assert "chat_translate" in out and "tm_intent" in out
        assert "crm_chat_chain" in out and "tm_intent_chain" in out
        assert "tm_demo_chain" in out and "tm.proposal" in out
        assert "crm.candidate" in out

    def test_missing_models_yaml_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """反向：models.yaml 缺失 -> [L4] 拒载，退出 1 并打印违规行。"""
        _make_tmp_repo(tmp_path, models_yaml=None)
        monkeypatch.chdir(tmp_path)
        rc = cli.main(["registry-check"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "[L4]" in out
        assert "models.yaml" in out

    def test_bad_models_yaml_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """反向：models.yaml 密钥直值（R20）-> [L4] 拒载，退出 1。"""
        _make_tmp_repo(tmp_path, models_yaml=_bad_models_yaml())
        monkeypatch.chdir(tmp_path)
        rc = cli.main(["registry-check"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "[L4]" in out

    def test_write_hashes_writes_l9_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--write-hashes：空册 -> 写 model_hashes.yaml（L9 登记，空 models 映射）退出 0。"""
        _set_env(monkeypatch)
        _make_tmp_repo(tmp_path, models_yaml=VALID_MODELS_YAML)
        monkeypatch.chdir(tmp_path)
        rc = cli.main(["registry-check", "--write-hashes"])
        out = capsys.readouterr().out
        assert rc == 0
        hashes = tmp_path / "engine" / "registry" / "model_hashes.yaml"
        assert hashes.is_file()
        assert "model_hashes.yaml" in out
        assert "models:" in hashes.read_text(encoding="utf-8")


# ---- run / resume：TaskRunner 桩注入（真 runner 由 T12a 覆盖）----


class _FakePhaseLine:
    def __init__(self, phase: str, message: str) -> None:
        self.phase = phase
        self.message = message


class _FakeTaskResult:
    def __init__(self, task_id: int, chain_id: str, status: str, error: str | None, phase_lines) -> None:
        self.task_id = task_id
        self.chain_id = chain_id
        self.status = status
        self.error = error
        self.phase_lines = phase_lines


class _FakeTaskRunner:
    """桩 runner：记录调用参数，按场景返回固定终态（§13 桩注入式，CLI 对桩零感知）。"""

    def __init__(
        self,
        engine,
        registry,
        *,
        agent_factory,
        audit_gate=None,
        writable_check=None,
        model_registry=None,
        repo_root=None,
    ) -> None:
        self.engine = engine
        self.registry = registry
        self.agent_factory = agent_factory
        self.audit_gate = audit_gate
        self.writable_check = writable_check
        self.model_registry = model_registry  # review 修复后 CLI 注入（C1）
        self.repo_root = repo_root
        self.calls: list = []
        self.resume_id: int | None = None

    async def run(self, chain_id, input_, *, trigger_type="manual", trigger_ref=None):
        self.calls.append(("run", chain_id, input_, trigger_type, trigger_ref))
        return _FakeTaskResult(
            task_id=42,
            chain_id=chain_id,
            status="done",
            error=None,
            phase_lines=[_FakePhaseLine("INIT", "policy ok (domain=demo, risk=read)")],
        )

    async def resume(self, task_id: int):
        self.resume_id = task_id
        return _FakeTaskResult(
            task_id=task_id,
            chain_id="demo-echo",
            status="failed",
            error="LLM 调用失败",
            phase_lines=[],
        )


class _FakeRunnerModule:
    def __init__(self) -> None:
        self.last_runner: _FakeTaskRunner | None = None

    def instantiate(self, engine, registry, **kwargs) -> _FakeTaskRunner:
        self.last_runner = _FakeTaskRunner(engine, registry, **kwargs)
        return self.last_runner


class _FakeEngine:
    """桩引擎：run/resume 接线测试不碰真 DB（_probe_db 同被桩掉）。"""

    async def dispose(self) -> None:
        return None


async def _noop_probe(engine) -> None:
    return None


def _wire_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeRunnerModule:
    """把 run/resume 依赖全部换成桩（loader 仍走真校验，用 tmp 空册仓库）。"""
    _set_env(monkeypatch)
    _make_tmp_repo(tmp_path, models_yaml=VALID_MODELS_YAML)
    monkeypatch.chdir(tmp_path)
    fake_mod = _FakeRunnerModule()
    monkeypatch.setattr(cli, "_runner_module", lambda: SimpleNamespace(TaskRunner=fake_mod.instantiate))
    monkeypatch.setattr(cli, "create_engine", lambda *a, **k: _FakeEngine())
    monkeypatch.setattr(cli, "_probe_db", _noop_probe)
    return fake_mod


class TestRunResumeWiring:
    def test_run_passes_args_and_prints_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = _wire_fakes(monkeypatch, tmp_path)
        rc = cli.main(["run", "demo-echo", "--input", '{"text": "hello"}'])
        out = capsys.readouterr().out
        assert rc == 0
        assert fake.last_runner is not None
        assert fake.last_runner.calls == [("run", "demo-echo", {"text": "hello"}, "manual", None)]
        assert "task e-000042 created, chain: demo-echo" in out
        assert "[INIT] policy ok (domain=demo, risk=read)" in out
        assert "[DONE]" in out

    def test_run_bad_input_json_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """坏 --input JSON：退出 1（数据错误属业务失败；usage 错才是 2），带清晰提示。"""
        _set_env(monkeypatch)
        _make_tmp_repo(tmp_path, models_yaml=VALID_MODELS_YAML)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cli, "create_engine", lambda *a, **k: _FakeEngine())
        rc = cli.main(["run", "demo-echo", "--input", "not-json"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "JSON" in err

    def test_run_input_must_be_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--input 是 JSON 但非对象：退出 1。"""
        _set_env(monkeypatch)
        _make_tmp_repo(tmp_path, models_yaml=VALID_MODELS_YAML)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cli, "create_engine", lambda *a, **k: _FakeEngine())
        rc = cli.main(["run", "demo-echo", "--input", "[1, 2]"])
        assert rc == 1
        assert "对象" in capsys.readouterr().err

    def test_resume_passes_task_id_and_prints_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = _wire_fakes(monkeypatch, tmp_path)
        rc = cli.main(["resume", "7"])
        out = capsys.readouterr().out
        assert rc == 1
        assert fake.last_runner is not None
        assert fake.last_runner.resume_id == 7
        assert "task e-000007 resumed, chain: demo-echo" in out
        assert "[FAILED] LLM 调用失败" in out

    def test_run_missing_dotenv_clear_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """create_engine 缺 .env（未注入桩）：退出 1 带清晰提示（R20 指向 .env）。"""
        _set_env(monkeypatch)
        _make_tmp_repo(tmp_path, models_yaml=VALID_MODELS_YAML)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cli, "_runner_module", lambda: SimpleNamespace(TaskRunner=object))
        monkeypatch.setattr(cli, "_probe_db", _noop_probe)
        # 不桩 create_engine：真实现读仓库根 .env；把 _DOTENV_PATH 指到不存在的
        # 文件，与真实仓库 .env 无关（本机开发已有 .env，测试必须自足）
        monkeypatch.setattr("engine.core.db._DOTENV_PATH", tmp_path / "no-such.env")
        rc = cli.main(["run", "demo-echo", "--input", "{}"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "LIUQUAN_ENGINE_DB_URL" in err
        assert ".env" in err


# ---- audit：嵌入式 PG 直插后跑命令 ----


async def _insert_audit_row(url: str, *, chain_id: str = "demo-echo") -> int:
    """建任务 + 工序实例 + 写一条 ok 审计（engine_audit 的 step_id 有 FK 约束）。

    每条调用一个独立引擎与事件循环，防 loop 绑定问题。
    """
    engine = create_async_engine(url)
    try:
        task_id = await db.create_task(
            engine, chain_id=chain_id, trigger_type="manual", trigger_ref=None, input_={}
        )
        step_id = await db.create_step(
            engine, task_id, step_index=0, worker_id="demo.echo", input_={}
        )
        await db.append_audit(
            engine,
            task_id=task_id,
            step_id=step_id,
            worker_id="demo.echo",
            model="deepseek-chat",
            attempt=0,
            result="ok",
            input_full={"text": "hello"},
            output_full={"text": "hi"},
            input_tokens=10,
            output_tokens=20,
            duration_ms=30,
        )
        return task_id
    finally:
        await engine.dispose()


class TestAuditCommand:
    @pytest.fixture(autouse=True)
    def _clean_shared_pg(self, engine_pg_cluster) -> None:
        """review major-1 修复：本类向共享 PG 簇插入任务/审计，每测后必须清表——
        否则残留的 queued 任务会被后续 test_runner/test_acceptance 的 SKIP LOCKED
        取走（顺序依赖 flaky：pytest tests/test_cli.py tests/test_runner.py 必红）。
        独立引擎 + 独立事件循环（asyncio.run），不与其他 loop 绑定。"""
        yield
        asyncio.run(_truncate_all(engine_pg_cluster.url))

    def test_audit_prints_summary_rows(
        self, engine_pg_cluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        task_id = asyncio.run(_insert_audit_row(engine_pg_cluster.url))
        monkeypatch.setattr(cli, "create_engine", lambda *a, **k: create_async_engine(engine_pg_cluster.url))
        rc = cli.main(["audit", str(task_id)])
        out = capsys.readouterr().out
        assert rc == 0
        assert f"audit task={task_id}" in out
        assert "worker=demo.echo" in out
        assert "result=ok" in out
        assert "model=deepseek-chat" in out
        assert "tokens_in=10" in out
        assert "tokens_out=20" in out
        assert "duration_ms=30" in out

    def test_audit_empty_hint(
        self, engine_pg_cluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "create_engine", lambda *a, **k: create_async_engine(engine_pg_cluster.url))
        rc = cli.main(["audit", "999999"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "无审计记录" in out


# ---- verify：monkeypatch _run_cmd，不真跑 pytest 嵌套 ----


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run_factory(results: dict[str, int]):
    def fake(cmd, *, cwd, timeout) -> _FakeProc:
        key = " ".join(cmd)
        return _FakeProc(results.get(key, 0))
    return fake


class TestVerifyCommand:
    def test_verify_all_green(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            cli,
            "_run_cmd",
            _fake_run_factory(
                {
                    "uv run python -m engine.lint": 0,
                    "uv run liuquan-engine registry-check": 0,
                    "uv run pytest": 0,
                }
            ),
        )
        rc = cli.main(["verify"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "绿 1/3 lint" in out
        assert "绿 2/3 registry" in out
        assert "绿 3/3 单测" in out
        assert "verify 通过" in out

    def test_verify_pytest_red(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            cli,
            "_run_cmd",
            _fake_run_factory(
                {
                    "uv run python -m engine.lint": 0,
                    "uv run liuquan-engine registry-check": 0,
                    "uv run pytest": 1,
                }
            ),
        )
        rc = cli.main(["verify"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "红 3/3 单测" in out
        assert "真红" in out

    def test_verify_lint_red(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            cli,
            "_run_cmd",
            _fake_run_factory(
                {
                    "uv run python -m engine.lint": 1,
                    "uv run liuquan-engine registry-check": 0,
                    "uv run pytest": 0,
                }
            ),
        )
        rc = cli.main(["verify"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "红 1/3 lint" in out


# ---- 版本 / 用法 / argparse 退出码 ----


class TestCliBasics:
    def test_version_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--version"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "liuquan-engine" in out
        assert cli.__version__ in out

    def test_no_command_prints_usage_exit_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "用法" in out
        for cmd in ("registry-check", "run", "resume", "audit", "verify"):
            assert cmd in out

    def test_unknown_command_usage_error_exit_two(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["bogus-command"])
        assert excinfo.value.code == 2

    def test_verify_subprocess_command_list_matches_spec(self) -> None:
        """verify 的 subprocess 命令与详设 §10 一致（lint/registry-check/pytest）。"""
        cmds = [" ".join(c) for _, c, _, _ in cli._VERIFY_CHECKS]
        assert cmds == [
            "uv run python -m engine.lint",
            "uv run liuquan-engine registry-check",
            "uv run pytest",
        ]

    def test_cli_module_invocation_end_to_end(self) -> None:
        """python -m engine.cli --version 端到端（console_scripts 同源）。"""
        proc = subprocess.run(
            [sys.executable, "-m", "engine.cli", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "liuquan-engine" in proc.stdout


async def _truncate_all(url: str) -> None:
    """清 4 表（TestAuditCommand 共享簇清理用；独立引擎防 loop 绑定）。"""
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(
                text(
                    "TRUNCATE engine_audit, engine_checkpoint, engine_step, "
                    "engine_task RESTART IDENTITY CASCADE"
                )
            )
    finally:
        await engine.dispose()
