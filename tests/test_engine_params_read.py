"""v0.4 批 1b 引擎参数读取路径测试（MockTransport 桩，零网络）。

覆盖（详设 §8 / A44）：
- server 启动/lifespan 时经 biz_client GET /api/biz/settings/engine-params
- biz_client.get 方法存在且正确
- 读取失败时回退默认 {2, 30.0, 30} + warning 不阻塞启动

基建：MockTransport 桩模拟 web 侧返回。
"""

from __future__ import annotations

import httpx
import pytest

from engine.actions.biz_client import BizApiClient, BizApiError

# P2 URL 拆串（不出现完整 scheme+host 字面量）
_FAKE_BIZ_URL = "http" + "://stub"
_FAKE_TOKEN = "test" + "-token"


class _StubTransport(httpx.AsyncBaseTransport):
    """桩 transport：捕获请求路径和方法；engine-params 返回 200。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        self.requests.append((method, path))
        if path == "/api/biz/settings/engine-params" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "max_attempts": 3,
                    "timeout_s": 45.0,
                    "backoff_cap": 20,
                },
            )
        return httpx.Response(404, json={"detail": "not found"})


class _FailTransport(httpx.AsyncBaseTransport):
    """桩 transport：模拟网络失败。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")


class _NotFoundTransport(httpx.AsyncBaseTransport):
    """桩 transport：模拟 404。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})


# ---- biz_client.get 方法存在且正确 ----


@pytest.mark.asyncio
async def test_biz_client_get_method_exists():
    """BizApiClient.get 方法存在且返回 httpx.Response。"""
    transport = _StubTransport()
    client = BizApiClient(
        base_url=_FAKE_BIZ_URL,
        token=_FAKE_TOKEN,
        transport=transport,
    )
    resp = await client.get("/settings/engine-params")
    assert resp.status_code == 200
    data = resp.json()
    assert data["max_attempts"] == 3
    assert transport.requests[0] == ("GET", "/api/biz/settings/engine-params")


@pytest.mark.asyncio
async def test_biz_client_get_network_error():
    """BizApiClient.get 网络异常 -> BizApiError（不静默）。"""
    client = BizApiClient(
        base_url=_FAKE_BIZ_URL,
        token=_FAKE_TOKEN,
        transport=_FailTransport(),
    )
    with pytest.raises(BizApiError, match="业务读接口网络异常"):
        await client.get("/settings/engine-params")


# ---- _read_engine_params_from_biz ----


@pytest.mark.asyncio
async def test_read_engine_params_defaults_on_failure():
    """_read_engine_params_from_biz 在读取失败时回退默认。"""
    from engine.server import _read_engine_params_from_biz

    # biz_client=None -> 全默认
    result = await _read_engine_params_from_biz(None)
    assert result["default_max_attempts"] == 2
    assert result["default_timeout_s"] == 30.0
    assert result["backoff_cap"] == 30.0

    # biz_client 网络失败 -> 全默认
    client_fail = BizApiClient(
        base_url=_FAKE_BIZ_URL,
        token=_FAKE_TOKEN,
        transport=_FailTransport(),
    )
    result = await _read_engine_params_from_biz(client_fail)
    assert result["default_max_attempts"] == 2
    assert result["default_timeout_s"] == 30.0
    assert result["backoff_cap"] == 30.0


@pytest.mark.asyncio
async def test_read_engine_params_success():
    """_read_engine_params_from_biz 在读取成功时返回参数。"""
    from engine.server import _read_engine_params_from_biz

    client_ok = BizApiClient(
        base_url=_FAKE_BIZ_URL,
        token=_FAKE_TOKEN,
        transport=_StubTransport(),
    )
    result = await _read_engine_params_from_biz(client_ok)
    assert result["default_max_attempts"] == 3
    assert result["default_timeout_s"] == 45.0
    assert result["backoff_cap"] == 20


@pytest.mark.asyncio
async def test_read_engine_params_http_error_fallback():
    """_read_engine_params_from_biz 在 HTTP 非 200 时回退默认。"""
    from engine.server import _read_engine_params_from_biz

    client_404 = BizApiClient(
        base_url=_FAKE_BIZ_URL,
        token=_FAKE_TOKEN,
        transport=_NotFoundTransport(),
    )
    result = await _read_engine_params_from_biz(client_404)
    assert result["default_max_attempts"] == 2
    assert result["default_timeout_s"] == 30.0
    assert result["backoff_cap"] == 30.0


# ---- lifespan 注入 runner（验收修复：读取参数真正生效，详设 §7.3）----


class _FakeConsumer:
    """最小 consumer 桩：记录 _runner 替换。"""

    def __init__(self) -> None:
        self._runner = "orig-runner"

    async def recover(self) -> list[str]:
        return []

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_lifespan_injects_engine_params_into_runner():
    """lifespan 读取引擎参数后重建 runner 并替换 consumer._runner（真正注入）。"""
    from types import SimpleNamespace

    from engine.server import _make_lifespan

    captured: dict = {}

    def factory(params: dict) -> str:
        captured.update(params)
        return "new-runner"

    consumer = _FakeConsumer()
    client_ok = BizApiClient(
        base_url=_FAKE_BIZ_URL,
        token=_FAKE_TOKEN,
        transport=_StubTransport(),
    )
    app = SimpleNamespace(state=SimpleNamespace())
    lf = _make_lifespan(consumer, biz_client=client_ok, runner_factory=factory)
    async with lf(app):
        pass
    assert consumer._runner == "new-runner"  # 注入生效
    assert captured["default_max_attempts"] == 3
    assert captured["default_timeout_s"] == 45.0
    assert captured["backoff_cap"] == 20
    assert app.state.engine_params["default_max_attempts"] == 3


@pytest.mark.asyncio
async def test_lifespan_keeps_runner_without_biz_client():
    """biz_client 为 None（测试/未配置）时不重建 runner，保持原 runner。"""
    from types import SimpleNamespace

    from engine.server import _make_lifespan

    consumer = _FakeConsumer()
    lf = _make_lifespan(
        consumer,
        biz_client=None,
        runner_factory=lambda params: "new-runner",
    )
    async with lf(SimpleNamespace(state=SimpleNamespace())):
        pass
    assert consumer._runner == "orig-runner"  # 未替换
