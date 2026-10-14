"""限流与 LLM 可观测性接线测试（slowapi 官方库 + litellm 官方 callback）。

对应 2026-10-14 打磨轮引进的两项工程能力。每条都锁"可观察行为"，
而不是"某个函数被调用过"。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lingxi.config.settings import RateLimitConfig, Settings
from lingxi.flow.dashboard_api import create_dashboard_app
from lingxi.flow.queue import EscalationQueue
from lingxi.flow.ticket import TicketStore


class TestRateLimitWiring:
    """限流必须生效，且**不能把正常响应变成 500**。"""

    def test_heavy_limit_installed(self) -> None:
        """启用限流时 app.state 上必须有 limiter（slowapi 官方挂载点）。"""
        store, queue = TicketStore(), EscalationQueue()
        app = create_dashboard_app(store, queue)
        assert getattr(app.state, "limiter", None) is not None

    def test_normal_response_not_broken_by_headers(self) -> None:
        """回归锁：headers_enabled=True 会让返回 dict 的端点 500。

        slowapi 开 headers_enabled 后，_inject_headers 要求被装饰端点返回
        Response 实例；FastAPI 端点多返回 dict（自动序列化），于是
        `parameter 'response' must be an instance of Response` → 500。
        实测复现过（persona/preview 连打 30 次全 500）。
        """
        from lingxi.config import get_settings

        settings = get_settings()
        assert settings.ratelimit.enabled is True
        store, queue = TicketStore(), EscalationQueue()
        client = TestClient(create_dashboard_app(store, queue))
        resp = client.post("/api/persona/preview", json={"message": "hi"})
        assert resp.status_code != 500, resp.text
        assert resp.status_code in (200, 401, 403, 503)

    def test_heavy_limit_actually_triggers(self) -> None:
        """超过额度必须返回 429（slowapi 官方行为）。"""
        from lingxi.config import get_settings

        settings = get_settings()
        old = settings.ratelimit.heavy
        settings.ratelimit.heavy = "3/minute"
        try:
            store, queue = TicketStore(), EscalationQueue()
            client = TestClient(create_dashboard_app(store, queue))
            codes = [
                client.post("/api/persona/preview", json={"message": "hi"}).status_code
                for _ in range(6)
            ]
            assert 429 in codes, f"额度 3/分钟下 6 次请求应出现 429，实际 {codes}"
        finally:
            settings.ratelimit.heavy = old

    def test_disabled_when_configured_off(self) -> None:
        from lingxi.config import get_settings

        settings = get_settings()
        old = settings.ratelimit.enabled
        settings.ratelimit.enabled = False
        try:
            store, queue = TicketStore(), EscalationQueue()
            app = create_dashboard_app(store, queue)
            assert getattr(app.state, "limiter", None) is None
        finally:
            settings.ratelimit.enabled = old


class TestLlmObservabilityWiring:
    """litellm 官方 callback 装配（token/成本/缓存命中 → Prometheus）。"""

    def test_callbacks_registered(self) -> None:
        from lingxi.agent.llm_observability import (
            reset_for_tests,
            setup_llm_observability,
        )

        reset_for_tests()
        setup_llm_observability(Settings())
        import litellm

        assert litellm.success_callback, "success_callback 未装配"
        assert litellm.failure_callback, "failure_callback 未装配"

    def test_setup_is_idempotent(self) -> None:
        from lingxi.agent.llm_observability import (
            reset_for_tests,
            setup_llm_observability,
        )

        reset_for_tests()
        setup_llm_observability(Settings())
        import litellm

        before = len(litellm.success_callback)
        setup_llm_observability(Settings())  # 第二次调用应直接返回
        assert len(litellm.success_callback) == before

    def test_cache_configured_when_enabled(self) -> None:
        from lingxi.agent.llm_observability import (
            reset_for_tests,
            setup_llm_observability,
        )

        reset_for_tests()
        settings = Settings()
        settings.llm_cache.enabled = True
        settings.llm_cache.backend = "disk"
        settings.llm_cache.disk_dir = ".test_litellm_cache"
        setup_llm_observability(settings)
        import litellm

        assert litellm.cache is not None, "启用后 litellm.cache 应已装配"

    def test_cache_absent_when_disabled(self) -> None:
        from lingxi.agent.llm_observability import (
            reset_for_tests,
            setup_llm_observability,
        )

        reset_for_tests()
        settings = Settings()
        settings.llm_cache.enabled = False
        import litellm

        old_cache = litellm.cache
        try:
            setup_llm_observability(settings)
            assert litellm.cache is None or litellm.cache == old_cache
        finally:
            litellm.cache = old_cache


class TestLlmMetrics:
    """LLM 指标写进 Prometheus 并能被抓取。"""

    def test_records_and_exposes(self) -> None:
        from lingxi.metrics.collector import MetricsCollector

        c = MetricsCollector()
        c.record_llm_success(
            provider="deepseek",
            model="deepseek-chat",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.002,
            duration=1.5,
            cache_hit=True,
        )
        out = c.generate().decode()
        assert 'zhixi_llm_tokens_total{kind="prompt",model="deepseek-chat",provider="deepseek"} 100.0' in out
        assert 'zhixi_llm_cost_usd_total{model="deepseek-chat",provider="deepseek"} 0.002' in out
        assert 'zhixi_llm_cache_hits_total{model="deepseek-chat",provider="deepseek"} 1.0' in out

    def test_failure_recorded(self) -> None:
        from lingxi.metrics.collector import MetricsCollector

        c = MetricsCollector()
        c.record_llm_failure(provider="openai", model="gpt-4o-mini")
        out = c.generate().decode()
        assert 'status="failure"' in out


class TestRateLimitConfig:
    def test_defaults_reasonable(self) -> None:
        rl = RateLimitConfig()
        assert rl.enabled is True
        assert "minute" in rl.heavy

    @pytest.mark.parametrize("quota", ["10/minute", "100/hour", "5/second"])
    def test_quota_format_accepted(self, quota: str) -> None:
        """slowapi 额度格式：<n>/<unit>，配置层不做正则校验（交给库）。"""
        rl = RateLimitConfig(heavy=quota)
        assert rl.heavy == quota
