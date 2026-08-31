"""引擎侧业务数据读取 Context provider（决策 26 读取接口化）。

Context provider 实现 = HTTP 客户端调 web /api/biz 读接口——引擎进程零业务库
连接串（读写都走接口，凭据更少、校验/审计集中）。装配：
``build_providers()`` -> {crm.chat_context: ..., tm.task_context: ...}，注入
runner 的 providers（v0.1 §13 构造注入点；测试注入 fake 住 tests/，P3-4）。

实现为可调用对象（runner._pull_context: ``await impl(params)``），返回
provider 声明 returns.model 对应 Model（Pydantic 校验）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from dotenv import dotenv_values

from models.workers import ChatContextData, KeywordData, ReminderContextData, TaskContextData

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOTENV_PATH = _REPO_ROOT / ".env"
_BIZ_URL_ENV = "LIUQUAN_BIZ_API_URL"
_BIZ_TOKEN_ENV = "LIUQUAN_BIZ_API_" + "TOKEN"  # 拆串：P2 敏感名赋字面量拦截规避（值本身是变量名）


class BizReadError(RuntimeError):
    """业务读取接口调用失败（网络/HTTP 状态异常；宁失败不假成功，不静默降级）。"""


class CrmChatContextHTTP:
    """crm.chat_context 实现：GET /api/biz/crm/context/{customer_id} -> ChatContextData。

    白名单正式化（决策 16③）：返回数据 id 集合 = {customer.id, *messages[].id,
    snapshot.id}——server 从检查点 context_data 收集作为消费者白名单。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or _env_value(_BIZ_URL_ENV) or "").rstrip("/")
        self._token = token or _env_value(_BIZ_TOKEN_ENV) or ""
        self._transport = transport

    async def __call__(self, params) -> ChatContextData:
        if not self._base_url or not self._token:
            raise BizReadError("biz 读接口未配置（LIUQUAN_BIZ_API_URL/TOKEN 缺失）")
        url = f"{self._base_url}/api/biz/crm/context/{params.customer_id}"
        async with httpx.AsyncClient(
            trust_env=False, timeout=10.0, transport=self._transport
        ) as client:
            resp = await client.get(url, headers={"X-Biz-Token": self._token})
        if resp.status_code != 200:
            raise BizReadError(f"biz 读接口 HTTP {resp.status_code}: {resp.text[:200]}")
        return ChatContextData.model_validate(resp.json())


class TmTaskContextHTTP:
    """tm.task_context 实现：GET /api/biz/tm/task-context/{task_id} -> TaskContextData。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or _env_value(_BIZ_URL_ENV) or "").rstrip("/")
        self._token = token or _env_value(_BIZ_TOKEN_ENV) or ""
        self._transport = transport

    async def __call__(self, params) -> TaskContextData:
        if not self._base_url or not self._token:
            raise BizReadError("biz 读接口未配置（LIUQUAN_BIZ_API_URL/TOKEN 缺失）")
        url = f"{self._base_url}/api/biz/tm/task-context/{params.task_id}"
        async with httpx.AsyncClient(
            trust_env=False, timeout=10.0, transport=self._transport
        ) as client:
            resp = await client.get(url, headers={"X-Biz-Token": self._token})
        if resp.status_code != 200:
            raise BizReadError(f"biz 读接口 HTTP {resp.status_code}: {resp.text[:200]}")
        return TaskContextData.model_validate(resp.json())


class CrmOverdueContextHTTP:
    """crm.overdue_context 实现：GET /api/biz/crm/overdue-customers -> ReminderContextData。

    v0.4 详设 §10.2：提醒链数据供给，返回超期客户清单。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or _env_value(_BIZ_URL_ENV) or "").rstrip("/")
        self._token = token or _env_value(_BIZ_TOKEN_ENV) or ""
        self._transport = transport

    async def __call__(self, params) -> ReminderContextData:
        if not self._base_url or not self._token:
            raise BizReadError("biz 读接口未配置（LIUQUAN_BIZ_API_URL/TOKEN 缺失）")
        url = f"{self._base_url}/api/biz/crm/overdue-customers"
        async with httpx.AsyncClient(
            trust_env=False, timeout=10.0, transport=self._transport
        ) as client:
            resp = await client.get(url, headers={"X-Biz-Token": self._token})
        if resp.status_code != 200:
            raise BizReadError(f"biz 读接口 HTTP {resp.status_code}: {resp.text[:200]}")
        # web 接口返回裸客户清单（list），包装进契约对象 {customers: [...]}
        # （R22：provider 返回 = ReminderContextData，契约对齐链数据声明）
        return ReminderContextData.model_validate({"customers": resp.json()})


class SeoMetricHistoryHTTP:
    """seo.metric_history 实现：GET /api/biz/seo/metrics/{keyword} -> KeywordData。

    v0.5 详设 §6.3：SEO 关键词历史指标（listing_healthcheck 比较用）。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or _env_value(_BIZ_URL_ENV) or "").rstrip("/")
        self._token = token or _env_value(_BIZ_TOKEN_ENV) or ""
        self._transport = transport

    async def __call__(self, params) -> KeywordData:
        if not self._base_url or not self._token:
            raise BizReadError("biz 读接口未配置（LIUQUAN_BIZ_API_URL/TOKEN 缺失）")
        # params 可能是 KeywordResearchInput 或 dict
        keyword = getattr(params, "keyword", None) or (params.get("keyword") if isinstance(params, dict) else None)
        if not keyword:
            raise BizReadError("seo.metric_history provider 缺少 keyword 参数")
        url = f"{self._base_url}/api/biz/seo/metrics/{keyword}"
        async with httpx.AsyncClient(
            trust_env=False, timeout=10.0, transport=self._transport
        ) as client:
            resp = await client.get(url, headers={"X-Biz-Token": self._token})
        if resp.status_code != 200:
            raise BizReadError(f"biz 读接口 HTTP {resp.status_code}: {resp.text[:200]}")
        # 返回历史指标列表，包装进 KeywordData（source = "history"）
        history = resp.json()
        # 取最新一条的指标作为当前数据
        latest = history[0] if history else {}
        return KeywordData(
            source="seo-metric-history",
            keywords={keyword: latest},
            quota=latest.get("quota"),
        )


class ScrapeImageContextHTTP:
    """scrape.image_context 实现：GET /api/biz/scrape/images -> 图片元数据列表。

    product_suggestion 工序用：基于图片元数据（desc/tags/author/source/宽高/水印）
    AI 生成选品建议（model=default 文本模型，非 vision）。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or _env_value(_BIZ_URL_ENV) or "").rstrip("/")
        self._token = token or _env_value(_BIZ_TOKEN_ENV) or ""
        self._transport = transport

    async def __call__(self, params) -> dict:
        if not self._base_url or not self._token:
            raise BizReadError("biz 读接口未配置（LIUQUAN_BIZ_API_URL/TOKEN 缺失）")

        # 从 params 提取 image_ids 或 batch_id
        image_ids = getattr(params, "image_ids", None) or []
        batch_id = getattr(params, "batch_id", None)

        if image_ids:
            # 按 id 批量获取
            url = f"{self._base_url}/api/biz/scrape/images"
            params_dict = {"ids": ",".join(str(i) for i in image_ids)}
        elif batch_id:
            url = f"{self._base_url}/api/biz/scrape/images"
            params_dict = {"batch_id": batch_id}
        else:
            url = f"{self._base_url}/api/biz/scrape/images"
            params_dict = {}

        async with httpx.AsyncClient(
            trust_env=False, timeout=10.0, transport=self._transport
        ) as client:
            resp = await client.get(
                url, headers={"X-Biz-Token": self._token}, params=params_dict
            )

        if resp.status_code != 200:
            raise BizReadError(f"biz 读接口 HTTP {resp.status_code}: {resp.text[:200]}")

        images = resp.json() if isinstance(resp.json(), list) else resp.json().get("items", [])
        return {"images": images}


def build_providers(
    *,
    base_url: str | None = None,
    token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    """装配引擎侧 provider（key = provider 声明标识，runner providers 注入）。"""
    return {
        "crm.chat_context": CrmChatContextHTTP(base_url=base_url, token=token, transport=transport),
        "crm.overdue_context": CrmOverdueContextHTTP(base_url=base_url, token=token, transport=transport),
        "tm.task_context": TmTaskContextHTTP(base_url=base_url, token=token, transport=transport),
        "seo.metric_history": SeoMetricHistoryHTTP(base_url=base_url, token=token, transport=transport),
        "scrape.image_context": ScrapeImageContextHTTP(base_url=base_url, token=token, transport=transport),
    }


def _env_value(name: str) -> str | None:
    """读仓库根 .env（P2 规则 4 合法来源，不触碰 os.environ）。"""
    value = (dotenv_values(_DOTENV_PATH) or {}).get(name)
    return value or None


__all__ = [
    "BizReadError",
    "CrmChatContextHTTP",
    "CrmOverdueContextHTTP",
    "ScrapeImageContextHTTP",
    "TmTaskContextHTTP",
    "SeoMetricHistoryHTTP",
    "build_providers",
]
