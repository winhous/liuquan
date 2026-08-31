"""engine/connectors/http_image.py：普通图片 URL 下载（详设-v0.5 §5.5）。

新增连接器，CRM 对话图片用。
骨架与可用性检查，外部真实调用实现留批 5（CRM 图片工序）。

连接器特点：
- 普通 http(s) 图片 URL 下载（httpx/urllib + UA 伪装）
- 落盘 {storage_dir}/crm/<message_id>/
- 非图片/下载失败 → 降级 ok=False + note
"""

from __future__ import annotations

import os
from typing import Any

from engine.connectors import ConnectorResult, register_connector

__all__ = ["HTTPImageConnector"]

_CONNECTOR_ID = "http_image"


class HTTPImageConnector:
    """普通图片 URL 下载连接器（CRM 对话图片用）。

    本批只做骨架与可用性检查，外部真实调用实现留批 5。
    """

    def __init__(self, storage_dir: str | None = None) -> None:
        self._storage_dir = storage_dir or os.environ.get(
            "SCRAPE_STORAGE_DIR", "/opt/liuquan/scrape/"
        ).strip()

    @property
    def available(self) -> bool:
        """检查连接器是否可用（httpx/urllib 可用）。

        本批骨架：固定返回 True（httpx/urllib 是标准库/常见依赖）。
        """
        return True

    async def download(
        self,
        url: str,
        *,
        message_id: int | None = None,
        storage_dir: str | None = None,
    ) -> ConnectorResult:
        """下载普通图片 URL（本批骨架，外部真实调用实现留批 5）。

        Args:
            url: 图片 URL（http/https）
            message_id: CRM 消息 id（可选，用于落盘子目录）
            storage_dir: 存储目录（缺省用构造时的值）

        Returns:
            ConnectorResult: ok=True + data（图片路径/尺寸）/ ok=False + note（降级）
        """
        if not self.available:
            return ConnectorResult(
                ok=False,
                note="HTTP 图片下载不可用（httpx/urllib 未安装）",
            )

        # 本批骨架：真实调用实现留批 5
        # TODO: httpx/urllib + UA 伪装
        # TODO: 非图片/下载失败 → 降级 ok=False + note
        # TODO: 落盘 {storage_dir}/crm/<message_id>/

        target_dir = storage_dir or self._storage_dir
        return ConnectorResult(
            ok=False,
            note="HTTP Image connector 骨架（批 5 实现真实下载）",
            data={
                "url": url,
                "message_id": message_id,
                "storage_dir": target_dir,
            },
        )


def _factory(ctx: Any) -> HTTPImageConnector:
    """工厂函数：注册到 CONNECTORS 注册表。"""
    return HTTPImageConnector()


# 模块加载时注册
register_connector(_CONNECTOR_ID, _factory)