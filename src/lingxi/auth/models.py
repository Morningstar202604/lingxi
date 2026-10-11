"""RBAC 数据模型与内存用户存储。

🔗 工程逻辑
    RBAC 三要素:
    1. Role (角色) → 岗位级别 (ADMIN/OPERATOR/VIEWER/API)
    2. Permission (权限) → 原子操作 (tickets.read, knowledge.write ...)
    3. 映射 (ROLE_PERMISSIONS) → 角色拥有权限的集合

    设计决策:
    - Role 与 Permission 用独立 Enum, 多对多关系灵活组合
    - VIEWER 仅有只读权限, 不能改数据 — 适合值班大屏展示
    - API role 给自动化脚本用, 只有知识库读写, 限制攻击面
    - tenant_id 默认 "default", 支持多租户隔离

🤖 AI 避坑
    - 不要在 Permission 里加通配符 (如 "*"), 显式列举更安全
    - UserStore 是内存 dict, 进程重启数据丢失
    - sha256 + salt 不够安全, 生产环境换 argon2-cffi
    - User dataclass 与 JWT payload 不同, 不要混淆
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from enum import StrEnum

from lingxi.core.logging import get_logger

log = get_logger(__name__)


class UserRole(StrEnum):
    """用户角色枚举。"""

    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"
    API = "api"


class Permission(StrEnum):
    """细粒度权限枚举 — 资源的 CRUD 原子操作。"""

    # 工单
    TICKETS_READ = "tickets.read"
    TICKETS_WRITE = "tickets.write"
    # 队列
    QUEUE_READ = "queue.read"
    QUEUE_WRITE = "queue.write"
    # 知识库
    KNOWLEDGE_READ = "knowledge.read"
    KNOWLEDGE_WRITE = "knowledge.write"
    # 规则
    RULES_READ = "rules.read"
    RULES_WRITE = "rules.write"
    # 审计
    AUDIT_READ = "audit.read"
    # 人格
    PERSONA_WRITE = "persona.write"
    # 指标
    METRICS_READ = "metrics.read"
    # 用户管理
    USER_MANAGE = "user.manage"


# ── 角色 → 权限映射 ─────────────────────────────────────────────

ROLE_PERMISSIONS: dict[UserRole, set[Permission]] = {
    UserRole.ADMIN: {
        Permission.TICKETS_READ,
        Permission.TICKETS_WRITE,
        Permission.QUEUE_READ,
        Permission.QUEUE_WRITE,
        Permission.KNOWLEDGE_READ,
        Permission.KNOWLEDGE_WRITE,
        Permission.RULES_READ,
        Permission.RULES_WRITE,
        Permission.AUDIT_READ,
        Permission.PERSONA_WRITE,
        Permission.METRICS_READ,
        Permission.USER_MANAGE,
    },
    UserRole.OPERATOR: {
        Permission.TICKETS_READ,
        Permission.TICKETS_WRITE,
        Permission.QUEUE_READ,
        Permission.QUEUE_WRITE,
        Permission.KNOWLEDGE_READ,
        Permission.KNOWLEDGE_WRITE,
        Permission.RULES_READ,
        Permission.AUDIT_READ,
    },
    UserRole.VIEWER: {
        Permission.TICKETS_READ,
        Permission.QUEUE_READ,
        Permission.KNOWLEDGE_READ,
    },
    UserRole.API: {
        Permission.KNOWLEDGE_READ,
        Permission.KNOWLEDGE_WRITE,
        Permission.RULES_READ,
        Permission.RULES_WRITE,
    },
}


# ── User 模型 ───────────────────────────────────────────────────


@dataclass
class User:
    """内存中的用户实体。

    不包含密码明文, 仅存 salt 与 hash。
    """

    username: str
    role: UserRole
    tenant_id: str = "default"
    disabled: bool = False
    password_hash: str = ""
    password_salt: str = ""

    def has_permission(self, permission: Permission) -> bool:
        """检查当前角色是否拥有指定权限。"""
        return permission in ROLE_PERMISSIONS.get(self.role, set())


@dataclass
class UserStore:
    """基于内存 dict 的用户存储。

    提供 CRUD 与密码校验, 内置默认用户。
    生产环境应替换为 DB-backed 实现 (PostgreSQL / Redis 等)。
    """

    _users: dict[str, User] = field(default_factory=dict)
    _salts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """初始化内置默认用户。"""
        if not self._users:
            self._seed_defaults()

    def _hash_password(self, password: str, salt: str) -> str:
        """SHA-256 + salt 哈希。

        生产环境应替换为 argon2-cffi 的 PasswordHasher。
        """
        return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()

    def _generate_salt(self) -> str:
        """生成随机 salt。"""
        return secrets.token_hex(16)

    def _seed_defaults(self) -> None:
        """写入内置默认用户。"""
        defaults = [
            ("admin", "admin123", UserRole.ADMIN, "default"),
            ("viewer", "view123", UserRole.VIEWER, "default"),
        ]
        for username, password, role, tenant in defaults:
            salt = self._generate_salt()
            pw_hash = self._hash_password(password, salt)
            user = User(
                username=username,
                role=role,
                tenant_id=tenant,
                disabled=False,
                password_hash=pw_hash,
                password_salt=salt,
            )
            self._users[username] = user
            self._salts[username] = salt
        log.info("UserStore seeded", count=len(defaults))

    def create(
        self,
        username: str,
        password: str,
        role: UserRole,
        tenant_id: str = "default",
    ) -> User | None:
        """创建新用户。返回 User 或 None (已存在)。"""
        if username in self._users:
            log.warning("UserStore.create: user exists", username=username)
            return None
        salt = self._generate_salt()
        pw_hash = self._hash_password(password, salt)
        user = User(
            username=username,
            role=role,
            tenant_id=tenant_id,
            disabled=False,
            password_hash=pw_hash,
            password_salt=salt,
        )
        self._users[username] = user
        self._salts[username] = salt
        log.info("UserStore.create: user created", username=username, role=role.value)
        return user

    def get(self, username: str) -> User | None:
        """按用户名查找用户。"""
        return self._users.get(username)

    def verify_password(self, username: str, password: str) -> bool:
        """校验用户密码。"""
        user = self._users.get(username)
        if user is None:
            return False
        expected = self._hash_password(password, user.password_salt)
        return secrets.compare_digest(expected, user.password_hash)

    def list_users(self) -> list[User]:
        """列出所有用户。"""
        return list(self._users.values())

    def delete(self, username: str) -> bool:
        """删除用户。"""
        if username in self._users:
            del self._users[username]
            self._salts.pop(username, None)
            log.info("UserStore.delete", username=username)
            return True
        return False


# ── 全局单例 ───────────────────────────────────────────────────

_user_store: UserStore | None = None


def get_user_store() -> UserStore:
    """获取全局 UserStore 单例。"""
    global _user_store
    if _user_store is None:
        _user_store = UserStore()
    return _user_store
