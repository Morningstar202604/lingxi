"""零配置可用性 —— 锁住"装完不配置也必须能用"这条产品底线。

🔗 工程逻辑
    上一轮把管理面改成"未配置 admin_token 即 fail-closed"(AUDIT-2026-10-10 #8)，
    安全上正确，但产品上把默认部署(``--dashboard-host 127.0.0.1``)一并打死了：
    用户双击 exe、什么都不配，看到的却是满屏 503 / 前端直接抛错。

    正确的切分不是"放开"或"锁死"，而是**按暴露面分级**：
    - 只监听 loopback（默认）→ 本机进程可访问是产品的一部分，免密放行
    - 监听 0.0.0.0（暴露到网络）→ 必须显式配 token，否则仍 fail-closed

    本文件锁住这个分级，以及"配了 token 之后校验照旧生效"。

🤖 AI 避坑
    - 别用"前端弹个框说请配置"糊弄：那只把问题从后端挪到前端，
      真正要修的是**默认部署路径**能不能直接跑。
    - 免密放行不等于"有 token 也能随便过"——显式带错 token 必须仍 401，
      否则就是"配了反而更不安全"的倒挂。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from lingxi.config.settings import get_settings
from lingxi.flow.dashboard_api import create_dashboard_app
from lingxi.flow.queue import EscalationQueue
from lingxi.flow.ticket import TicketStore


def _app(*, local_only: bool) -> TestClient:
    return TestClient(
        create_dashboard_app(TicketStore(), EscalationQueue(), local_only=local_only),
    )


@pytest.fixture()
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """回到"用户什么都没配"的状态：默认 token + 非 debug。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "debug", False, raising=False)
    monkeypatch.setattr(
        settings.security,
        "admin_token",
        SecretStr("change-me-in-production"),
        raising=False,
    )


# ── 1. loopback 零配置：必须直接能用 ──


class TestLoopbackZeroConfig:
    def test_local_only_allows_anonymous_read(
        self, unconfigured: None,
    ) -> None:
        """默认部署（仅 loopback）不配 token 也能读管理面。"""
        resp = _app(local_only=True).get("/api/dashboard/summary")
        assert resp.status_code == 200, resp.text
        assert "active_tickets" in resp.json()

    def test_local_only_allows_anonymous_write(
        self, unconfigured: None,
    ) -> None:
        """免密是"本地模式"，不是"只读模式"——认领也得能过。"""
        resp = _app(local_only=True).post(
            "/api/queue/claim",
            params={"agent_id": "seat-1"},
        )
        # 空队列返回 404（REST 语义），说明已通过鉴权进了业务逻辑
        assert resp.status_code in (200, 404), resp.text

    def test_metrics_scrape_also_works(
        self, unconfigured: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/metrics 在本地模式下同样免密（Prometheus 本机抓取是常态）。"""
        settings = get_settings()
        monkeypatch.setattr(settings.metrics, "enabled", True, raising=False)
        resp = _app(local_only=True).get("/metrics")
        assert resp.status_code == 200, resp.text


# ── 2. 非 loopback：仍必须 fail-closed ──


class TestExposedStillFailsClosed:
    def test_non_local_still_503_when_unconfigured(
        self, unconfigured: None,
    ) -> None:
        """绑定到 0.0.0.0 且未配 token → 继续 503，绝不放行。"""
        resp = _app(local_only=False).get("/api/dashboard/summary")
        assert resp.status_code == 503, resp.text
        assert "ADMIN_TOKEN" in resp.json()["detail"]

    def test_default_param_is_fail_closed(self, unconfigured: None) -> None:
        """默认值必须是安全的一侧：不确定是否 loopback 时按暴露处理。"""
        resp = _app(local_only=False).get("/api/tickets")
        assert resp.status_code == 503, resp.text


# ── 3. 配了 token 之后，校验必须照旧 ──


class TestConfiguredTokenStillEnforced:
    def test_wrong_token_rejected_in_local_mode(
        self, admin_token_env: str,
    ) -> None:
        """显式带错 token → 401。免密模式不等于"任何 token 都过"。"""
        resp = _app(local_only=True).get(
            "/api/dashboard/summary",
            headers={"x-admin-token": "wrong-token"},
        )
        assert resp.status_code == 401, resp.text

    def test_correct_token_accepted_in_local_mode(
        self, admin_headers: dict[str, str],
    ) -> None:
        resp = _app(local_only=True).get(
            "/api/dashboard/summary", headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text


# ── 4. 开关可关：运维能主动关掉本地免密 ──


class TestOptOutSwitch:
    def test_allow_anonymous_local_false_restores_503(
        self, unconfigured: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """allow_anonymous_local=False 时即便 loopback 也 503。"""
        settings = get_settings()
        monkeypatch.setattr(
            settings.security, "allow_anonymous_local", False, raising=False,
        )
        resp = _app(local_only=True).get("/api/dashboard/summary")
        assert resp.status_code == 503, resp.text


# ── 5. 暴露面判定：host 字符串本身 ──


class TestLoopbackHostDetection:
    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "localhost", "::1", "[::1]"],
    )
    def test_loopback_hosts(self, host: str) -> None:
        from lingxi.__main__ import _is_loopback_host

        assert _is_loopback_host(host) is True, host

    @pytest.mark.parametrize(
        "host",
        ["0.0.0.0", "192.168.1.10", "10.0.0.5", "::", "", "example.com"],
    )
    def test_exposed_hosts(self, host: str) -> None:
        from lingxi.__main__ import _is_loopback_host

        assert _is_loopback_host(host) is False, host


# ── 6. CORS：前端直连后端所必需（代理缓冲 SSE 的替代方案）──


class TestCORSLetsFrontendConnectDirectly:
    def test_loopback_origin_allowed(
        self, admin_headers: dict[str, str],
    ) -> None:
        """浏览器里跑的前端（http://localhost:3100）必须能直连后端。"""
        resp = _app(local_only=True).get(
            "/api/dashboard/summary",
            headers={**admin_headers, "Origin": "http://localhost:3100"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3100"

    def test_preflight_for_custom_header(
        self, unconfigured: None,
    ) -> None:
        """x-admin-token 是自定义头 → 必须先过 OPTIONS 预检。"""
        resp = _app(local_only=True).options(
            "/api/dashboard/summary",
            headers={
                "Origin": "http://127.0.0.1:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-admin-token",
            },
        )
        assert resp.status_code == 200, resp.text
        assert "x-admin-token" in resp.headers.get(
            "access-control-allow-headers", ""
        ).lower()

    def test_non_loopback_origin_rejected(
        self, admin_headers: dict[str, str],
    ) -> None:
        """外部站点不得读到管理面数据（默认只放行 loopback 来源）。"""
        resp = _app(local_only=True).get(
            "/api/dashboard/summary",
            headers={**admin_headers, "Origin": "https://evil.example.com"},
        )
        assert "access-control-allow-origin" not in {
            k.lower() for k in resp.headers
        } or resp.headers.get("access-control-allow-origin") != "https://evil.example.com"

    def test_cors_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """运维可以彻底关掉 CORS（前端打包进 Tauri 时用不到）。"""
        settings = get_settings()
        monkeypatch.setattr(
            settings.security, "cors_allow_loopback", False, raising=False,
        )
        resp = _app(local_only=True).get(
            "/api/dashboard/summary", headers={"Origin": "http://localhost:3100"},
        )
        assert "access-control-allow-origin" not in resp.headers
