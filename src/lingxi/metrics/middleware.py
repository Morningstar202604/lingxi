"""ASGI 中间件：自动记录所有 HTTP 请求到 Prometheus。

🔗 工程逻辑
    放在 FastAPI 中间件层，自动捕获所有请求的 method/status/duration。
    无需修改每个 endpoint。

🤖 AI 避坑
    - ASGI 中间件要兼容 both scope types (http / websocket)
    - 不要用 time.time() 做 duration，用 time.monotonic() 防回拨
    - endpoint label 用 route.path 而非 raw path（避免 label 爆炸）
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from lingxi.core import get_logger
from lingxi.metrics.collector import get_metrics_collector

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = get_logger(__name__)


class PrometheusMiddleware:
    """ASGI 中间件：记录 HTTP 请求计数 + 延迟。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET")

        # 尝试从 route 获取干净的 endpoint 名称
        endpoint = path
        if "route" in scope:
            route = scope["route"]
            if hasattr(route, "path"):
                endpoint = route.path

        start = time.monotonic()
        status_code = 200

        async def wrapped_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        except Exception:
            status_code = 500
            raise
        finally:
            duration = time.monotonic() - start
            collector = get_metrics_collector()
            collector.record_http_request(method, endpoint, status_code, duration)
