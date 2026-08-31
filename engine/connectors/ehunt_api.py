"""engine/connectors/ehunt_api.py：eHunt API 通道（详设-v0.5 §5.1）。

照搬广成 runtime/ehunt_connector.py，真实 HTTP 调用实现。

连接器特点：
- POST api.ehunt.ai/api/v1/items，X-VIP-TOKEN 鉴权
- UA 伪装必做（eHunt WAF 拦 python-urllib 默认 UA 直接 403）
- 两道闸：8 词硬顶（keywords[:8]）+ page_size≤100（默认 10）
- 429 = 当日配额耗尽 → 降级 {ok: False, note: "配额耗尽…"}，不抛穿链
- 记账：透传服务端 quota 回显（详设 §9），无本地账本
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from engine.connectors import ConnectorResult, register_connector

__all__ = ["EHuntAPIConnector"]

_CONNECTOR_ID = "ehunt_api"
_API_KEY_ENV = "EHUNT_API_KEY"
_API_BASE = "https://api.ehunt.ai"
_ITEMS_PATH = "/api/v1/items"


class EHuntAPIConnector:
    """eHunt API 通道（竞品画像）。

    照搬广成 ehunt_connector.py，真实 HTTP 调用实现。
    """

    def __init__(
        self,
        api_key: str | None = None,
        transport: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        timeout: int = 30,
    ) -> None:
        self._api_key = api_key or os.environ.get(_API_KEY_ENV, "").strip()
        self._transport = transport
        self._timeout = timeout

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
        """搜索竞品画像（照搬广成 ehunt_connector.py 逻辑）。

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

        # 两道闸：8 词硬顶 + page_size≤100
        truncated_keywords = keywords[:8]
        clamped_page_size = min(max(1, page_size), 100)

        result: dict[str, Any] = {"ok": True, "keywords": {}, "quota": None}
        for keyword in truncated_keywords:
            payload = {
                "search_key": keyword,
                "sort_by": 2,  # 总销量降序，头部竞品最有参考价值
                "desc": 1,
                "page_num": 1,
                "page_size": clamped_page_size,
            }
            response = self._post(payload)
            if not response.get("ok"):
                return ConnectorResult(
                    ok=False,
                    note=str(response.get("note") or "请求失败"),
                )
            body = response.get("body") or {}
            data_block = body.get("data") or {}
            result["keywords"][keyword] = self._summarize(keyword, data_block)
            if isinstance(body.get("quota"), dict):
                result["quota"] = body["quota"]

        return ConnectorResult(
            ok=True,
            data=result,
        )

    # ── 内部 ─────────────────────────────────────────────

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发送 POST 请求（照搬广成逻辑）。"""
        try:
            if self._transport:
                body = self._transport(_ITEMS_PATH, payload)
            else:
                body = self._http_post(_ITEMS_PATH, payload)
        except Exception as exc:  # noqa: BLE001 - 网络异常统一降级为 note
            return {"ok": False, "note": f"eHunt 请求失败：{exc}"}
        code = body.get("code")
        if code != 200:
            note = f"eHunt 返回 code={code}: {body.get('message', '')}"
            # 429 = 当日配额耗尽
            if code == 429:
                note = f"eHunt 配额耗尽：今日已用 {body.get('quota', {}).get('used_today', '?')}/200"
            return {"ok": False, "note": note}
        return {"ok": True, "body": body}

    def _http_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """真实 HTTP POST 请求（照搬广成逻辑）。"""
        token = os.environ["EHUNT_API_KEY"].strip()
        request = urllib.request.Request(
            f"{_API_BASE}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "X-VIP-TOKEN": token,
                "Content-Type": "application/json",
                # eHunt WAF 拦 urllib 默认 UA(python-urllib/* 直接 403)
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) guangcheng-ehunt-connector/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _summarize(keyword: str, data_block: dict[str, Any]) -> dict[str, Any]:
        """把单关键词的返回聚成画像：总量 + 均价 + top 竞品（照搬广成逻辑）。"""
        items = [item for item in data_block.get("list") or [] if isinstance(item, dict)]
        top_items = [
            {
                "title": str(item.get("title", "")),
                "price": item.get("price"),
                "sales_total": item.get("sales_total"),
                "reviews": item.get("reviews"),
                "favorites": item.get("favorites"),
                "tags": [t.strip() for t in str(item.get("tags", "")).split(",") if t.strip()],
                "store_name": str(item.get("store_name", "")),
            }
            for item in items[:5]
        ]
        prices = [float(item["price"]) for item in items if isinstance(item.get("price"), (int, float))]
        return {
            "keyword": keyword,
            "product_num": data_block.get("product_num"),  # 匹配商品总数，竞争度代理
            "avg_price_top": round(sum(prices) / len(prices), 2) if prices else None,
            "top_competitors": top_items,
        }


def _factory(ctx: Any) -> EHuntAPIConnector:
    """工厂函数：注册到 CONNECTORS 注册表。"""
    return EHuntAPIConnector()


# 模块加载时注册
register_connector(_CONNECTOR_ID, _factory)