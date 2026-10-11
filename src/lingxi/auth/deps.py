"""FastAPI 鉴权依赖。

🔗 工程逻辑
    FastAPI 的 dependency injection 是鉴权的最自然位置:
    - get_current_user: 解析 Bearer JWT → 返回 User 对象
    - require_permission: 校验 User 的角色是否拥有指定 permission
    - verify_api_key: API Key 认证 (适用自动化脚本/OAuth-less 场景)

    两种认证入口:
    ┌─────────────────┐     ┌────────────────────┐
    │  Bearer JWT     │ --> │  get_current_user  │ --> User
    │  (人用登录)      │     └────────────────────┘
    └─────────────────┘
    ┌─────────────────┐     ┌────────────────────┐
    │  X-API-Key      │ --> │  verify_api_key    │ --> User
    │  (自动化脚本)    │     └────────────────────┘
    └─────────────────┘

🤖 AI 避坑
    - HTTPAuthorizationCredentials 自动解析 "Bearer xxx" 格式
    - require_permission 是闭包, 必须返回 Depends 可消费的类型
    - API Key 映射是静态的, 生产应查 DB 或 Redis
    - 禁用用户立即生效 (token 仍有效, 但 fetch 不到用户 → 401)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lingxi.auth.jwt import decode_access_token
from lingxi.auth.models import ROLE_PERMISSIONS, Permission, User, UserStore, get_user_store
from lingxi.config.settings import get_settings
from lingxi.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

# ── 安全方案 ───────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)


# ── API Key 静态映射 ────────────────────────────────────────────

_API_KEY_MAP: dict[str, dict[str, str]] = {
    "zxk_admin_xxx": {
        "username": "admin",
        "description": "Default admin API key",
    },
    "zxk_operator_xxx": {
        "username": "operator",
        "description": "Default operator API key",
    },
}


# ── 依赖函数 ───────────────────────────────────────────────────

def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
) -> User:
    """从 Authorization: Bearer <JWT> 获取当前用户。

    流程:
    1. 解析 HTTPBearer credentials
    2. 解码 JWT (校验签名 + exp)
    3. 查找用户 (校验存在 + 未禁用)

    Raises:
        HTTPException(401): 认证失败
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings = get_settings()
    secret = settings.auth.jwt_secret

    payload = decode_access_token(credentials.credentials, secret)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    username = payload.get("sub")
    if not isinstance(username, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    store: UserStore = get_user_store()
    user = store.get(username)
    if user is None:
        log.warning("get_current_user: user not found in store", username=username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.disabled:
        log.warning("get_current_user: user disabled", username=username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User is disabled",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def require_permission(permission: Permission) -> Callable[[User], User]:
    """返回依赖工厂: 校验当前用户是否拥有指定权限。

    用法::

        @router.post("/tickets")
        async def create_ticket(
            user: Annotated[User, Depends(require_permission(Permission.TICKETS_WRITE))]
        ): ...

    Args:
        permission: 要求拥有的权限

    Raises:
        HTTPException(403): 权限不足
    """

    def _check(current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if permission not in ROLE_PERMISSIONS.get(current_user.role, set()):
            log.warning(
                "Permission denied",
                username=current_user.username,
                required=permission.value,
                role=current_user.role.value,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions: {permission.value} required",
            )
        return current_user

    return _check


def verify_api_key(
    x_api_key: Annotated[str | None, Header(None)] = None,
) -> User:
    """通过 X-API-Key header 认证。

    适用于自动化脚本、CLI 工具、不适合人类交互 Bearer token 的场景。
    API Key 前缀: zxk_

    Args:
        x_api_key: X-API-Key header 值

    Raises:
        HTTPException(401): Key 无效
        HTTPException(404): 映射用户不存在
    """
    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    mapping = _API_KEY_MAP.get(x_api_key)
    if mapping is None:
        log.warning("verify_api_key: invalid key", key_prefix=x_api_key[:8] + "...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    username = mapping["username"]
    store: UserStore = get_user_store()
    user = store.get(username)
    if user is None:
        # API key 映射的用户不存在 (如 admin 被删了)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{username}' mapped by API key not found",
        )

    if user.disabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User mapped by API key is disabled",
        )

    return user
