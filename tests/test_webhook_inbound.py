"""入站 Webhook HMAC 验签端点测试（/api/webhook/inbound）。

验证 dashboard_api 在配置 inbound_hmac_secret 时挂载验签路由，
未配置时端点整体不挂载（404）。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lingxi.config import get_settings
from lingxi.flow.dashboard_api import create_dashboard_app
from lingxi.flow.queue import EscalationQueue
from lingxi.flow.ticket import TicketStore
from lingxi.security.hmac import sign_webhook_payload


@pytest.fixture
def webhook_app_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, dict[str, Any]]:
    """配置了入站 HMAC secret 的 Dashboard TestClient。"""
    from pydantic import SecretStr

    settings = get_settings()
    secret = "test-inbound-webhook-secret-0123456789"
    monkeypatch.setattr(
        settings.webhook, "inbound_hmac_secret", SecretStr(secret), raising=False
    )
    store, queue = TicketStore(), EscalationQueue()
    app = create_dashboard_app(store, queue)
    return TestClient(app), {"secret": secret, "settings": settings}


class TestInboundWebhookEndpoint:
    """HMAC 验签端点行为面。"""

    def test_valid_signature_accepted(
        self, webhook_app_client: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, meta = webhook_app_client
        body = json.dumps({"event": "ticket.escalated", "ticket_id": "t-1"})
        headers = sign_webhook_payload(body, meta["secret"])
        resp = client.post(
            "/api/webhook/inbound",
            content=body,
            headers={**headers, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["received"] is True
        assert data["payload"]["ticket_id"] == "t-1"

    def test_wrong_secret_rejected(
        self, webhook_app_client: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, meta = webhook_app_client
        body = b'{"event": "x"}'
        headers = sign_webhook_payload(body, "attacker-secret")
        resp = client.post("/api/webhook/inbound", content=body, headers=headers)
        assert resp.status_code == 401
        assert "Signature" in resp.json()["detail"]

    def test_tampered_body_rejected(
        self, webhook_app_client: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, meta = webhook_app_client
        original = b'{"event": "original"}'
        headers = sign_webhook_payload(original, meta["secret"])
        resp = client.post(
            "/api/webhook/inbound",
            content=b'{"event": "tampered"}',
            headers=headers,
        )
        assert resp.status_code == 401

    def test_stale_timestamp_rejected(
        self, webhook_app_client: tuple[TestClient, dict[str, Any]]
    ) -> None:
        """防重放窗口（默认 300s）外的时间戳拒绝。"""
        client, meta = webhook_app_client
        import time

        old_ts = str(int(time.time()) - 9999)
        body = b'{"event": "old"}'
        # 用旧时间戳手算签名（绕过 sign_webhook_payload 取当前时间）
        import hashlib
        import hmac as _hmac

        payload = f"{old_ts}\n".encode() + body
        sig = _hmac.new(meta["secret"].encode(), payload, hashlib.sha256).hexdigest()
        resp = client.post(
            "/api/webhook/inbound",
            content=body,
            headers={"X-ZhiXi-Signature": f"sha256={sig}", "X-ZhiXi-Timestamp": old_ts},
        )
        assert resp.status_code == 401

    def test_missing_headers_rejected(
        self, webhook_app_client: tuple[TestClient, dict[str, Any]]
    ) -> None:
        client, _ = webhook_app_client
        resp = client.post("/api/webhook/inbound", content=b"{}")
        assert resp.status_code == 401

    def test_not_mounted_without_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未配置 inbound_hmac_secret 时端点 404（不产生半开验签面）。"""
        settings = get_settings()
        from pydantic import SecretStr

        monkeypatch.setattr(
            settings.webhook, "inbound_hmac_secret", SecretStr(""), raising=False
        )
        store, queue = TicketStore(), EscalationQueue()
        client = TestClient(create_dashboard_app(store, queue))
        resp = client.post("/api/webhook/inbound", content=b"{}")
        assert resp.status_code == 404
