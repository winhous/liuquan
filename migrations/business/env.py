"""业务库 Alembic 环境（详设-v0.2 §2.2：tm schema 三表迁移，v0.2 新增）。

- DB URL 来源（P2 规则4 合法来源，不触碰 os.environ）：
  1) alembic 命令行 `-x db_url=<url>`（测试/部署注入）；
  2) 仓库根 .env 的 LIUQUAN_TM_DB_URL（dotenv_values 读文件，R20 值只存 .env）。
  两处都无 = 报错退出。
- target_metadata = models.tm 的 TmBase.metadata（ORM 与迁移同源对齐，
  v0.2 初始迁移为手写 DDL，后续 autogenerate 可直接复用本 metadata）。
- async 模式（承 v0.1 技术定：引擎走 asyncpg，业务库同 async 引擎）。
- 与 migrations/engine/env.py 同构（engine 迁引擎库 4 表，本文件迁业务库 3 表）。
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import dotenv_values
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# migrations/business/env.py -> 仓库根（不依赖 cwd/alembic.ini 的 prepend_sys_path）
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from models.tm import TmBase  # noqa: E402  （sys.path 注入后导入）

_DB_URL_ENV = "LIUQUAN_TM_DB_URL"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = TmBase.metadata


def _db_url() -> str:
    """业务库连接串：`-x db_url=` 优先，其次 .env 文件；两处都无报错退出。

    alembic 1.19 无 get_x_argument（旧 API），-x 参数存 config.cmd_opts.x
    （action="append" 的 key=value 列表），按 key 取 db_url。
    """
    cmd_opts = getattr(config, "cmd_opts", None)
    if cmd_opts is not None:
        for item in getattr(cmd_opts, "x", None) or ():
            key, sep, value = item.partition("=")
            if sep and key == "db_url":
                return value
    url = dotenv_values(_REPO_ROOT / ".env").get(_DB_URL_ENV)
    if not url:
        raise RuntimeError(
            f"未提供业务库连接串：alembic -x db_url=... 或 .env 的 {_DB_URL_ENV}"
            "（规范 R20：值只存 .env）"
        )
    return url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 不连库（URL 仍要求环境变量）。"""
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: object) -> None:
    """在同步连接上执行迁移（async 模式下经 run_sync 调用）。"""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """异步连接执行迁移（业务库走 asyncpg，SQLAlchemy async 引擎 + asyncio.run）。"""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _db_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式：从环境变量 URL 建 async 连接执行迁移。"""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
