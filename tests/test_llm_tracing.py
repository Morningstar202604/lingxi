"""LLM 追踪接线测试（LangSmith / OpenTelemetry，全部走官方集成）。

设计立场：外部追踪"接了更好"，但**默认不接** —— 离线部署（本项目常态）
不该因为缺追踪后端而报错或拖慢启动。Prometheus 指标始终在采集。
"""
from __future__ import annotations

import pytest

from lingxi.agent.llm_observability import (
    reset_for_tests,
    setup_llm_observability,
    setup_tracing,
    tracing_status,
)
from lingxi.config import Settings


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    # 清掉可能影响结果的追踪环境变量
    for k in ("LANGCHAIN_TRACING_V2", "LANGCHAIN_API_KEY", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


class TestDefaultOff:
    def test_no_env_means_no_external_tracing(self) -> None:
        """未配置任何追踪环境变量 → 不挂外部后端（零配置可用）。"""
        result = setup_tracing(Settings())
        assert result == {"langsmith": False, "otel": False}

    def test_status_reports_no_external(self) -> None:
        """st 里不该出现我们挂的外部追踪后端。

        注意：litellm 自己会挂 `LeastBusyLoggingHandler` 等内部 handler，
        所以断言的是"没有 otel/langsmith"，不是"callbacks 全空"。
        """
        setup_tracing(Settings())
        st = tracing_status()
        external = [c for c in st["callbacks"] if c in ("otel", "LangsmithLogger")]
        assert external == [], f"未配置时不该挂外部追踪: {external}"


class TestOtelEnabled:
    def test_otel_endpoint_attaches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配了 OTLP 端点 → 挂上 otel callback（独立 SDK 路径）。"""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
        result = setup_tracing(Settings())
        assert result["otel"] is True
        st = tracing_status()
        assert st["external_tracing"] is True
        assert "otel" in st["callbacks"]

    def test_upstream_litellm_mcp_conflict_documented(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归锁：不能退回 litellm 自带的 OTel 集成。

        litellm 1.83 的 `integrations.opentelemetry.OpenTelemetry` 会 import
        它自带的 experimental_mcp_client（按 mcp<2 API 写），与 mcp 2.3 冲突，
        挂载必失败（实测 'Server' object has no attribute 'list_tools'）。
        所以必须走独立 OTel SDK + 字符串 callback 名。
        """
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
        result = setup_tracing(Settings())
        assert result["otel"] is True, "独立 SDK 路径应当成功（litellm 路径会失败）"

        import litellm

        for cb in getattr(litellm, "callbacks", None) or []:
            assert not type(cb).__name__.startswith("OpenTelemetry"), (
                "不应挂 litellm 自带 OpenTelemetry 实例（与 mcp 2.x 冲突）"
            )


class TestLangsmithEnabled:
    def test_langsmith_requires_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只给 API key 不给开关 → 不启用（避免误连）。"""
        monkeypatch.setenv("LANGCHAIN_API_KEY", "sk-test")
        result = setup_tracing(Settings())
        assert result["langsmith"] is False

    def test_langsmith_with_flag_and_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
        monkeypatch.setenv("LANGCHAIN_API_KEY", "sk-test")
        result = setup_tracing(Settings())
        # 依赖缺失或凭据无效时不阻塞（只记 warning），但不应抛异常
        assert isinstance(result["langsmith"], bool)


class TestObservabilityIdempotent:
    def test_full_setup_is_idempotent(self) -> None:
        reset_for_tests()
        setup_llm_observability(Settings())
        import litellm

        before = len(getattr(litellm, "success_callback", None) or [])
        setup_llm_observability(Settings())
        after = len(getattr(litellm, "success_callback", None) or [])
        assert before == after


class TestSetupEndpointExposesStatus:
    def test_setup_status_has_rerank_and_tracing(self, admin_headers: dict) -> None:
        from fastapi.testclient import TestClient

        from lingxi.flow.dashboard_api import create_dashboard_app
        from lingxi.flow.queue import EscalationQueue
        from lingxi.flow.ticket import TicketStore

        client = TestClient(create_dashboard_app(TicketStore(), EscalationQueue()))
        resp = client.get("/api/setup/status", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "embedding" in data
        assert "rerank" in data
        assert "tracing" in data
        assert "enabled" in data["rerank"]
        assert "external_tracing" in data["tracing"]
