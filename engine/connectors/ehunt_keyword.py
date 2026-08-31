"""engine/connectors/ehunt_keyword.py：eHunt CDP 9222 逐词指标（详设-v0.5 §5.2）。

适配改造广成 runtime/ehunt_keyword_connector.py，真实调用实现。

连接器特点：
- playwright connect_over_cdp(9222) 复用已开 keyword-tool 页
- 逐词 fill+Enter → 等 article:has-text('竞争度') + 1500ms → 读 innerText 按字段标签正则解析
- 失败降级 ok=False，工序跳过逐词指标记 metrics_note（不 failed）
- 页面结构改版即失效 → 回归 + 人工兜底（风险提示写进详设 §14）
"""

from __future__ import annotations

import re
from typing import Any, Callable

from engine.connectors import ConnectorResult, register_connector

__all__ = ["EHuntKeywordConnector"]

_CONNECTOR_ID = "ehunt_keyword"
_CDP_URL = "http://127.0.0.1:9222"
_KEYWORD_TOOL_URL = "https://ehunt.ai/cn/keyword-tool"

# 指标卡文本格式（innerText，标签即分隔符）：
#   name necklace频率13竞争度225.1K浏览量总76.4M月26.7M收藏量总2.2M月3.9K
#   销量总1.9M月5.1K分数3.39Google PD100Google CPC$2.04 历史趋势
_NUM = r"NR|[\d.,]+[KMB]?"
_CARD_RE = re.compile(
    r"^(?P<keyword>.+?)"
    r"频率(?P<frequency>[\d,]+)"
    r"竞争度(?P<competition>" + _NUM + r")"
    r"浏览量总(?P<views_total>" + _NUM + r")月(?P<views_month>" + _NUM + r")"
    r"收藏量总(?P<favorites_total>" + _NUM + r")月(?P<favorites_month>" + _NUM + r")"
    r"销量总(?P<sales_total>" + _NUM + r")月(?P<sales_month>" + _NUM + r")"
    r"分数(?P<score>NR|[\d.]+)"
    r"Google PD(?P<google_pd>NR|\d+)"
    r"Google CPC(?P<google_cpc>NR|\$[\d.,]+)",
    re.DOTALL,
)

_MULTIPLIER = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def parse_number(text: str) -> int | float | None:
    """'225.1K'->225100 / '76.4M'->76400000 / '918'->918 / 'NR'->None。"""
    value = str(text).strip()
    if not value or value.upper() == "NR":
        return None
    multiplier = 1
    if value[-1].upper() in _MULTIPLIER:
        multiplier = _MULTIPLIER[value[-1].upper()]
        value = value[:-1]
    try:
        number = float(value.replace(",", ""))
    except ValueError:
        return None
    scaled = number * multiplier
    return int(scaled) if scaled == int(scaled) else scaled


def parse_card_text(text: str) -> dict[str, Any] | None:
    """解析一张指标卡文本为指标 dict；不匹配返回 None。"""
    match = _CARD_RE.match(str(text).strip())
    if not match:
        return None
    groups = match.groupdict()
    card: dict[str, Any] = {"keyword": groups["keyword"].strip()}
    for field in (
        "competition",
        "views_total",
        "views_month",
        "favorites_total",
        "favorites_month",
        "sales_total",
        "sales_month",
    ):
        card[field] = parse_number(groups[field])
    try:
        card["frequency"] = int(groups["frequency"].replace(",", ""))
    except ValueError:
        card["frequency"] = None
    score = groups["score"]
    card["score"] = None if score == "NR" else float(score)
    pd = groups["google_pd"]
    card["google_pd"] = None if pd == "NR" else int(pd)
    cpc = groups["google_cpc"]
    card["google_cpc"] = None if cpc == "NR" else float(cpc.lstrip("$").replace(",", ""))
    return card


class EHuntKeywordConnector:
    """eHunt 关键词工具页连接器：CDP 读页，逐词采集搜索量/竞争度等指标。"""

    def __init__(
        self,
        cdp_url: str = _CDP_URL,
        transport: Callable[[str], list[str]] | None = None,
        timeout: int = 30,
        max_related: int = 5,
    ) -> None:
        self._cdp_url = cdp_url
        self._transport = transport
        self._timeout = timeout
        self._max_related = max_related

    @property
    def available(self) -> bool:
        """真实路径探测 CDP 端口；注入传输层时恒可用。"""
        if self._transport is not None:
            return True
        try:
            import urllib.request

            with urllib.request.urlopen(f"{self._cdp_url}/json/version", timeout=3) as response:
                return bool(response.read())
        except Exception:  # noqa: BLE001 - 端口不通即为不可用
            return False

    async def fetch_keyword_metrics(
        self,
        keywords: list[str],
    ) -> ConnectorResult:
        """获取关键词逐词指标（照搬广成 ehunt_keyword_connector.py 逻辑）。

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

        # 两道闸：8 词硬顶
        truncated_keywords = keywords[:8]

        cards_by_keyword: dict[str, list[dict[str, Any]]] = {}
        if self._transport is not None:
            for keyword in truncated_keywords:
                cards_by_keyword[keyword] = [
                    parsed
                    for text in self._transport(keyword)
                    if (parsed := parse_card_text(text)) is not None
                ]
        else:
            session = self._open_session()
            if session is None:
                return ConnectorResult(
                    ok=False,
                    note="CDP 连接失败（需先启动调试 Chrome：scripts/ehunt-chrome.sh）",
                )
            try:
                for keyword in truncated_keywords:
                    cards_by_keyword[keyword] = session.search(keyword)
            except Exception as exc:  # noqa: BLE001 - 读页异常统一降级为 note
                return ConnectorResult(
                    ok=False,
                    note=f"CDP 读页失败：{exc}",
                )
            finally:
                session.close()

        result: dict[str, Any] = {"ok": True, "keywords": {}}
        for keyword, cards in cards_by_keyword.items():
            exact = next(
                (c for c in cards if c["keyword"].strip().lower() == keyword.lower()), None
            )
            if exact is None:
                result["keywords"][keyword] = {
                    "keyword": keyword,
                    "note": "页面未返回该词的精确指标卡。",
                    "related": cards[: self._max_related],
                }
                continue
            related = [c for c in cards if c is not exact][: self._max_related]
            result["keywords"][keyword] = {**exact, "related": related}

        return ConnectorResult(
            ok=True,
            data=result,
        )

    # ── 内部：CDP 会话 ────────────────────────────────────

    def _open_session(self) -> "_CdpSession | None":
        try:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            browser = playwright.chromium.connect_over_cdp(self._cdp_url)
            return _CdpSession(browser, playwright, self._timeout)
        except Exception as exc:  # noqa: BLE001 - 连不上 CDP 即不可用
            return None


class _CdpSession:
    """CDP 页面会话：复用已开的 keyword-tool 页，逐词搜索并解析指标卡。"""

    _SEARCH_SELECTORS = (
        'input[placeholder*="关键词"]',
        'input[placeholder*="keyword"]',
        'input[placeholder*="Keyword"]',
        "input[type=text]",
        "input:not([type])",
    )

    def __init__(self, browser, playwright, timeout: int) -> None:
        self._browser = browser
        self._playwright = playwright
        self._timeout = timeout
        self._page = None

    def search(self, keyword: str) -> list[dict[str, Any]]:
        page = self._ensure_page()
        box = self._find_search_box(page)
        box.fill(keyword)
        page.keyboard.press("Enter")
        # 指标卡渲染完成标志：出现含字段标签的 article
        page.wait_for_selector("article:has-text('竞争度')", timeout=self._timeout * 1000)
        page.wait_for_timeout(1500)  # 等相关词卡片补齐
        cards = [
            parsed
            for text in page.locator("article").all_inner_texts()
            if (parsed := parse_card_text(text)) is not None
        ]
        if cards:
            return cards
        return self._read_table_fallback(page)

    def close(self) -> None:
        try:
            # connect_over_cdp 下 close() 仅断开连接，不动常驻的登录态 Chrome
            self._browser.close()
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        context = self._browser.contexts[0]
        for existing in context.pages:
            if "keyword-tool" in (existing.url or ""):
                self._page = existing
                return self._page
        page = context.new_page()
        page.goto(_KEYWORD_TOOL_URL, wait_until="domcontentloaded", timeout=self._timeout * 1000)
        page.wait_for_selector("article, table", timeout=self._timeout * 1000)
        self._page = page
        return self._page

    def _find_search_box(self, page):
        for selector in self._SEARCH_SELECTORS:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                return locator
        return page.locator("input").first

    @staticmethod
    def _read_table_fallback(page) -> list[dict[str, Any]]:
        """窄窗指标卡不可见时兜底：读宽屏表格行（列序 2026-08-16 钉死）。

        列：0 勾选 1 收藏 2 关键词 3 频率 4 竞争度 5/6 浏览量总/月
            7/8 收藏量总/月 9/10 销量总/月 11 分数 12 Google PD 13 Google CPC
        """
        rows: list[dict[str, Any]] = []
        for table_index in range(page.locator("table").count()):
            table = page.locator("table").nth(table_index)
            body_rows = table.locator("tbody tr")
            for row_index in range(body_rows.count()):
                cells = [c.strip() for c in body_rows.nth(row_index).locator("td").all_inner_texts()]
                if len(cells) < 14 or not cells[2]:
                    continue
                rows.append(
                    {
                        "keyword": cells[2],
                        "frequency": parse_number(cells[3]),
                        "competition": parse_number(cells[4]),
                        "views_total": parse_number(cells[5]),
                        "views_month": parse_number(cells[6]),
                        "favorites_total": parse_number(cells[7]),
                        "favorites_month": parse_number(cells[8]),
                        "sales_total": parse_number(cells[9]),
                        "sales_month": parse_number(cells[10]),
                        "score": parse_number(cells[11]),
                        "google_pd": parse_number(cells[12]),
                        "google_cpc": parse_number(cells[13].lstrip("$")),
                    }
                )
        return rows


def _factory(ctx: Any) -> EHuntKeywordConnector:
    """工厂函数：注册到 CONNECTORS 注册表。"""
    return EHuntKeywordConnector()


# 模块加载时注册
register_connector(_CONNECTOR_ID, _factory)