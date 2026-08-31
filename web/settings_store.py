"""web/settings_store.py：配置地基 DAO（详设-v0.4 §3/§4，决策 34）。

SettingsStore：sys.settings 表 + sys.shop 表的 DAO 层。
- settings：get(key, default) / set(key, value, description='') / 独立提交原子性
- shop：list_shops / create_shop / update_shop / toggle_shop / delete_shop
- 类型解析：按 key 注册表判定 int/bool/time/str，解析失败回退默认
- from_env() 类方法（照 CRMStore.from_env 模式，.env 的 LIUQUAN_TM_DB_URL）

R24 零 engine import；R20 密钥只进 .env（settings 表存非敏感参数）。
"""

from __future__ import annotations

import logging
from datetime import time
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.sys import Setting, Shop
from web.tm_store import create_tm_engine

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOTENV_PATH = _REPO_ROOT / ".env"
_TM_DB_URL_ENV = "LIUQUAN_TM_DB_URL"

# ---- 类型解析注册表（详设 §7.3 键清单）----
# key -> 类型标识；不在注册表的 key 视为 str
_TYPE_REGISTRY: dict[str, str] = {
    "crm.follow_up_days": "int",
    "crm.page_size": "int",
    "schedule.default_time": "time",
    "engine.max_attempts": "int",
    "engine.timeout_s": "float",
    "engine.backoff_cap": "int",
    "notify.feishu_enabled": "bool",
}


class SettingsError(Exception):
    """业务拒绝（路由捕获 -> 页面 err 提示；照 CrmWebError 模式）。"""


def _parse_value(raw: str, key: str) -> object:
    """按 key 注册类型解析 value 字符串；失败返回 None（调用方回退 default）。"""
    type_kind = _TYPE_REGISTRY.get(key, "str")
    if type_kind == "int":
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None
    if type_kind == "float":
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None
    if type_kind == "bool":
        normalized = raw.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
        return None
    if type_kind == "time":
        try:
            parts = raw.strip().split(":")
            return time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError, TypeError):
            return None
    # str
    return raw


class SettingsStore:
    """配置地基 DAO（构造注入 engine；生产经 from_env）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._maker = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_env(cls) -> SettingsStore:
        url = (dotenv_values(_DOTENV_PATH) or {}).get(_TM_DB_URL_ENV)
        return cls(create_tm_engine(url))

    async def dispose(self) -> None:
        await self._engine.dispose()

    # ---- settings DAO ----

    async def get(self, key: str, default: object = None) -> object:
        """查 sys.settings -> 有则按类型解析返回；无/解析失败回退 default。"""
        async with self._maker() as session:
            row = await session.get(Setting, key)
            if row is None:
                return default
            parsed = _parse_value(row.value, key)
            if parsed is None:
                return default
            return parsed

    async def get_raw(self, key: str) -> str | None:
        """查 sys.settings 原始 TEXT 值（不解析；设置页展示用）。"""
        async with self._maker() as session:
            row = await session.get(Setting, key)
            return row.value if row is not None else None

    async def set(
        self, key: str, value: object, description: str = ""
    ) -> None:
        """upsert 单键（独立提交原子性，只动该 key，不动其他 key）。"""
        value_str = str(value) if value is not None else ""
        description = description or ""
        async with self._maker() as session, session.begin():
            existing = await session.get(Setting, key)
            if existing is not None:
                existing.value = value_str
                if description:
                    existing.description = description
            else:
                session.add(
                    Setting(key=key, value=value_str, description=description)
                )

    # ---- shop DAO ----

    async def list_shops(self, *, include_disabled: bool = False) -> list[dict]:
        """列出店铺；默认只含启用中（CRM 下拉用）。"""
        async with self._maker() as session:
            stmt = select(Shop).order_by(Shop.id.asc())
            if not include_disabled:
                stmt = stmt.where(Shop.enabled == True)  # noqa: E712
            rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": s.id,
                "name": s.name,
                "remark": s.remark or "",
                "enabled": s.enabled,
                "created_at": s.created_at,
            }
            for s in rows
        ]

    async def create_shop(self, name: str, remark: str = "") -> int:
        """新建店铺（重名抛业务错）。"""
        name = name.strip()
        if not name:
            raise SettingsError("店铺名必填")
        if len(name) > 100:
            raise SettingsError("店铺名不超过 100 字")
        async with self._maker() as session, session.begin():
            dup = await session.execute(
                select(Shop.id).where(func.lower(Shop.name) == name.lower())
            )
            if dup.scalar_one_or_none() is not None:
                raise SettingsError(f"已存在同名店铺「{name}」（忽略大小写）")
            row = Shop(name=name, remark=(remark or "").strip())
            session.add(row)
            await session.flush()
            return row.id

    async def update_shop(self, shop_id: int, name: str, remark: str = "") -> None:
        """改店铺名/备注（重名抛业务错）。"""
        name = name.strip()
        if not name:
            raise SettingsError("店铺名必填")
        async with self._maker() as session, session.begin():
            shop = await session.get(Shop, shop_id)
            if shop is None:
                raise SettingsError("店铺不存在")
            dup = await session.execute(
                select(Shop.id).where(
                    func.lower(Shop.name) == name.lower(),
                    Shop.id != shop_id,
                )
            )
            if dup.scalar_one_or_none() is not None:
                raise SettingsError(f"已存在同名店铺「{name}」（忽略大小写）")
            shop.name = name
            shop.remark = (remark or "").strip()

    async def toggle_shop(self, shop_id: int) -> bool:
        """启停店铺；返回新的 enabled 状态。"""
        async with self._maker() as session, session.begin():
            shop = await session.get(Shop, shop_id)
            if shop is None:
                raise SettingsError("店铺不存在")
            shop.enabled = not shop.enabled
            return shop.enabled

    async def delete_shop(self, shop_id: int) -> None:
        """物理删除店铺（二次确认由路由层 hx-confirm 保证）。"""
        async with self._maker() as session, session.begin():
            shop = await session.get(Shop, shop_id)
            if shop is None:
                raise SettingsError("店铺不存在")
            await session.delete(shop)


__all__ = [
    "SettingsError",
    "SettingsStore",
]
