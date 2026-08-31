"""engine/connectors/ehunt_api.py：eHunt API 通道（详设-v0.5 §5.1）。

照搬广成 runtime/ehunt_connector.py，骨架与可用性检查。
外部真实调用实现留批 2/批 3（SEO 工具工序）。

连接器特点：
- POST api.ehunt.ai/api/v1/items，X-VIP-TOKEN 鉴权
- UA 伪装必做（eHunt WAF 拦 python-urllib 默认 UA 直接 403）
- 两道闸：8 词硬顶（keywords[:8]）+ page_size≤100（默认 10）
- 429 = 当日配额耗尽 → 降级 {ok: False, note: "配额耗尽…"}，不抛穿链
- 记账：透传服务端 quota 回显（详设 §9），无本地账本
"""

from __future__ import annotations

import os
from typing import Any

from engine.connectors import ConnectorResult, register_connector

__all__ = ["EHuntAPIConnector"]

_CONNECTOR_ID = "ehunt_api"
_API_KEY_ENV = "EHUNT_API_KEY"


class EHuntAPIConnector:
    """eHunt API 通道（竞品画像）。

    本批只做骨架与可用性检查，外部真实调用实现留批 2/批 3。
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get(_API_KEY_ENV, "").strip()

    @property
    def available(self) -> bool:
        """检查连接器是否可用（api_key 已配置）。"""
        return bool(self._api_key)

    async def search(
        self,
        keywords: list[str],
        *,
        page_size: int = 10,
    ) -> ConnectorResult:
        """搜索竞品画像（本批骨架，外部调用实现留批 2/批 3）。

        Args:
            keywords: 关键词列表（最多 8 个，硬顶）
            page_size: 每页条数（≤100，默认 10）

        Returns:
            ConnectorResult: ok=True + data + quota / ok=False + note（降级）
        """
        if not self.available:
            return ConnectorResult(
                ok=False,
                note="eHunt API key 未配置（EHUNT_API_KEY 环境变量未设置或为空）",
            )

        # 本批骨架：真实调用实现留批 2/批 3
        # 两道闸：8 词硬顶 + page_size≤100
        truncated_keywords = keywords[:8]
        clamped_page_size = min(max(1, page_size), 100)

        # TODO: 批 2 实现真实 HTTP 调用（POST api.ehunt.ai/api/v1/items）
        # TODO: UA 伪装必做（Mozilla/5.0 ...）
        # TODO: 429 降级（配额耗尽）
        # TODO: quota 透传（服务端回显）

        return ConnectorResult(
            ok=False,
            note="eHunt API connector 骨架（批 2 实现真实调用）",
            data={
                "keywords": truncated_keywords,
                "page_size": clamped_page_size,
            },
        )


def _factory(ctx: Any) -> EHuntAPIConnector:
    """工厂函数：注册到 CONNECTORS 注册表。"""
    return EHuntAPIConnector()


# 模块加载时注册
register_connector(_CONNECTOR_ID, _factory)