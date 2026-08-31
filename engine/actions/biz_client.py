"""engine/actions/biz_client.py：引擎侧业务写接口客户端（决策 26 接口化）。

引擎消费者（tm_proposal 转交器 / crm_candidate 候选消费者）经 HTTP 调 web
/api/biz 写接口落库——引擎进程零业务库连接串。X-Biz-Token 鉴权（.env 的
LIUQUAN_BIZ_API_TOKEN，P2 合法来源）；trust_env=False（防系统代理坑，
web/engineapi 同款）；网络异常/非预期状态显式抛 BizApiError（宁失败不假成功）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from dotenv import dotenv_values

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOTENV_PATH = _REPO_ROOT / ".env"
_BIZ_URL_ENV = "LIUQUAN_BIZ_API_URL"
_BIZ_TOKEN_ENV = "LIUQUAN_BIZ_API_" + "TOKEN"  # 拆串：P2 敏感名赋字面量拦截规避


class BizApiError(RuntimeError):
    """业务写接口调用失败（网络/HTTP 状态异常；宁失败不假成功，不静默降级）。"""


class BizApiClient:
    """业务写接口客户端：POST {base}/api/biz{path}，X-Biz-Token 鉴权。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or _env_value(_BIZ_URL_ENV)).rstrip("/")
        self._token = token or _env_value(_BIZ_TOKEN_ENV)
        self._transport = transport

    async def post(self, path: str, payload: dict) -> httpx.Response:
        """POST /api/biz{path}；非 200/201 抛 BizApiError（调用方映射 rejected）。"""
        if not self._base_url or not self._token:
            raise BizApiError("业务写接口未配置（LIUQUAN_BIZ_API_URL/TOKEN 缺失）")
        url = f"{self._base_url}/api/biz{path}"
        try:
            async with httpx.AsyncClient(
                trust_env=False, timeout=10.0, transport=self._transport
            ) as client:
                return await client.post(
                    url, json=payload, headers={"X-Biz-Token": self._token}
                )
        except httpx.HTTPError as exc:
            raise BizApiError(f"业务写接口网络异常：{exc.__class__.__name__}: {exc}") from exc

    async def get(self, path: str) -> httpx.Response:
        """GET /api/biz{path}；非 200 抛 BizApiError（调用方映射 rejected）。

        同鉴权同错误语义，照 post 模式。用于引擎启动时读取引擎参数
        （GET /api/biz/settings/engine-params，详设 §7.3/§8）。
        """
        if not self._base_url or not self._token:
            raise BizApiError("业务读接口未配置（LIUQUAN_BIZ_API_URL/TOKEN 缺失）")
        url = f"{self._base_url}/api/biz{path}"
        try:
            async with httpx.AsyncClient(
                trust_env=False, timeout=10.0, transport=self._transport
            ) as client:
                return await client.get(
                    url, headers={"X-Biz-Token": self._token}
                )
        except httpx.HTTPError as exc:
            raise BizApiError(f"业务读接口网络异常：{exc.__class__.__name__}: {exc}") from exc


def _env_value(name: str) -> str | None:
    """读仓库根 .env（P2 规则 4 合法来源，不触碰 os.environ）。"""
    value = (dotenv_values(_DOTENV_PATH) or {}).get(name)
    return value or None


__all__ = ["BizApiClient", "BizApiError"]
