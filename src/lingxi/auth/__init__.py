"""JWT RBAC 多租户鉴权模块。

🔗 工程逻辑
    为什么独立 auth 包而不是塞进 security？
    - security 专注数据保护（PII 脱敏、内容护栏）
    - auth 专注身份认证与访问控制（JWT、RBAC、多租户）
    三者各司其职: security.pii 处理数据、security.guardrails 处理内容安全、
    auth 处理"你是谁、你能做什么"。

    认证链路:
    ┌──────────────┐    Bearer JWT     ┌──────────────┐    role→permissions    ┌──────────┐
    │  Login API   │ ───────────────> │  decode JWT  │ ──────────────────> │  RBAC    │
    │  (issue)     │                  │  validate    │                      │  check   │
    └──────────────┘                  └──────────────┘                      └──────────┘

    多租户隔离通过 token 中携带的 tenant_id 实现, 下游服务在读/写资源时
    应当校验当前用户 tenant_id 与目标资源 tenant_id 一致。

🤖 AI 避坑
    - JWT secret 必须通过 AuthConfig.jwt_secret 配置, 默认值仅用于开发
    - password 哈希使用 sha256+salt 仅为占位实现, 生产必须换 argon2
    - UserStore 是内存存储, 进程重启数据丢失 — 生产需替换为持久化后端
    - 不要在 JWT payload 里塞敏感数据 (它是 base64 不是加密)
"""
from lingxi.auth.deps import get_current_user, require_permission, verify_api_key
from lingxi.auth.jwt import create_access_token, decode_access_token
from lingxi.auth.models import (
    ROLE_PERMISSIONS,
    Permission,
    User,
    UserRole,
    UserStore,
    get_user_store,
)
from lingxi.auth.router import create_auth_router

__all__ = [
    # models
    "UserRole",
    "Permission",
    "ROLE_PERMISSIONS",
    "User",
    "UserStore",
    "get_user_store",
    # jwt
    "create_access_token",
    "decode_access_token",
    # deps
    "get_current_user",
    "require_permission",
    "verify_api_key",
    # router
    "create_auth_router",
]
