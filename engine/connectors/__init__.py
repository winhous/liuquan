"""engine/connectors：外部资源连接器注册表（详设-v0.5 §5）。

工序不直连外部（R11 精神 + 广成「外联 connector」先例）。connector = 外部资源封装
（HTTP/CDP/浏览器/本地 vendor），工序内只按 id 引用。

设计要点：
- CONNECTORS: dict[str, ConnectorFactory] 注册表（id -> 工厂函数）
- get_connector(id, ctx) 原语：按 id 取 connector 实例（可注入测试 fake）
- 未配置 key / vendor 缺失 / Chrome 未起 → 返回降级结构（ok=False + note），不抛穿链
- Key 纪律：EHUNT_API_KEY / VISION_API_KEY 等只进 .env（R20/P2），connector 读 os.environ
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol

__all__ = [
    "ConnectorFactory",
    "ConnectorResult",
    "CONNECTORS",
    "get_connector",
    "register_connector",
    "build_connectors",
]

# ---- 连接器结果（降级结构：ok=False + note 不抛穿链）----


@dataclass(frozen=True, slots=True)
class ConnectorResult:
    """连接器执行结果（详设 §5：失败降级结构 {ok, note, data}）。"""

    ok: bool
    note: str = ""
    data: dict[str, Any] | None = None


# ---- 连接器工厂协议（可注入测试 fake）----


class ConnectorFactory(Protocol):
    """连接器工厂函数签名：接收 ctx（EngineContext），返回连接器实例。"""

    def __call__(self, ctx: Any) -> Any: ...


# ---- 注册表 ----

CONNECTORS: dict[str, ConnectorFactory] = {}


def register_connector(connector_id: str, factory: ConnectorFactory) -> None:
    """注册连接器工厂（模块加载时调用）。"""
    CONNECTORS[connector_id] = factory


def get_connector(connector_id: str, ctx: Any) -> Any | None:
    """按 id 取连接器实例；未注册返回 None（工序内降级处理）。

    用法示例（工序 run.py）：
        connector = ctx.connectors.get("ehunt_api")
        if connector is None:
            return ConnectorResult(ok=False, note="ehunt_api connector 未注入")
        result = await connector.search(keywords=["test"])
        if not result.ok:
            return result  # 降级：ok=False + note，不抛穿链
    """
    factory = CONNECTORS.get(connector_id)
    if factory is None:
        return None
    return factory(ctx)


def build_connectors(storage_dir: str | None = None) -> dict[str, Any]:
    """装配连接器实例注册表（server 启动用；工序经 ctx.connectors 按 id 引用）。

    v0.6 §5.5：storage_dir 从引擎启动读 engine-params 的 scrape.storage_dir 传入
    （connector 落盘根目录）；工厂签名照 _factory(ctx, storage_dir=None)。
    """
    out: dict[str, Any] = {}
    for connector_id, factory in CONNECTORS.items():
        try:
            out[connector_id] = factory(None, storage_dir=storage_dir)
        except TypeError:
            # 兼容未接 storage_dir 参数的工厂（防御：缺参时按原签名调用）
            out[connector_id] = factory(None)
    return out


# ---- 环境变量读取辅助（照 models_config.py 的 env: 模式）----


def _read_env(key: str, required: bool = False) -> str | None:
    """读 os.environ；required=True 且未设置/为空时返回 None（调用方降级）。

    R20：密钥只进 .env，dotenv 装载在入口层，本模块只读 os.environ。
    P2：connector 在豁免区内（engine 包），可读 os.environ。
    """
    value = os.environ.get(key, "").strip()
    if not value:
        if required:
            return None
        return ""
    return value


# ---- 加载骨架模块（注册五个连接器）----
# xhs/xianyu 已照广成搬回真实调用（详设-v0.6 §6 H1-H3，批 3）；ehunt_api/ehunt_keyword
# 为 v0.5 SEO 连接器（可用性检查 + 真实 HTTP 调用）；http_image 通用图片连接器

from engine.connectors import ehunt_api  # noqa: E402
from engine.connectors import ehunt_keyword  # noqa: E402
from engine.connectors import http_image  # noqa: E402
from engine.connectors import quark  # noqa: E402  # v0.6 批 7：夸克网盘（上传/登录状态探测，真跑才碰 subprocess）
from engine.connectors import xhs  # noqa: E402
from engine.connectors import xianyu  # noqa: E402