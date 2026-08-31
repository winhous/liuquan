"""engine/connectors/ehunt_keyword.py：eHunt CDP 9222 逐词指标（详设-v0.5 §5.2）。

适配改造广成 runtime/ehunt_keyword_connector.py，骨架与可用性检查。
外部真实调用实现留批 2/批 3（SEO 工具工序）。

连接器特点：
- playwright connect_over_cdp(9222) 复用已开 keyword-tool 页
- 逐词 fill+Enter → 等 article:has-text('竞争度') + 1500ms → 读 innerText 按字段标签正则解析
- 失败降级 ok=False，工序跳过逐词指标记 metrics_note（不 failed）
- 页面结构改版即失效 → 回归 + 人工兜底（风险提示写进详设 §14）
"""

from __future__ import annotations

import os
from typing import Any

from engine.connectors import ConnectorResult, register_connector

__all__ = ["EHuntKeywordConnector"]

_CONNECTOR_ID = "ehunt_keyword"
_CDP_PORT = 9222


class EHuntKeywordConnector:
    """eHunt CDP 通道（逐词指标）。

    本批只做骨架与可用性检查，外部真实调用实现留批 2/批 3。
    """

    def __init__(self) -> None:
        pass

    @property
    def available(self) -> bool:
        """检查连接器是否可用（CDP 9222 端口可连接）。

        本批骨架：固定返回 False（真实检测留批 2/批 3）。
        """
        # TODO: 批 2 实现真实 CDP 连接检测
        return False

    async def fetch_keyword_metrics(
        self,
        keywords: list[str],
    ) -> ConnectorResult:
        """获取关键词逐词指标（本批骨架，外部调用实现留批 2/批 3）。

        Args:
            keywords: 关键词列表

        Returns:
            ConnectorResult: ok=True + data（逐词指标）/ ok=False + note（降级）
        """
        if not self.available:
            return ConnectorResult(
                ok=False,
                note="eHunt CDP 通道不可用（Chrome 9222 未启动或未登录）",
            )

        # 本批骨架：真实调用实现留批 2/批 3
        # TODO: playwright connect_over_cdp(9222)
        # TODO: 逐词 fill+Enter → 等 article:has-text('竞争度') + 1500ms
        # TODO: 读 innerText 按字段标签正则解析
        # TODO: 失败降级 ok=False，不抛穿链

        return ConnectorResult(
            ok=False,
            note="eHunt Keyword connector 骨架（批 2 实现真实调用）",
            data={"keywords": keywords},
        )


def _factory(ctx: Any) -> EHuntKeywordConnector:
    """工厂函数：注册到 CONNECTORS 注册表。"""
    return EHuntKeywordConnector()


# 模块加载时注册
register_connector(_CONNECTOR_ID, _factory)