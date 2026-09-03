"""业务库 ORM（database: liuquan, schema: sys）—— settings / shop 两表。

纯数据层（详设-v0.4 §3/§4：无业务逻辑；web 侧读写，引擎经 HTTP
接口读取，引擎零业务库连接串（决策 26）。R22 唯一通用语言）。

settings 表：键值对配置（域.参数 命名；value 全 TEXT，按 key 类型解析）。
shop 表：店铺管理（增删改启停；CRM source_shop 下拉候选来源）。

复用 models/tm.py 的 TmBase（业务库 declarative base，R22）。
migrations/business/versions/0006 创建 sys schema + 两表（详设 §3/§4）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from models.tm import TmBase


class Setting(TmBase):
    """sys.settings：配置键值对（详设-v0.4 §3）。

    键命名：域.参数（如 crm.follow_up_days），CHECK 正则防注入；
    value 全 TEXT，按键类型解析（int/bool/time/str）；
    description 中文说明（设置页展示）。
    """

    __tablename__ = "settings"
    __table_args__ = (
        CheckConstraint(
            "key ~ '^[a-z][a-z0-9_.\\-]{0,63}$'",
            name="chk_setting_key_format",
        ),
        {"schema": "sys"},
    )

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    description: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Shop(TmBase):
    """sys.shop：店铺管理（详设-v0.4 §4 + v0.7 §3.7 platform 列）。

    name 唯一（CHECK + 索引）；enabled 启停（禁用不出现在 CRM 下拉）。
    CRM customer.source_shop 存的是店铺名文本，无 FK（R21 精神）。
    platform 店铺平台（迁移 0013，详设-v0.7 §3.7）：etsy/xianyu/xhs/other。
    """

    __tablename__ = "shop"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 100", name="chk_shop_name"),
        CheckConstraint(
            "platform IN ('etsy','xianyu','xhs','other')",
            name="chk_sys_shop_platform",
        ),
        Index("idx_shop_enabled", "enabled"),
        {"schema": "sys"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    remark: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    platform: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'other'")
    )  # 'etsy','xianyu','xhs','other'（v0.7 §3.7）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["Setting", "Shop"]
