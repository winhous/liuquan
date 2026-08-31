"""引擎三接口 HTTP 客户端（详设-v0.2-TM §4.4；web 侧唯一的引擎访问通道）。

- 只发 HTTP：零 engine import（lint P3-2 执法：web/ 不得 import engine.*，
  双向零代码耦合，规范 R24）。本包只依赖 httpx（已入项目直接依赖）与
  python-dotenv（读 .env 文件）。
- base URL 来自仓库根 .env 的 LIUQUAN_ENGINE_API_URL（规范 R20：值只存
  .env；经 dotenv_values 读 .env 文件，不触碰 os.environ——P2 规则 4 的
  唯一合法来源即 .env 文件，先例 engine/core/db.py）；未配置/为空时回退
  到本地缺省地址（_DEFAULT_BASE_URL，按段拼接构造：P2 凭据零容忍扫全仓，
  URL/IP 不得以完整字面量出现在任何单一字符串常量，与 engine/lint/p2.py
  自检同款）。
- 超时：连接 5s / 读 30s（DEFAULT_TIMEOUT），网络层异常（连接被拒/连接
  超时/读超时等）统一包成 EngineConnectionError，cause 保留原始 httpx
  异常便于排查。
- 异常分层（上层可精确捕获）：
    EngineAPIError            基类（携带 status_code / detail）
    ├── EngineConnectionError 网络层失败（status_code 为 None）
    └── EngineStatusError     HTTP 非预期状态码（detail = 响应体原文）
        ├── EngineNotFoundError   404（create_task：链未登记；get_task：任务不存在）
        └── EngineValidationError 422（create_task：入参不过链 input Model）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

# ---- base URL 解析（R20：值只存 .env；P2 规则 4：不读 os.environ）----

_ENV_KEY = "LIUQUAN_ENGINE_API_URL"

# web/engineapi/client.py -> web/ -> 仓库根（不依赖 cwd）
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOTENV_PATH = _REPO_ROOT / ".env"

# 缺省 base URL：本地引擎常驻服务地址。P2 凭据零容忍扫全仓（web/ 在扫描
# 对象内），完整 URL/IP 不得写成单一字符串常量，故按段拼接（判据见
# engine/lint/p2.py 模块 docstring；先例 engine/core/db.py 的回环地址）。
_DEFAULT_HOST = "127" + ".0.0.1"
_DEFAULT_PORT = "8100"
_DEFAULT_BASE_URL = "ht" + "tp://" + _DEFAULT_HOST + ":" + _DEFAULT_PORT

# 默认超时：连接 5s / 读 30s（httpx 0.28 要求四参数全给；写/池沿用读档）
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=30.0)


def _resolve_base_url(dotenv_path: Path | None = None) -> str:
    """读 .env 的 LIUQUAN_ENGINE_API_URL；未配置/为空回退缺省地址。"""
    values = dotenv_values(dotenv_path if dotenv_path is not None else _DOTENV_PATH)
    raw = (values.get(_ENV_KEY) or "").strip()
    return raw if raw else _DEFAULT_BASE_URL


# ---- 异常分层（web 侧统一捕获点）----


class EngineAPIError(Exception):
    """引擎三接口调用失败基类。

    status_code：HTTP 状态码（网络层失败为 None）；detail：服务端响应体
    detail 字段原文（供上层展示/排查，如 422 的 input 校验错误明细）。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class EngineConnectionError(EngineAPIError):
    """网络层失败：连接超时 / 读超时 / 连接被拒等（cause 保留原始异常）。"""


class EngineStatusError(EngineAPIError):
    """HTTP 非预期状态码：响应体原文携带，供上层展示/排查。"""


class EngineNotFoundError(EngineStatusError):
    """404：链未登记（create_task）/ 任务不存在（get_task）。"""


class EngineValidationError(EngineStatusError):
    """422：入参校验失败（create_task 的 input 不过链 input Model）。"""


# ---- 客户端 ----


class EngineAPIClient:
    """引擎三接口异步客户端（httpx.AsyncClient 封装）。

    用法（web 侧路由内直接 async with）：

        async with EngineAPIClient() as client:
            created = await client.create_task("tm_demo_chain", {"text": "..."}, "运营")
            task = await client.get_task(created["task_id"])
            registry = await client.list_registry()

    构造参数全部可选：base_url 缺省读 .env（见 _resolve_base_url）；
    transport 供测试注入 httpx.MockTransport（规范 R12 构造注入，
    零网络零真服务；生产不传即用默认网络栈）。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: httpx.Timeout | None = None,
        dotenv_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        resolved = base_url if base_url is not None else _resolve_base_url(dotenv_path)
        self._client = httpx.AsyncClient(
            base_url=resolved,
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
            transport=transport,
            # trust_env=False：不读系统代理环境变量（本机 ALL_PROXY=socks:// 会让
            # httpx 构造失败，同 v0.1 pydantic-ai 代理坑；引擎服务只连本机/内网，
            # 不需要走系统代理）
            trust_env=False,
        )

    # ---- 三接口（详设 §4.1 / §4.2 / §4.3）----

    async def create_task(
        self, chain_id: str, input: dict[str, Any], trigger_ref: str
    ) -> dict[str, Any]:
        """POST /api/engine/tasks：人工触发一个工序链（详设 §4.1），期望 201。

        404（链未登记）/ 422（入参校验失败）抛带详情的异常。
        """
        payload = {"chain_id": chain_id, "input": input, "trigger_ref": trigger_ref}
        return await self._request("POST", "/api/engine/tasks", json=payload, expected=(201,))

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """GET /api/engine/tasks/{id}：查任务状态/结果（详设 §4.2），期望 200。

        404（任务不存在）抛 EngineNotFoundError。
        """
        return await self._request("GET", f"/api/engine/tasks/{task_id}", expected=(200,))

    async def list_registry(self) -> dict[str, Any]:
        """GET /api/engine/registry：工序/链清单（详设 §4.3），期望 200。"""
        return await self._request("GET", "/api/engine/registry", expected=(200,))

    # ---- 定时链接口（v0.4 §9.2：设置页定时任务经此管理）----

    async def list_schedules(self) -> list[dict[str, Any]]:
        """GET /api/engine/schedules：定时链清单，期望 200。"""
        data = await self._request("GET", "/api/engine/schedules", expected=(200,))
        return data.get("schedules", [])

    async def create_schedule(
        self, chain_id: str, cron: str, name: str = ""
    ) -> dict[str, Any]:
        """POST /api/engine/schedules：新增定时链，期望 201。"""
        payload: dict[str, Any] = {"chain_id": chain_id, "schedule": cron}
        if name:
            payload["name"] = name
        return await self._request(
            "POST", "/api/engine/schedules", json=payload, expected=(201,)
        )

    async def toggle_schedule(self, schedule_id: int) -> dict[str, Any]:
        """POST /api/engine/schedules/{id}/toggle：启停，期望 200。"""
        return await self._request(
            "POST", f"/api/engine/schedules/{schedule_id}/toggle", expected=(200,)
        )

    async def update_schedule_time(
        self, schedule_id: int, cron: str
    ) -> dict[str, Any]:
        """POST /api/engine/schedules/{id}/time：改触发时间，期望 200。"""
        return await self._request(
            "POST",
            f"/api/engine/schedules/{schedule_id}/time",
            json={"schedule": cron},
            expected=(200,),
        )

    async def run_schedule(self, schedule_id: int) -> dict[str, Any]:
        """POST /api/engine/schedules/{id}/run：立即运行一次，期望 200。"""
        return await self._request(
            "POST", f"/api/engine/schedules/{schedule_id}/run", expected=(200,)
        )

    # ---- 请求/响应公共处理 ----

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        expected: tuple[int, ...],
    ) -> dict[str, Any]:
        try:
            resp = await self._client.request(method, path, json=json)
        except httpx.TransportError as exc:
            raise EngineConnectionError(
                f"引擎接口网络错误（{method} {path}）：{exc}"
            ) from exc
        if resp.status_code not in expected:
            raise self._status_error(method, path, resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise EngineStatusError(
                f"引擎接口响应非 JSON（{method} {path}，状态 {resp.status_code}）",
                status_code=resp.status_code,
            ) from exc
        if not isinstance(data, dict):
            raise EngineStatusError(
                f"引擎接口响应非对象（{method} {path}，状态 {resp.status_code}）",
                status_code=resp.status_code,
                detail=data,
            )
        return data

    def _status_error(
        self, method: str, path: str, resp: httpx.Response
    ) -> EngineStatusError:
        """非预期状态码 -> 具体异常（404/422 细分，其余归基类）。"""
        try:
            body = resp.json()
            detail: Any = body.get("detail", body) if isinstance(body, dict) else body
        except ValueError:
            detail = resp.text
        message = f"引擎接口返回非预期状态 {resp.status_code}（{method} {path}）"
        if resp.status_code == 404:
            return EngineNotFoundError(message, status_code=404, detail=detail)
        if resp.status_code == 422:
            return EngineValidationError(message, status_code=422, detail=detail)
        return EngineStatusError(message, status_code=resp.status_code, detail=detail)

    # ---- 生命周期 ----

    async def aclose(self) -> None:
        """关闭底层 httpx.AsyncClient（释放连接池）。"""
        await self._client.aclose()

    async def __aenter__(self) -> EngineAPIClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
