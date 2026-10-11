"""HMAC-SHA256 Webhook 签名测试。"""
from __future__ import annotations

import time

from lingxi.security.hmac import sign_webhook_payload, verify_webhook_signature


class TestSignWebhookPayload:
    """签名功能测试。"""

    def test_basic_signature(self) -> None:
        headers = sign_webhook_payload(b'{"test": 1}', "secret123")
        assert "X-ZhiXi-Signature" in headers
        assert "X-ZhiXi-Timestamp" in headers
        assert headers["X-ZhiXi-Signature"].startswith("sha256=")

    def test_string_body(self) -> None:
        """str 输入应自动编码为 bytes。"""
        headers = sign_webhook_payload("hello world", "key")
        assert headers["X-ZhiXi-Signature"].startswith("sha256=")

    def test_custom_timestamp(self) -> None:
        """自定义时间戳。"""
        headers = sign_webhook_payload(b"body", "key", timestamp=1700000000.0)
        assert headers["X-ZhiXi-Timestamp"] == "1700000000"

    def test_different_secrets_produce_different_signatures(self) -> None:
        h1 = sign_webhook_payload(b"body", "secret_a")
        h2 = sign_webhook_payload(b"body", "secret_b")
        assert h1["X-ZhiXi-Signature"] != h2["X-ZhiXi-Signature"]

    def test_same_input_same_signature(self) -> None:
        """确定性：相同输入+相同时间戳 → 相同签名。"""
        ts = 1700000000.0
        h1 = sign_webhook_payload(b"body", "key", timestamp=ts)
        h2 = sign_webhook_payload(b"body", "key", timestamp=ts)
        assert h1["X-ZhiXi-Signature"] == h2["X-ZhiXi-Signature"]

    def test_different_body_different_signature(self) -> None:
        h1 = sign_webhook_payload(b"body_a", "key", timestamp=1700000000.0)
        h2 = sign_webhook_payload(b"body_b", "key", timestamp=1700000000.0)
        assert h1["X-ZhiXi-Signature"] != h2["X-ZhiXi-Signature"]


class TestVerifyWebhookSignature:
    """验证功能测试。"""

    def test_valid_signature(self) -> None:
        body = b'{"event": "test"}'
        secret = "webhook-secret"
        headers = sign_webhook_payload(body, secret)
        assert verify_webhook_signature(
            body,
            secret,
            headers["X-ZhiXi-Signature"],
            headers["X-ZhiXi-Timestamp"],
        )

    def test_invalid_signature(self) -> None:
        body = b'{"event": "test"}'
        headers = sign_webhook_payload(body, "correct-secret")
        assert not verify_webhook_signature(
            body, "wrong-secret",
            headers["X-ZhiXi-Signature"],
            headers["X-ZhiXi-Timestamp"],
        )

    def test_tampered_body(self) -> None:
        """篡改 body 后验证失败。"""
        body = b'{"event": "original"}'
        secret = "key"
        headers = sign_webhook_payload(body, secret)
        tampered = b'{"event": "tampered"}'
        assert not verify_webhook_signature(
            tampered, secret,
            headers["X-ZhiXi-Signature"],
            headers["X-ZhiXi-Timestamp"],
        )

    def test_expired_timestamp(self) -> None:
        """过期时间戳应拒绝。"""
        body = b"body"
        secret = "key"
        old_ts = str(int(time.time()) - 600)  # 10 分钟前
        assert not verify_webhook_signature(
            body, secret,
            "sha256=anything",
            old_ts,
            max_age_seconds=300,
        )

    def test_invalid_timestamp_format(self) -> None:
        """无效时间戳格式应拒绝。"""
        assert not verify_webhook_signature(b"body", "key", "sha256=abc", "not-a-number")

    def test_signature_without_prefix(self) -> None:
        """签名没有 sha256= 前缀也能验证。"""
        body = b"data"
        secret = "s"
        ts = str(int(time.time()))
        import hashlib
        import hmac as _hmac
        payload = f"{ts}\n".encode() + body
        sig = _hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        # 直接传 hex (无前缀)
        assert verify_webhook_signature(body, secret, sig, ts)

    def test_future_timestamp_within_tolerance(self) -> None:
        """未来时间戳在容忍范围内应通过。"""
        body = b"x"
        secret = "k"
        future_ts = str(int(time.time()) + 60) # 1 分钟后
        import hashlib
        import hmac as _hmac
        payload = f"{future_ts}\n".encode() + body
        sig = _hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(body, secret, sig, future_ts, max_age_seconds=300)
