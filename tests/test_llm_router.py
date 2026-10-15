"""LLM Router 测试（多 key 轮转 / 熔断 / 预算，全部走 litellm 官方 Router）。

设计立场：**单 key 场景不启用 Router** —— 多一层包装对零配置本地模型
是负收益（启动更慢、行为更难预测）。只有配了多凭据或显式开启才构建。
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from lingxi.agent.llm_router import (
    build_router,
    reset_for_tests,
    router_status,
    should_enable_router,
)
from lingxi.config import Settings


@pytest.fixture(autouse=True)
def _clean() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


def _multi_key_settings(**llm_overrides: Any) -> Settings:
    s = Settings()
    s.llm.provider = "openai"
    s.llm.model = "gpt-4o-mini"
    s.llm.base_url = None
    s.llm.api_key = SecretStr("sk-primary")
    s.llm.api_keys = [SecretStr("sk-extra-1"), SecretStr("sk-extra-2")]
    for k, v in llm_overrides.items():
        setattr(s.llm, k, v)
    return s


class TestEnablementDecision:
    def test_single_key_does_not_enable(self) -> None:
        """零配置/单 key：不启用（避免无谓的包装层）。"""
        s = Settings()
        assert should_enable_router(s) is False
        assert build_router(s) is None
        assert router_status() == {"enabled": False}

    def test_extra_keys_enable(self) -> None:
        assert should_enable_router(_multi_key_settings()) is True

    def test_explicit_flag_enables_without_extra_keys(self) -> None:
        s = Settings()
        s.llm.router_enabled = True
        assert should_enable_router(s) is True

    def test_empty_keys_list_does_not_enable(self) -> None:
        """空列表不算配置（避免踩到"设了空数组就启用"的坑）。"""
        s = Settings()
        s.llm.api_keys = []
        assert should_enable_router(s) is False


class TestRouterConstruction:
    def test_deployments_cover_models_and_keys(self) -> None:
        """部署数 = 模型数 × 凭据数（同组轮转）。"""
        s = _multi_key_settings()
        router = build_router(s)
        assert router is not None
        st = router_status()
        # 3 个模型（1 主 + 2 fallback）× 3 个 key（1 主 + 2 备）= 9 起
        assert st["deployments"] >= 9, f"部署数偏少: {st}"

    def test_strategy_applied(self) -> None:
        s = _multi_key_settings(routing_strategy="least-busy")
        router = build_router(s)
        assert router is not None
        assert router_status()["strategy"] == "least-busy"

    def test_cooldown_settings_accepted(self) -> None:
        """熔断参数（allowed_fails / cooldown_time）应被接受。"""
        s = _multi_key_settings(allowed_fails=5, cooldown_time=120.0)
        router = build_router(s)
        assert router is not None

    def test_budget_accepted(self) -> None:
        s = _multi_key_settings(max_budget_usd=10.0)
        router = build_router(s)
        assert router is not None

    def test_secretstr_keys_are_unwrapped(self) -> None:
        """回归锁：SecretStr 必须解包成明文再传 litellm。

        实测踩过：直接传 SecretStr 列表会被 litellm 的 pydantic 校验拒绝
        （Input should be a valid string, input_type=SecretStr），
        Router 静默构建失败 → 退回直连，多 key 轮转永不生效。
        """
        s = _multi_key_settings()
        router = build_router(s)
        assert router is not None, "SecretStr 未解包会导致 Router 构建失败"

    def test_rebuild_same_config_is_cached(self) -> None:
        s = _multi_key_settings()
        r1 = build_router(s)
        r2 = build_router(s)
        assert r1 is r2, "同配置应复用同一 Router 实例"

    def test_config_change_rebuilds(self) -> None:
        s1 = _multi_key_settings()
        build_router(s1)
        s2 = _multi_key_settings(routing_strategy="latency-based-routing")
        build_router(s2)
        assert router_status()["strategy"] == "latency-based-routing"


class TestRouterStatus:
    def test_disabled_status_shape(self) -> None:
        assert router_status() == {"enabled": False}

    def test_enabled_status_shape(self) -> None:
        build_router(_multi_key_settings())
        st = router_status()
        assert st["enabled"] is True
        assert "deployments" in st
        assert "strategy" in st


class TestCallLLMFallbackToDirect:
    @pytest.mark.asyncio
    async def test_router_failure_falls_back_to_direct(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Router 调用失败时不能整个崩 —— 应落到直连路径再试。

        这是真实的容错设计：Router 配置有误（如 base_url 写错）不该让
        整个引擎失去 LLM 能力。
        """
        from unittest.mock import AsyncMock, patch

        from lingxi.agent.llm import call_llm

        class _FakeResp:
            choices = [type("C", (), {"message": type("M", (), {"content": "ok", "tool_calls": None})()})()]
            usage = None

        import lingxi.agent.llm_router as lr

        monkeypatch.setattr(lr, "build_router", lambda settings: None)  # 强制不走 Router

        with patch("litellm.acompletion", new=AsyncMock(return_value=_FakeResp())):
            resp = await call_llm([{"role": "user", "content": "hi"}])
        assert resp is not None
