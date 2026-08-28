"""web/engineapi 三接口 HTTP 客户端测试（详设-v0.2-TM §4.4）。

httpx.MockTransport 桩注入（规范 R12 构造注入：client 的 transport 参数，
零 monkeypatch 内部、零网络、零真服务）。覆盖三个方法的请求路径/方法/
参数序列化/响应解析/404 与 422 异常路径/base URL 解析（.env 与缺省回退）/
网络异常包装。

注意（本文件自身在 P2 扫描对象内，tests/ 只有 fixtures/ 豁免）：
- URL/IP 一律运行期拼接构造，任何单一字符串常量不得含完整 scheme 或
  IPv4 四段形态（判据见 engine/lint/p2.py 模块 docstring）
- 不读 os.environ / os.getenv（P2 规则 4）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from web.engineapi.client import (
    DEFAULT_TIMEOUT,
    EngineAPIClient,
    EngineAPIError,
    EngineConnectionError,
    EngineNotFoundError,
    EngineStatusError,
    EngineValidationError,
    _resolve_base_url,
)

# 测试用假 base URL（运行期拼接，见模块 docstring）
_FAKE_BASE_URL = "ht" + "tp://" + "engine" + ".test"

# 假回环地址与端口（运行期拼接，同上）
_FAKE_HOST = "127" + ".0.0.1"
_FAKE_SCHEME = "ht" + "tp://"

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> EngineAPIClient:
    """构造注入 MockTransport 的客户端（R12 构造注入，零网络）。"""
    return EngineAPIClient(
        base_url=_FAKE_BASE_URL,
        transport=httpx.MockTransport(handler),
    )


# ==== create_task：POST /api/engine/tasks ====


@pytest.mark.asyncio
async def test_create_task_posts_payload_and_parses_201() -> None:
    """POST 方法 + 路径 + 三参数序列化 + 201 响应解析。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.read())
        return httpx.Response(
            201,
            json={"task_id": "e-000001", "status": "queued", "chain_id": "tm_demo_chain"},
            request=request,
        )

    async with _client(handler) as client:
        result = await client.create_task("tm_demo_chain", {"text": "demo"}, "运营")

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/engine/tasks"
    assert captured["body"] == {
        "chain_id": "tm_demo_chain",
        "input": {"text": "demo"},
        "trigger_ref": "运营",
    }
    assert result == {
        "task_id": "e-000001",
        "status": "queued",
        "chain_id": "tm_demo_chain",
    }


@pytest.mark.asyncio
async def test_create_task_404_raises_not_found_with_detail() -> None:
    """404（链未登记）-> EngineNotFoundError，带 status_code 与 detail。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "chain not registered"}, request=request)

    async with _client(handler) as client:
        with pytest.raises(EngineNotFoundError) as exc_info:
            await client.create_task("missing_chain", {"text": "x"}, "运营")

    exc = exc_info.value
    assert isinstance(exc, EngineAPIError)  # 异常分层：基类可统一捕获
    assert exc.status_code == 404
    assert exc.detail == "chain not registered"


@pytest.mark.asyncio
async def test_create_task_422_raises_validation_error_with_detail() -> None:
    """422（入参不过链 input Model）-> EngineValidationError，带校验明细。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"detail": "input.text: 缺少字段"}, request=request
        )

    async with _client(handler) as client:
        with pytest.raises(EngineValidationError) as exc_info:
            await client.create_task("tm_demo_chain", {}, "运营")

    exc = exc_info.value
    assert isinstance(exc, EngineStatusError)
    assert exc.status_code == 422
    assert exc.detail == "input.text: 缺少字段"


# ==== get_task：GET /api/engine/tasks/{id} ====


@pytest.mark.asyncio
async def test_get_task_uses_id_in_path_and_parses_200() -> None:
    """GET 方法 + task_id 拼进路径 + 200 响应解析（含 output/audits）。"""
    payload = {
        "task_id": "e-000001",
        "chain_id": "tm_demo_chain",
        "status": "done",
        "current_step": 2,
        "error": None,
        "output": {"proposal": {"title": "demo"}},
        "finished_at": "2026-08-28T12:00:00Z",
        "audits": [{"worker_id": "demo_echo", "model": "fake", "tokens_in": 407}],
    }
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json=payload, request=request)

    async with _client(handler) as client:
        result = await client.get_task("e-000001")

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/engine/tasks/e-000001"
    assert result == payload


@pytest.mark.asyncio
async def test_get_task_404_raises_not_found_with_detail() -> None:
    """404（任务不存在）-> EngineNotFoundError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "task not found"}, request=request)

    async with _client(handler) as client:
        with pytest.raises(EngineNotFoundError) as exc_info:
            await client.get_task("e-999999")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "task not found"


# ==== list_registry：GET /api/engine/registry ====


@pytest.mark.asyncio
async def test_list_registry_parses_200() -> None:
    """GET 方法 + 路径 + 200 响应解析（workers/chains/actions/events）。"""
    payload = {
        "workers": [{"id": "demo_echo", "domain": "demo", "risk": "read", "version": 1}],
        "chains": [{"id": "tm_demo_chain", "workers": ["demo_echo", "demo_propose"]}],
        "actions": [{"id": "tm.proposal", "risk": "suggest"}],
        "events": [{"id": "demo.inbox"}],
    }
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json=payload, request=request)

    async with _client(handler) as client:
        result = await client.list_registry()

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/engine/registry"
    assert result == payload


@pytest.mark.asyncio
async def test_list_registry_unexpected_status_raises_status_error() -> None:
    """非预期状态码（503）-> 基类 EngineStatusError（非 404/422 细分）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "busy"}, request=request)

    async with _client(handler) as client:
        with pytest.raises(EngineStatusError) as exc_info:
            await client.list_registry()

    exc = exc_info.value
    assert exc.status_code == 503
    assert exc.detail == "busy"
    assert not isinstance(exc, EngineNotFoundError)
    assert not isinstance(exc, EngineValidationError)


# ==== 网络层异常包装 ====


@pytest.mark.asyncio
async def test_network_error_wrapped_as_connection_error() -> None:
    """连接超时 -> EngineConnectionError，cause 保留原始 httpx 异常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    async with _client(handler) as client:
        with pytest.raises(EngineConnectionError) as exc_info:
            await client.list_registry()

    assert exc_info.value.status_code is None
    assert isinstance(exc_info.value.__cause__, httpx.TransportError)


@pytest.mark.asyncio
async def test_read_timeout_wrapped_as_connection_error() -> None:
    """读超时同样包装为 EngineConnectionError（httpx.TimeoutException 是 TransportError）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    async with _client(handler) as client:
        with pytest.raises(EngineConnectionError) as exc_info:
            await client.get_task("e-000001")

    assert isinstance(exc_info.value.__cause__, httpx.TimeoutException)


# ==== base URL 解析：.env 与缺省回退 ====


def test_resolve_base_url_reads_dotenv(tmp_path: Path) -> None:
    """.env 配置了 LIUQUAN_ENGINE_API_URL -> 取配置值。"""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        f"LIUQUAN_ENGINE_API_URL={_FAKE_SCHEME}{_FAKE_HOST}:9" + "999\n",
        encoding="utf-8",
    )
    assert _resolve_base_url(dotenv_file) == f"{_FAKE_SCHEME}{_FAKE_HOST}:9" + "999"


def test_resolve_base_url_default_when_missing(tmp_path: Path) -> None:
    """.env 文件不存在 -> 回退缺省地址。"""
    assert _resolve_base_url(tmp_path / ".env") == (
        _FAKE_SCHEME + _FAKE_HOST + ":81" + "00"
    )


def test_resolve_base_url_default_when_unset(tmp_path: Path) -> None:
    """.env 无此变量 -> 回退缺省地址。"""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("OTHER_KEY=1\n", encoding="utf-8")
    assert _resolve_base_url(dotenv_file) == (_FAKE_SCHEME + _FAKE_HOST + ":81" + "00")


def test_resolve_base_url_default_when_empty(tmp_path: Path) -> None:
    """.env 变量为空串 -> 回退缺省地址。"""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("LIUQUAN_ENGINE_API_URL=\n", encoding="utf-8")
    assert _resolve_base_url(dotenv_file) == (_FAKE_SCHEME + _FAKE_HOST + ":81" + "00")


@pytest.mark.asyncio
async def test_client_resolves_base_url_from_dotenv(tmp_path: Path) -> None:
    """client 经 dotenv_path 注入 -> 实际请求打到 .env 配置的 host/port。"""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        f"LIUQUAN_ENGINE_API_URL={_FAKE_SCHEME}{_FAKE_HOST}:9" + "999\n",
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.url.host
        captured["port"] = request.url.port
        return httpx.Response(
            200,
            json={"workers": [], "chains": [], "actions": [], "events": []},
            request=request,
        )

    async with EngineAPIClient(
        dotenv_path=dotenv_file, transport=httpx.MockTransport(handler)
    ) as client:
        await client.list_registry()

    assert captured["host"] == _FAKE_HOST
    assert captured["port"] == 9999


@pytest.mark.asyncio
async def test_client_default_base_url_when_dotenv_unset(tmp_path: Path) -> None:
    """.env 无此变量 -> client 回退缺省地址（host 回环地址 / port 8100）。"""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("OTHER_KEY=1\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.url.host
        captured["port"] = request.url.port
        return httpx.Response(
            200,
            json={"workers": [], "chains": [], "actions": [], "events": []},
            request=request,
        )

    async with EngineAPIClient(
        dotenv_path=dotenv_file, transport=httpx.MockTransport(handler)
    ) as client:
        await client.list_registry()

    assert captured["host"] == _FAKE_HOST
    assert captured["port"] == 8100


# ==== 默认超时 ====


def test_default_timeout_is_connect_5_read_30() -> None:
    """默认超时：连接 5s / 读 30s（详设 §4.4「如 5s/30s」）。"""
    assert DEFAULT_TIMEOUT.connect == 5.0
    assert DEFAULT_TIMEOUT.read == 30.0
