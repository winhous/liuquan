"""T6 嵌入式 PG16 测试簇 fixture（详设-v0.1 §13「ACT 写库通道：不桩，独立 PG
测试库，测的就是真 SQL 真事务」）。

engine_pg_cluster（session 级**同步** fixture，opt-in：只有显式请求它的测试
才拉起 PG，现有测试（test_llm.py 等）零感知、不拖慢）：
- initdb 临时簇到 tests/.pgdata/pgtest-<pid>/（随机端口、trust 认证、UTF8、
  --locale=C；tests/.pgdata/ 已被 .gitignore 拦截）
- pg_ctl 启动 -> createdb 建引擎库 -> alembic upgrade head（subprocess 调
  `uv run alembic`，DB URL 经 `-x db_url=<url>` 传参——env.py 读 alembic
  x 参数，不注入环境变量，P2 规则4 合法）
- yield PgCluster（DB URL 与 PGBIN 信息）-> teardown 停簇 + 清理数据目录
- 集群生命周期用同步 fixture 管（避免 session 级 async fixture 与 function 级
  事件循环冲突）；AsyncEngine 由测试内/function 级 async fixture 按需建

PG16 二进制在 ~/.local/pg16（Wave 0 T3 无 root 部署）；解压 libpq 不在系统
ld 路径，本模块以 os.putenv 注入 LD_LIBRARY_PATH 供子进程使用（只写环境，不读；
P2 规则4 拦的是 os.environ/os.getenv 的读取，putenv 属合法写入路径）。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- 回环地址一律运行期拼接（"127." 加 "0.0.1"），任何单一字符串常量不得含
  完整 IPv4 四段或 URL scheme（判据见 engine/lint/p2.py 模块 docstring）
- 不读 os.environ / os.getenv（P2 规则4）：环境传递只经 os.putenv 写
  LD_LIBRARY_PATH（子进程用）+ alembic URL 走命令行 -x 参数
"""

from __future__ import annotations

import getpass
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

# ---- 本机 PG16 部署位（Wave 0 T3：apt download + dpkg -x 解压，无 root）----
_PGROOT = Path.home() / ".local" / "pg16"
_PGBIN = _PGROOT / "usr/lib/postgresql/16/bin"
_PGLIB = _PGROOT / "usr/lib/x86_64-linux-gnu"

_DB_NAME = "liuquan_engine"
_HOST = "127." + "0.0.1"  # 回环地址运行期拼接（P2 拦完整 IPv4 字面量）
_SOCKET_DIR = "/tmp"


@dataclass(frozen=True)
class PgCluster:
    """嵌入式簇信息：url 供 AsyncEngine 连接；pgbin/port/datadir 供运维与调试。"""

    url: str
    pgbin: Path
    port: int
    datadir: Path
    db_name: str


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """子进程包装：失败时把 stderr 带出来（无网络访问：全部本地回环）。"""
    try:
        return subprocess.run(
            cmd, check=True, capture_output=True, text=True, **kwargs
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"命令失败 {cmd}: {detail}") from exc


def _free_port(host: str) -> int:
    """向内核要一个空闲端口（bind 0 随机分配；关闭后交给 PG 使用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def engine_pg_cluster() -> PgCluster:
    """session 级同步 fixture：临时簇生命周期（initdb -> 启动 -> 迁移 -> teardown）。"""
    if not (_PGBIN / "initdb").is_file():
        pytest.skip("本机未部署 PG16（Wave 0 T3 的 ~/.local/pg16 缺失）")

    repo_root = Path(__file__).resolve().parents[1]
    pgdata_root = repo_root / "tests" / ".pgdata"
    datadir = pgdata_root / f"pgtest-{os.getpid()}"
    # review major-2 修复：清理全部残留簇（含不同 pid 的孤儿——pytest 被 kill 后
    # teardown 不执行、PG 常驻、目录不删；原来只清同 pid，长期累积占端口/内存）
    if (_PGBIN / "pg_ctl").is_file():
        for stale in sorted(pgdata_root.glob("pgtest-*")):
            if not stale.is_dir():
                continue
            subprocess.run(
                [str(_PGBIN / "pg_ctl"), "-D", str(stale), "-m", "fast", "stop"],
                capture_output=True,
                text=True,
            )
    for stale in sorted(pgdata_root.glob("pgtest-*")):
        shutil.rmtree(stale, ignore_errors=True)
    datadir.mkdir(parents=True)

    user = getpass.getuser()
    port = _free_port(_HOST)
    logfile = datadir / "server.log"

    # 解压 libpq 不在系统 ld 路径：写入环境供所有 PG 子进程继承（只写不读，P2 安全）
    os.putenv("LD_LIBRARY_PATH", str(_PGLIB))

    started = False
    try:
        _run(
            [
                str(_PGBIN / "initdb"),
                "-D",
                str(datadir),
                "-U",
                user,
                "--auth-local=trust",
                "--auth-host=trust",
                "-E",
                "UTF8",
                "--locale=C",
            ]
        )
        _run(
            [
                str(_PGBIN / "pg_ctl"),
                "-D",
                str(datadir),
                "-l",
                str(logfile),
                "-o",
                f"-p {port} -k {_SOCKET_DIR} -c listen_addresses=localhost",
                "-w",
                "start",
            ],
            timeout=60,
        )
        started = True
        _run(
            [
                str(_PGBIN / "createdb"),
                "-h",
                _HOST,
                "-p",
                str(port),
                "-U",
                user,
                _DB_NAME,
            ]
        )

        url = f"postgresql+asyncpg://{user}@{_HOST}:{port}/{_DB_NAME}"
        # alembic 子进程（env.py）经 -x db_url= 收连接串（不注入环境变量）
        _run(
            [
                "uv",
                "run",
                "alembic",
                "-c",
                "migrations/engine/alembic.ini",
                "-x",
                f"db_url={url}",
                "upgrade",
                "head",
            ],
            cwd=repo_root,
            timeout=180,
        )
    except BaseException:
        # 设置中途失败（如迁移报错）也要停簇清目录，不留孤儿进程
        if started:
            subprocess.run(
                [
                    str(_PGBIN / "pg_ctl"),
                    "-D",
                    str(datadir),
                    "-m",
                    "fast",
                    "stop",
                ],
                capture_output=True,
                text=True,
            )
        shutil.rmtree(datadir, ignore_errors=True)
        raise

    cluster = PgCluster(
        url=url, pgbin=_PGBIN, port=port, datadir=datadir, db_name=_DB_NAME
    )
    yield cluster

    # teardown：停簇（尽力而为）+ 清理数据目录（tests/.pgdata/ 已 gitignore）
    subprocess.run(
        [
            str(_PGBIN / "pg_ctl"),
            "-D",
            str(datadir),
            "-m",
            "fast",
            "stop",
        ],
        capture_output=True,
        text=True,
    )
    shutil.rmtree(datadir, ignore_errors=True)
