"""出站 Webhook HMAC-SHA256 签名。

🔗 工程逻辑
    规则引擎的 webhook 动作在出站 HTTP POST 时，
    使用 HMAC-SHA256 + 时间戳 + 请求体 生成签名，
    通过 X-ZhiXi-Signature、X-ZhiXi-Timestamp 两个 Header 发送。

    接收方验证流程：
    1. 读取 X-ZhiXi-Timestamp，与当前时间比对（防重放，默认 ±5min）
    2. 用共享 secret 对 `{timestamp}\n{body}` 做 HMAC-SHA256
    3. 将 hex digest 与 X-ZhiXi-Signature 比对

    此模式与 Stripe Webhook、GitHub Webhook 的签名方案一致，
    是业界成熟的标准做法。

🤖 AI 避坑
    - 不要使用 md5 或 sha1（已被证明不安全）
    - 签名 payload 必须包含 timestamp，否则无法防重放
    - 用 hmac.compare_digest 做常量时间比较，防时序攻击
    - secret 必须足够长（≥32 字符随机值）
"""
from __future__ import annotations

import hashlib
import hmac
import time

from lingxi.core import get_logger

log = get_logger(__name__)


def sign_webhook_payload(
    body: bytes | str,
    secret: str,
    *,
    timestamp: float | None = None,
) -> dict[str, str]:
    """为 webhook 负载生成 HMAC-SHA256 签名。

    参数:
        body: 请求体 (bytes 或 str)
        secret: 共享密钥
        timestamp: 指定时间戳 (默认用当前时间)

    返回:
        dict 包含 signature 和 timestamp header 值。

    用法:
        headers = sign_webhook_payload(payload_bytes, "my-secret-key")
        async with httpx.AsyncClient() as c:
            await c.post(url, content=payload_bytes, headers=headers)
    """
    if isinstance(body, str):
        body = body.encode("utf-8")

    ts = timestamp if timestamp is not None else time.time()
    ts_str = str(int(ts))
    payload = f"{ts_str}\n".encode() + body

    sig = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return {
        "X-ZhiXi-Signature": f"sha256={sig}",
        "X-ZhiXi-Timestamp": ts_str,
    }


def verify_webhook_signature(
    body: bytes | str,
    secret: str,
    signature: str,
    timestamp: str | int,
    *,
    max_age_seconds: int = 300,
) -> bool:
    """验证 webhook 签名（供 webhook 接收端使用，与 sign_webhook_payload 配对）。

    参数:
        body: 收到的原始请求体
        secret: 共享密钥
        signature: X-ZhiXi-Signature header 的值 (格式 "sha256=...")
        timestamp: X-ZhiXi-Timestamp header 的值
        max_age_seconds: 最大允许的时钟偏移（秒），默认 5 分钟

    返回:
        True 签名有效，False 无效。
    """
    if isinstance(body, str):
        body = body.encode("utf-8")

    # 防重放：检查时间戳
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        log.warning("webhook_invalid_timestamp", timestamp=timestamp)
        return False

    now = int(time.time())
    if abs(now - ts) > max_age_seconds:
        log.warning(
            "webhook_timestamp_too_old",
            timestamp=ts,
            now=now,
            age=abs(now - ts),
            max_age=max_age_seconds,
        )
        return False

    # 重新计算签名
    payload = f"{ts}\n".encode() + body
    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # 从 "sha256=..." 提取 hex 部分
    received = signature.split("=", 1)[1] if "=" in signature else signature

    # 常量时间比较，防止时序攻击
    valid = hmac.compare_digest(received, expected)
    if not valid:
        log.warning("webhook_signature_mismatch")
    return valid


__all__ = [
    "sign_webhook_payload",
    "verify_webhook_signature",
]
