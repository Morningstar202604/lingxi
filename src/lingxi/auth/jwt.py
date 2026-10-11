"""JWT Token 创建与解码。

🔗 工程逻辑
    为什么用 JWT 而不是 session table？
    - 无状态 — 不需要第三方 session store, 适合单实例部署
    - 自包含 — token 自带 sub/role/tenant_id, 不需要每次查库
    - 横向扩展 — 多实例共享同一个 secret 即可互相验证

    Payload 结构:
    {
        "sub": "username",
        "role": "admin",
        "tenant_id": "default",
        "exp": 1700000000,
        "iat": 1699999900
    }

🤖 AI 避坑
    - decode 失败必须返回 None, 不要抛异常 — 上层 decide 如何处理
    - algorithm 写死 HS256, 不要用 none (PyJWT 2.x 已经禁止使用)
    - 不要把敏感数据放 JWT payload (它是 base64 编码, 不是加密)
    - exp 由 PyJWT 自动校验, 不需要手动比较
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import jwt
from jwt import PyJWTError

from lingxi.core.logging import get_logger

if TYPE_CHECKING:
    from lingxi.auth.models import UserRole

log = get_logger(__name__)


def create_access_token(
    username: str,
    role: UserRole,
    tenant_id: str,
    secret: str,
    expires_minutes: int = 60,
) -> str:
    """创建 JWT access token。

    Args:
        username: 用户名 (subject)
        role: 用户角色
        tenant_id: 租户 ID
        secret: JWT 签名密钥
        expires_minutes: 过期时间 (分钟), 默认 60

    Returns:
        编码后的 JWT 字符串
    """
    now = datetime.now(UTC)
    payload: dict[str, str | int] = {
        "sub": username,
        "role": role.value,
        "tenant_id": tenant_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    token: str = jwt.encode(payload, secret, algorithm="HS256")
    return token


def decode_access_token(token: str, secret: str) -> dict[str, str | int] | None:
    """解码并校验 JWT token。

    校验项:
    - 签名正确性
    - exp (过期时间)
    - 算法为 HS256

    Args:
        token: JWT 字符串
        secret: JWT 签名密钥

    Returns:
        成功返回 payload dict, 失败返回 None
    """
    try:
        payload: dict[str, str | int] = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
        )
        return payload
    except PyJWTError as e:
        log.warning("JWT decode failed", error=str(e))
        return None
