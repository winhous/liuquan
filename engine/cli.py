"""刘全引擎 CLI 入口（详设-v0.1 §10；任务 T12b）。

五个命令全部落地：

- ``registry-check [--write-hashes]``：全册 YAML+Model 校验（loader.validate），
  合规时打印工序/链/Context provider/事件/Action 清单（空册 = 合法）；
  ``--write-hashes`` 重写 L9 登记文件 engine/registry/model_hashes.yaml。
  0 违规退出 0，有违规逐行打印 ``[L#] ...`` 退出 1。
- ``run <chain_id> --input '<json>'``：建任务入队并同步执行到终态
  （v0.1 无常驻进程，一次进程跑完）。坏 JSON / 非对象输入 = 数据错误，
  报错退出 1（业务失败；argparse 的 usage 错才退出 2）。
- ``resume <task_id>``：从最后检查点续跑（崩溃/暂停后）。
- ``audit <task_id>``：该任务全部 LLM 调用明细（每条一行 audit_summary，
  §8 安全摘要）；无记录提示后退出 0。
- ``verify``：四绿报告（lint + registry + 单测），subprocess 调
  ``uv run python -m engine.lint`` / ``uv run liuquan-engine registry-check`` /
  ``uv run pytest``，汇总「绿/红」风格对齐 scripts/check.sh；全过退出 0，
  任一失败退出 1。

退出码约定（scripts/check.sh 依赖此约定）：
- 0 成功；1 业务失败（校验不合规/执行失败/数据错误）；2 argparse usage 错。

技术决策（供变更日志）：
- ``run/resume`` 的 runner 依赖（engine/core/runner.py，T12a 并行实现中）
  延迟导入（``_runner_module()``），registry-check/audit/verify 不依赖 runner
  即可独立工作；CLI 按 T12a 接口契约对接，不修改 runner。
- ``writable_check``（DbAuditGate 的探活注入）取 CLI 一次性探活结果：
  进程启动期对引擎库做一次最小连接探活（SELECT 1），成功后注入
  ``writable_check=lambda: True``——CLI 是一次性进程，审计可写性在启动期
  已验证；逐次调用的探活属 runner/常驻进程的职责。
- ``registry-check``/``run``/``resume`` 先经 ``dotenv.load_dotenv`` 装载仓库根
  .env 进 os.environ（models.yaml 的 env: 引用在加载期由 T8 解析；R20 值只存
  .env）；未配置 .env 时 registry-check 如实报 [L4] 拒载（fail-fast，
  与 loader「拒载 = 引擎不起」同哲学）。
- 仓库根定位：自 cwd 向上找 pyproject.toml 标记（不依赖调用方 cwd）。

CLI 属引擎自身（engine/cli.py），不放 scripts/（开发规范 R24）；CLI 自身在
lint 扫描对象内（P2/P3 照扫）：本文件不出现 URL/IP/密钥字面量，不读
os.environ（.env 装载经 dotenv 库），连接串只在运行期来自 .env。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import text

from engine.core.audit import audit_summary
from engine.core.db import create_engine, dispose_engine, get_audit
from engine.core.llm.models_config import load_models
from engine.registry import loader

# 与 pyproject.toml 的 project.version 保持同步（v0.1 内均为 0.1.0）
__version__ = "0.1.0"

USAGE = """\
用法: liuquan-engine <命令> [参数]

v0.1 命令（详设 §10）:
  registry-check       全册 YAML+Model 校验，打印工序/链/事件/Action 清单
                        （--write-hashes 重写 L9 Model 结构摘要登记）
  run <chain_id>       建任务入队并同步执行到终态（--input 传 JSON 对象）
  resume <task_id>     从最后检查点续跑（崩溃/暂停后）
  audit <task_id>      查该任务全部 LLM 调用明细（§8 安全摘要）
  verify               lint + registry + 单测，四绿报告

退出码: 0 成功；1 业务失败（校验不合规/执行失败/输入数据错误）；2 参数用法错误
"""

# verify 的三项检查（详设 §10）：(展示名, 子进程命令, 超时秒, 红时描述)
_VERIFY_CHECKS = (
    ("1/3 lint", ("uv", "run", "python", "-m", "engine.lint"), 120.0, "lint 有违规（详见上方输出）"),
    (
        "2/3 registry",
        ("uv", "run", "liuquan-engine", "registry-check"),
        120.0,
        "registry-check 不合规",
    ),
    ("3/3 单测", ("uv", "run", "pytest"), 1800.0, "pytest 有失败或未收集到测试"),
)


# ---- 基础设施 ----


def _repo_root() -> Path:
    """仓库根定位：自 cwd 向上找 pyproject.toml（找不到退回 cwd）。"""
    cwd = Path.cwd().resolve()
    for cand in (cwd, *cwd.parents):
        if (cand / "pyproject.toml").is_file():
            return cand
    return cwd


def _load_dotenv(repo_root: Path) -> None:
    """装载仓库根 .env 进 os.environ（R20：值只存 .env；env: 引用在加载期解析）。"""
    load_dotenv(repo_root / ".env")


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _runner_module() -> Any:
    """延迟导入 engine.core.runner（T12a 并行实现中；CLI 按接口契约对接）。

    run/resume 之外的命令不依赖 runner，无需加载本模块。
    """
    from engine.core import runner  # noqa: PLC0415

    return runner


def _llm_agent_factory() -> Any:
    """真 Agent 工厂（engine/core/llm/agent.py；agent_cls 不传 = 真 Agent，§13）。"""
    from engine.core.llm import agent_factory  # noqa: PLC0415

    return agent_factory


async def _probe_db(engine: Any) -> None:
    """最小 DB 探活：连接引擎库执行 SELECT 1（CLI 启动期一次性探活）。

    失败即抛异常（由上层转成清晰错误退出 1）；成功后 writable_check 取真。
    测试可整体桩掉本函数（monkeypatch cli._probe_db）。
    """
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _dispose(engine: Any) -> None:
    await dispose_engine(engine)


# ---- registry-check ----


def _print_registry(registry: Any) -> None:
    """打印注册表清单（空册 = 合法，详设 §4.4；load_registry 成功时）。"""
    print("==== 注册表清单（空册 = 合法，详设 §4.4）====")
    print(f"工序 {len(registry.workers)}：{_fmt_ids(registry.workers)}")
    print(f"链 {len(registry.chains)}：{_fmt_ids(registry.chains)}")
    print(f"Context provider {len(registry.context_providers)}：{_fmt_ids(registry.context_providers)}")
    print(f"事件 {len(registry.events)}：{_fmt_ids(registry.events)}")
    print(f"Action {len(registry.actions)}：{_fmt_ids(registry.actions)}")


def _fmt_ids(mapping: dict) -> str:
    ids = ", ".join(mapping) if mapping else "（无）"
    return ids


def _cmd_registry_check(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    _load_dotenv(repo_root)
    violations = loader.validate(repo_root)
    if args.write_hashes:
        # review 修复：注册表有「非 L9」违规时不改写登记文件（失败路径上的写
        # 副作用）；L9 mismatch 本身正是 write-hashes 要修的（改 Model 后
        # bump version + 重跑登记），不拦。
        blocking = [v for v in violations if not v.startswith("[L9]")]
        if blocking:
            for line in blocking:
                print(line)
            print(
                f"registry-check：{len(blocking)} 条非 L9 违规，--write-hashes 已跳过"
                "（先修违规再重写登记）"
            )
            return 1
        try:
            path = loader.write_hashes(repo_root)
        except loader.RegistryLoadError as exc:
            for line in exc.violations:
                print(line)
            return 1
        print(f"已写入 Model 结构摘要登记（L9）：{_rel(repo_root, path)}")
        violations = loader.validate(repo_root)  # 重写后重新校验（L9 已修）
    if violations:
        for line in violations:
            print(line)
        print(f"registry-check：{len(violations)} 条违规，注册表不合规（详设 §4.4 拒载）")
        return 1
    registry = loader.load_registry(repo_root)
    _print_registry(registry)
    print("registry-check：0 违规，注册表合规")
    return 0


# ---- run / resume ----


def _print_result(result: Any, elapsed: float, *, verb: str = "created") -> int:
    """打印任务结果（§10 相位流水）：任务行 + 每相位一行 + 终态行。

    verb：run 用 "created"；resume 用 "resumed"（review 修复：resume 误打 created）。
    """
    print(f"task e-{result.task_id:06d} {verb}, chain: {result.chain_id}")
    for line in result.phase_lines:
        print(f"[{line.phase}] {line.message}")
    if str(result.status).lower() == "done":
        print(f"[DONE] {elapsed:.1f} s total")
        return 0
    err = result.error or f"status={result.status}"
    print(f"[FAILED] {err}")
    return 1


async def _run_task_async(repo_root: Path, chain_id: str, input_: dict) -> int:
    """run 的异步主体：建引擎 -> 探活 -> TaskRunner.run -> 打印结果。"""
    engine = None
    try:
        engine = create_engine()
        registry = loader.load_registry(repo_root)
        model_registry = load_models(repo_root / "models.yaml")
        await _probe_db(engine)
        runner = _runner_module().TaskRunner(
            engine,
            registry,
            agent_factory=_llm_agent_factory(),
            writable_check=lambda: True,
            model_registry=model_registry,
            repo_root=repo_root,
        )
        start = time.monotonic()
        result = await runner.run(chain_id, input_)
        elapsed = time.monotonic() - start
    finally:
        if engine is not None:
            await _dispose(engine)
    return _print_result(result, elapsed)


async def _resume_task_async(repo_root: Path, task_id: int) -> int:
    """resume 的异步主体：建引擎 -> 探活 -> TaskRunner.resume -> 打印结果。"""
    engine = None
    try:
        engine = create_engine()
        registry = loader.load_registry(repo_root)
        model_registry = load_models(repo_root / "models.yaml")
        await _probe_db(engine)
        runner = _runner_module().TaskRunner(
            engine,
            registry,
            agent_factory=_llm_agent_factory(),
            writable_check=lambda: True,
            model_registry=model_registry,
            repo_root=repo_root,
        )
        start = time.monotonic()
        result = await runner.resume(task_id)
        elapsed = time.monotonic() - start
    finally:
        if engine is not None:
            await _dispose(engine)
    return _print_result(result, elapsed, verb="resumed")


def _cmd_run(args: argparse.Namespace) -> int:
    """run <chain_id> --input '<json>'：坏 JSON / 非对象 = 数据错误退出 1。"""
    repo_root = _repo_root()
    _load_dotenv(repo_root)
    try:
        input_ = json.loads(args.input)
    except json.JSONDecodeError as exc:
        print(f"错误：--input 不是合法 JSON（{exc}）", file=sys.stderr)
        return 1
    if not isinstance(input_, dict):
        print("错误：--input 必须是 JSON 对象（键值映射）", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_run_task_async(repo_root, args.chain_id, input_))
    except ValueError as exc:
        # create_engine 缺 .env 等配置错误（R20：值只存 .env）
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except loader.RegistryLoadError as exc:
        print("错误：注册表拒载（详见下方）：", file=sys.stderr)
        for line in exc.violations:
            print(line, file=sys.stderr)
        return 1
    except Exception as exc:  # CLI 顶层兜底：runner/DB 等未预期失败显式报错
        print(f"错误：{exc}", file=sys.stderr)
        return 1


def _cmd_resume(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    _load_dotenv(repo_root)
    try:
        return asyncio.run(_resume_task_async(repo_root, args.task_id))
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except loader.RegistryLoadError as exc:
        print("错误：注册表拒载（详见下方）：", file=sys.stderr)
        for line in exc.violations:
            print(line, file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


# ---- audit ----


async def _audit_rows_async(task_id: int) -> list:
    """audit 的异步主体：建引擎 -> get_audit（引擎随查询生命周期释放）。"""
    engine = None
    try:
        engine = create_engine()
        return await get_audit(engine, task_id)
    finally:
        if engine is not None:
            await _dispose(engine)


def _cmd_audit(args: argparse.Namespace) -> int:
    """audit <task_id>：每条一行 §8 安全摘要；空则提示无记录，退出 0。"""
    repo_root = _repo_root()
    _load_dotenv(repo_root)
    try:
        rows = asyncio.run(_audit_rows_async(args.task_id))
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    if not rows:
        print(f"任务 e-{args.task_id:06d} 无审计记录（任务不存在或尚无 LLM 调用）")
        return 0
    for row in rows:
        print(audit_summary(row._asdict()))
    return 0


# ---- verify ----


def _run_cmd(cmd: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    """执行子进程并捕获输出（verify 用；测试注入桩，禁止测试内真跑 pytest 嵌套）。"""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _cmd_verify(args: argparse.Namespace) -> int:
    """verify：lint + registry + 单测，四绿报告（风格对齐 scripts/check.sh）。"""
    repo_root = _repo_root()
    summary: list[str] = []
    red = 0
    for label, cmd, timeout, red_desc in _VERIFY_CHECKS:
        print(f"---- {label}：{' '.join(cmd)} ----")
        try:
            proc = _run_cmd(list(cmd), cwd=repo_root, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"（超时 {int(timeout)}s 被终止）")
            summary.append(f"红 {label}（超时）")
            red += 1
            continue
        except FileNotFoundError as exc:
            print(f"（命令不可用：{exc}）")
            summary.append(f"红 {label}（命令不可用）")
            red += 1
            continue
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.strip().splitlines()[-15:]:
            print(line)
        if proc.returncode == 0:
            summary.append(f"绿 {label}（通过）")
        else:
            summary.append(f"红 {label}（{red_desc}）")
            red += 1
    print()
    print("==== 汇总（绿/红，风格对齐 scripts/check.sh）====")
    for line in summary:
        print(line)
    if red:
        print(f"\n结果：{red} 项真红 — verify 未通过（四绿守门 R13，改动未完成）")
        return 1
    print("\n结果：verify 通过（lint + registry + 单测全绿）")
    return 0


# ---- argparse ----


def build_parser() -> argparse.ArgumentParser:
    """构建参数解析器（五子命令，退出码约定见模块 docstring）。"""
    parser = argparse.ArgumentParser(
        prog="liuquan-engine",
        description="刘全 AI 引擎 CLI（v0.1 地基，详设 §10）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p_reg = sub.add_parser("registry-check", help="全册 YAML+Model 校验，打印工序/链/事件/Action 清单")
    p_reg.add_argument(
        "--write-hashes",
        action="store_true",
        help="重写 engine/registry/model_hashes.yaml（L9 Model 结构摘要登记）",
    )
    p_reg.set_defaults(func=_cmd_registry_check)

    p_run = sub.add_parser("run", help="建任务入队并同步执行到终态（--input 传 JSON 对象）")
    p_run.add_argument("chain_id", help="链 id（registry 已登记）")
    p_run.add_argument("--input", required=True, help="任务输入 JSON 对象，如 '{\"text\": \"hi\"}'")
    p_run.set_defaults(func=_cmd_run)

    p_res = sub.add_parser("resume", help="从最后检查点续跑（崩溃/暂停后）")
    p_res.add_argument("task_id", type=int, help="任务 id")
    p_res.set_defaults(func=_cmd_resume)

    p_aud = sub.add_parser("audit", help="查该任务全部 LLM 调用明细（§8 安全摘要）")
    p_aud.add_argument("task_id", type=int, help="任务 id")
    p_aud.set_defaults(func=_cmd_audit)

    p_ver = sub.add_parser("verify", help="lint + registry + 单测，四绿报告")
    p_ver.set_defaults(func=_cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（console_scripts ``liuquan-engine`` 指向此处）。

    ``--version`` 打印版本退出 0；无命令打印用法退出 0（骨架期行为保留）；
    未知参数/未知命令交给 argparse 报错退出 2。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        print(USAGE, end="")
        return 0
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
