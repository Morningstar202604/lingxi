"""Prometheus 指标收集器测试。"""
from __future__ import annotations

import pytest

from lingxi.metrics import get_metrics_collector

try:
    from prometheus_client import CollectorRegistry  # noqa: F401
    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False

# 延迟导入，仅在 prometheus_client 可用时加载
if HAS_PROMETHEUS:
    from lingxi.metrics.collector import MetricsCollector


class TestMetricsCollector:
    """指标收集器核心测试。"""

    @staticmethod
    def _new_collector() -> MetricsCollector:
        """创建使用隔离 registry 的新收集器（测试用）。"""
        if not HAS_PROMETHEUS:
            pytest.skip("prometheus_client not installed")
        registry = CollectorRegistry()
        return MetricsCollector(registry=registry)

    def test_singleton(self) -> None:
        """get_metrics_collector 返回单例。"""
        c1 = get_metrics_collector()
        c2 = get_metrics_collector()
        assert c1 is c2

    def test_new_collector_not_noop_when_prometheus_installed(self) -> None:
        """当 prometheus_client 安装时收集器非 noop。"""
        c = self._new_collector()
        assert not c._noop

    def test_record_http_request(self) -> None:
        c = self._new_collector()
        c.record_http_request("GET", "/api/test", 200, 0.05)

    def test_record_agent_invocation(self) -> None:
        c = self._new_collector()
        c.record_agent_invocation("qq_private", "success", 3.5)
        c.record_agent_invocation("gewechat", "escalated", 12.0)

    def test_record_escalation(self) -> None:
        c = self._new_collector()
        c.record_escalation("low_confidence")
        c.record_escalation("keyword_match")
        c.record_escalation("timeout")
        c.record_escalation("prompt_injection")

    def test_update_business_metrics(self) -> None:
        c = self._new_collector()
        c.update_business_metrics(
            {"processing": 5, "waiting_user": 2, "escalated": 1},
            queue_depth=3,
        )

    def test_generate_returns_bytes(self) -> None:
        c = self._new_collector()
        output = c.generate()
        assert isinstance(output, bytes)
        assert len(output) > 0

    def test_generate_contains_zhixi_metrics(self) -> None:
        """验证生成的 Prometheus 输出包含 zhixi 前缀指标。"""
        c = self._new_collector()
        c.record_http_request("GET", "/test", 200, 0.1)
        output = c.generate().decode("utf-8")
        assert "zhixi_" in output

    def test_categorize_reason(self) -> None:
        assert "low_confidence" in MetricsCollector._categorize_reason("low_confidence")
        assert "timeout" in MetricsCollector._categorize_reason("timeout")
        assert "injection" in MetricsCollector._categorize_reason("prompt_injection")
        assert "keyword" in MetricsCollector._categorize_reason("keyword_match")
        assert MetricsCollector._categorize_reason("") == "none"
        assert MetricsCollector._categorize_reason("unknown") == "other"


class TestPrometheusMiddleware:
    """ASGI 中间件测试。"""

    @pytest.mark.asyncio()
    async def test_middleware_passes_through_non_http(self) -> None:
        """WebSocket scope 直接透传。"""
        from lingxi.metrics.middleware import PrometheusMiddleware

        async def app(scope, receive, send):  # type: ignore[no-untyped-def]
            await send({"type": "websocket.connect"})

        middleware = PrometheusMiddleware(app)

        called = False

        async def receive():  # type: ignore[no-untyped-def]
            return {"type": "websocket.receive"}

        async def send(msg):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True

        await middleware({"type": "websocket"}, receive, send)
        assert called

    @pytest.mark.asyncio()
    async def test_middleware_records_http_request(self) -> None:
        """HTTP 请求应被记录。"""
        from lingxi.metrics.middleware import PrometheusMiddleware

        request_count = 0

        async def app(scope, receive, send):  # type: ignore[no-untyped-def]
            nonlocal request_count
            request_count += 1
            await send({
                "type": "http.response.start",
                "status": 200,
            })

        middleware = PrometheusMiddleware(app)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
        }

        async def receive():  # type: ignore[no-untyped-def]
            return {"type": "http.request", "body": b""}

        sent_messages = []

        async def send(msg):  # type: ignore[no-untyped-def]
            sent_messages.append(msg)

        await middleware(scope, receive, send)
        assert request_count == 1
        # 确认 status 被正确捕获
        assert sent_messages[0]["status"] == 200
