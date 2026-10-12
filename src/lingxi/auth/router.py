"""鉴权 API 路由 — 登录、用户管理、权限查询。

🔗 工程逻辑
    RESTful 接口设计:
    - POST /login        → 获取 JWT token (不需要认证)
    - GET  /me           → 当前用户信息 (需要 Bearer JWT)
    - GET  /users        → 列出所有用户 (需要 ADMIN)
    - POST /users        → 创建新用户 (需要 ADMIN)
    - GET  /permissions  → 当前用户权限列表 (需要 Bearer JWT)

    返回格式统一: {"detail": "..."} 用于错误, 业务数据直接返回。

🤖 AI 避坑
    - login 不校验 auth.enabled, 因为认证总是要能 login
    - 不要返回 User 的 password_hash / password_salt 字段
    - 创建用户时 role 必须来自 UserRole 枚举
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from lingxi.auth.deps import get_current_user, require_permission
from lingxi.auth.jwt import (
    JwtSecretNotConfiguredError,
    create_access_token,
    resolve_signing_secret,
)
from lingxi.auth.models import (
    ROLE_PERMISSIONS,
    Permission,
    User,
    UserRole,
    UserStore,
    get_user_store,
)
from lingxi.config.settings import get_settings
from lingxi.core.logging import get_logger

log = get_logger(__name__)


def create_auth_router() -> APIRouter:
    """工厂函数：创建并返回 auth 路由（方便 dashboard_api 集成）。"""
    return router


router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Request/Response 模型 ──────────────────────────────────────


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    username: str
    role: str
    tenant_id: str
    disabled: bool


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: UserRole
    tenant_id: str = "default"


class PermissionsResponse(BaseModel):
    username: str
    role: str
    permissions: list[str]


# ── 路由 ───────────────────────────────────────────────────────


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """用户登录, 返回 JWT access token。"""
    store: UserStore = get_user_store()

    if not store.verify_password(body.username, body.password):
        log.warning("login failed", username=body.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = store.get(body.username)
    if user is None or user.disabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User disabled",
        )

    settings = get_settings()
    try:
        secret = resolve_signing_secret(settings.auth.jwt_secret)
    except JwtSecretNotConfiguredError as e:
        # 绝不退化用默认密钥签发：那种 token 任何读过源码的人都能伪造
        log.error("login_jwt_secret_unconfigured", username=user.username)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JWT signing secret is not configured",
        ) from e

    token = create_access_token(
        username=user.username,
        role=user.role,
        tenant_id=user.tenant_id,
        secret=secret,
        expires_minutes=settings.auth.jwt_expire_minutes,
    )

    log.info("login success", username=user.username, role=user.role.value)
    return LoginResponse(access_token=token, token_type="bearer")


@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserResponse:
    """获取当前登录用户信息。"""
    return UserResponse(
        username=current_user.username,
        role=current_user.role.value,
        tenant_id=current_user.tenant_id,
        disabled=current_user.disabled,
    )


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    current_user: Annotated[User, Depends(require_permission(Permission.USER_MANAGE))],
) -> list[UserResponse]:
    """列出所有用户 (需要 USER_MANAGE 权限)。"""
    store: UserStore = get_user_store()
    users = store.list_users()
    return [
        UserResponse(
            username=u.username,
            role=u.role.value,
            tenant_id=u.tenant_id,
            disabled=u.disabled,
        )
        for u in users
    ]


@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    body: CreateUserRequest,
    current_user: Annotated[User, Depends(require_permission(Permission.USER_MANAGE))],
) -> UserResponse:
    """创建新用户 (需要 USER_MANAGE 权限)。"""
    store: UserStore = get_user_store()
    user = store.create(
        username=body.username,
        password=body.password,
        role=body.role,
        tenant_id=body.tenant_id,
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User '{body.username}' already exists",
        )
    log.info(
        "user created",
        creator=current_user.username,
        new_user=body.username,
        role=body.role.value,
    )
    return UserResponse(
        username=user.username,
        role=user.role.value,
        tenant_id=user.tenant_id,
        disabled=user.disabled,
    )


@router.get("/permissions", response_model=PermissionsResponse)
async def get_permissions(
    current_user: Annotated[User, Depends(get_current_user)],
) -> PermissionsResponse:
    """列出当前用户拥有的所有权限。"""
    perms = ROLE_PERMISSIONS.get(current_user.role, set())
    return PermissionsResponse(
        username=current_user.username,
        role=current_user.role.value,
        permissions=sorted(p.value for p in perms),
    )
