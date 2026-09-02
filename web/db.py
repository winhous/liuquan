"""web/db.py：web 侧数据库会话工具（v0.5 批 4 补建——scrape_store 依赖）。

照 tm_store 模式：连接串读仓库根 .env 的 LIUQUAN_TM_DB_URL（R20/P2 规则 4
合法来源 = .env 文件，dotenv_values 读，不触碰 os.environ）；
模块级惰性 AsyncEngine（进程内复用），get_db_session 上下文管理器。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from dotenv import dotenv_values
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

# 业务库连接串变量名（R20：值只存 .env；照 web/tm_store.py 同款注释）
_TM_DB_URL_ENV = "LIUQUAN_TM_DB_URL"

# web/db.py -> web/ -> 仓库根（不依赖 cwd）
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOTENV_PATH = _REPO_ROOT / ".env"

_engine = None
_maker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """惰性建业务库 AsyncEngine（进程内复用，失败抛 ValueError）。

    v0.6 批 1 起公开：scrape_store 的 get_link_queue/set_link_queue 需构建
    SettingsStore 读设置键（照函数式 DAO 风格，引擎与 get_db_session 共用）。
    """
    global _engine
    if _engine is None:
        url = dotenv_values(_DOTENV_PATH).get(_TM_DB_URL_ENV)
        if not url:
            raise ValueError(
                f".env 的 {_TM_DB_URL_ENV} 未设置：web 需要业务库连接串"
                "（规范 R20：值只存 .env）"
            )
        _engine = create_async_engine(url, pool_pre_ping=True)
    return _engine


def _get_maker() -> async_sessionmaker[AsyncSession]:
    """惰性建 sessionmaker（进程内复用，失败抛 ValueError）。"""
    global _maker
    if _maker is None:
        _maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _maker


@asynccontextmanager
async def get_db_session() -> AsyncIterator[AsyncSession]:
    """业务库会话上下文管理器（scrape_store 等函数式 DAO 用）。"""
    maker = _get_maker()
    async with maker() as session:
        yield session
