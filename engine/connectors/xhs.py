"""engine/connectors/xhs.py：小红书扒图（详设-v0.5 §5.3）。

照搬广成 image-download，骨架与可用性检查。
外部真实调用实现留批 4（扒图工序）。

连接器特点：
- 路由条件：xiaohongshu.com（保留 xsec_token query）
- vendor/XHS-Downloader（uv run python -c 子进程调用，库依赖与刘全 venv 隔离）
- 元数据：sqlite3 读 ExploreData.db（作品 ID → 作品描述/标签/作者 ID）
- 元数据失败降级不阻塞
"""

from __future__ import annotations

import os
from typing import Any

from engine.connectors import ConnectorResult, register_connector

__all__ = ["XHSConnector"]

_CONNECTOR_ID = "xhs"


class XHSConnector:
    """小红书扒图连接器。

    本批只做骨架与可用性检查，外部真实调用实现留批 4。
    """

    def __init__(self, storage_dir: str | None = None) -> None:
        self._storage_dir = storage_dir or os.environ.get(
            "SCRAPE_STORAGE_DIR", "/opt/liuquan/scrape/"
        ).strip()

    @property
    def available(self) -> bool:
        """检查连接器是否可用（vendor 目录存在）。

        本批骨架：固定返回 False（真实检测留批 4）。
        """
        # TODO: 批 4 实现真实 vendor 目录检测
        return False

    async def download(
        self,
        url: str,
        *,
        batch_id: str,
        storage_dir: str | None = None,
    ) -> ConnectorResult:
        """下载小红书作品图片（本批骨架，外部调用实现留批 4）。

        Args:
            url: 小红书分享链接（含 xsec_token）
            batch_id: 批次 id（同一次扒图共享）
            storage_dir: 存储目录（缺省用构造时的值）

        Returns:
            ConnectorResult: ok=True + data（图片路径/元数据）/ ok=False + note（降级）
        """
        if not self.available:
            return ConnectorResult(
                ok=False,
                note="小红书扒图 vendor 未配置（XHS-Downloader 目录不存在）",
            )

        # 本批骨架：真实调用实现留批 4
        # TODO: vendor/XHS-Downloader（uv run python -c 子进程调用）
        # TODO: 元数据：sqlite3 读 ExploreData.db
        # TODO: 元数据失败降级不阻塞

        target_dir = storage_dir or self._storage_dir
        return ConnectorResult(
            ok=False,
            note="XHS connector 骨架（批 4 实现真实下载）",
            data={
                "url": url,
                "batch_id": batch_id,
                "storage_dir": target_dir,
            },
        )


def _factory(ctx: Any) -> XHSConnector:
    """工厂函数：注册到 CONNECTORS 注册表。"""
    return XHSConnector()


# 模块加载时注册
register_connector(_CONNECTOR_ID, _factory)