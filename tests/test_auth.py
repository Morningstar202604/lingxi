"""JWT RBAC 鉴权测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from lingxi.auth import (
    Permission,
    UserRole,
    UserStore,
    create_access_token,
    decode_access_token,
)
from lingxi.auth.router import create_auth_router

# 测试用凭据。刻意不用任何"看起来像产品默认值"的串——
# 本文件过去把 admin/admin123、viewer/view123 当作契约锁死，
# 等于用测试为一对公开后门口令背书。
_TEST_JWT_SECRET = "unit-test-jwt-secret-0123456789abcdef-32b+"
_TEST_ADMIN_PASSWORD = "bootstrap-passwd-for-tests"


@pytest.fixture()
def auth_env(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """给全局 Settings 单例注入合格密钥与引导管理员口令，并重置用户存储。

    必须在校验/构造 UserStore 之前改好 settings：播种发生在构造时。
    返回 (jwt_secret, admin_password)。
    """
    import lingxi.auth.models as auth_models
    from lingxi.config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings.auth, "jwt_secret", _TEST_JWT_SECRET, raising=False)
    monkeypatch.setattr(
        settings.auth, "admin_password", SecretStr(_TEST_ADMIN_PASSWORD), raising=False
    )
    monkeypatch.setattr(settings.auth, "admin_username", "admin", raising=False)
    monkeypatch.setattr(settings.auth, "api_key_hashes", {}, raising=False)

    # 让下一次 get_user_store() 用新配置重新播种
    monkeypatch.setattr(auth_models, "_user_store", None, raising=False)
    return _TEST_JWT_SECRET, _TEST_ADMIN_PASSWORD


class TestJwtToken:
    """JWT 编码/解码测试。"""

    def test_create_and_decode(self) -> None:
        token = create_access_token("admin", UserRole.ADMIN, "default", "test-secret-key-for-testing-32bytes!")
        payload = decode_access_token(token, "test-secret-key-for-testing-32bytes!")
        assert payload is not None
        assert payload["sub"] == "admin"
        assert payload["role"] == "admin"
        assert payload["tenant_id"] == "default"

    def test_decode_wrong_secret_fails(self) -> None:
        token = create_access_token("admin", UserRole.ADMIN, "t", "right-secret-long-enough-for-hmac-sha256!")
        assert decode_access_token(token, "wrong-secret-long-enough-for-hmac-sha256!") is None

    def test_decode_invalid_token(self) -> None:
        assert decode_access_token("not.a.token", "test-secret-key-for-testing-32bytes!") is None

    def test_token_has_expiry(self) -> None:
        token = create_access_token("user", UserRole.VIEWER, "t", "unit-test-secret-long-enough-for-hmac-sha256!", expires_minutes=30)
        payload = decode_access_token(token, "unit-test-secret-long-enough-for-hmac-sha256!")
        assert payload is not None
        assert "exp" in payload
        assert "iat" in payload
        assert payload["exp"] > payload["iat"]

    def test_token_expires(self) -> None:
        """过期 token 解码应失败。"""
        token = create_access_token("user", UserRole.VIEWER, "t", "unit-test-secret-long-enough-for-hmac-sha256!", expires_minutes=-1)
        assert decode_access_token(token, "unit-test-secret-long-enough-for-hmac-sha256!") is None

    def test_custom_expiry(self) -> None:
        token = create_access_token("u", UserRole.VIEWER, "t", "unit-test-secret-long-enough-for-hmac-sha256!", expires_minutes=120)
        payload = decode_access_token(token, "unit-test-secret-long-enough-for-hmac-sha256!")
        assert payload is not None
        # 应当在 ~120 分钟后 (允许 1 秒误差)
        assert payload["exp"] - payload["iat"] == 7200


class TestUserStore:
    """用户存储测试。"""

    def test_no_users_without_bootstrap(self) -> None:
        """未配置 admin_password 时不得存在任何账号（旧的 admin/viewer 已删除）。"""
        store = UserStore()
        assert store.get("admin") is None
        assert store.get("viewer") is None
        assert store.list_users() == []

    def test_verify_correct_password(self, auth_env: tuple[str, str]) -> None:
        _, password = auth_env
        store = UserStore()
        assert store.verify_password("admin", password)
        # 曾经的内置口令必须不再可用
        assert not store.verify_password("admin", "admin123")
        assert not store.verify_password("viewer", "view123")

    def test_verify_wrong_password(self, auth_env: tuple[str, str]) -> None:
        store = UserStore()
        assert not store.verify_password("admin", "wrong")
        assert not store.verify_password("nonexistent", "any")

    def test_create_user(self) -> None:
        store = UserStore()
        user = store.create("operator1", "pass123", UserRole.OPERATOR)
        assert user is not None
        assert user.username == "operator1"
        assert user.role == UserRole.OPERATOR
        assert store.verify_password("operator1", "pass123")

    def test_create_duplicate_returns_none(self) -> None:
        store = UserStore()
        store.create("new_u", "p", UserRole.VIEWER)
        assert store.create("new_u", "p", UserRole.VIEWER) is None

    def test_list_users(self, auth_env: tuple[str, str]) -> None:
        store = UserStore()
        users = store.list_users()
        assert len(users) == 1  # 仅引导出的 admin，不再有第二个内置 viewer
        assert users[0].username == "admin"

    def test_delete_user(self) -> None:
        store = UserStore()
        store.create("del_me", "p", UserRole.VIEWER)
        assert store.delete("del_me")
        assert store.get("del_me") is None
        assert not store.delete("del_me")  # 已删除


class TestRBAC:
    """权限映射测试。"""

    def test_admin_has_all_permissions(self) -> None:
        from lingxi.auth.models import ROLE_PERMISSIONS
        perms = ROLE_PERMISSIONS[UserRole.ADMIN]
        assert Permission.TICKETS_READ in perms
        assert Permission.USER_MANAGE in perms
        assert Permission.KNOWLEDGE_WRITE in perms

    def test_viewer_is_readonly(self) -> None:
        from lingxi.auth.models import ROLE_PERMISSIONS
        perms = ROLE_PERMISSIONS[UserRole.VIEWER]
        assert Permission.TICKETS_READ in perms
        assert Permission.TICKETS_WRITE not in perms
        assert Permission.KNOWLEDGE_WRITE not in perms

    def test_api_role_limited(self) -> None:
        from lingxi.auth.models import ROLE_PERMISSIONS
        perms = ROLE_PERMISSIONS[UserRole.API]
        assert Permission.KNOWLEDGE_WRITE in perms
        assert Permission.USER_MANAGE not in perms
        assert Permission.AUDIT_READ not in perms

    def test_operator_permissions(self) -> None:
        from lingxi.auth.models import ROLE_PERMISSIONS
        perms = ROLE_PERMISSIONS[UserRole.OPERATOR]
        assert Permission.TICKETS_WRITE in perms
        assert Permission.AUDIT_READ in perms
        assert Permission.USER_MANAGE not in perms


class TestAuthRouter:
    """Auth HTTP 端点测试。"""

    @pytest.fixture()
    def client(self, auth_env: tuple[str, str]) -> TestClient:
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(create_auth_router())
        return TestClient(app)

    def test_login_success(self, client: TestClient, auth_env: tuple[str, str]) -> None:
        _, password = auth_env
        resp = client.post("/api/auth/login", json={"username": "admin", "password": password})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_rejects_historical_default_password(
        self, client: TestClient
    ) -> None:
        """admin/admin123 曾是源码里的内置口令，必须不再能登录。"""
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert resp.status_code == 401

    def test_login_wrong_password(self, client: TestClient) -> None:
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client: TestClient) -> None:
        resp = client.post("/api/auth/login", json={"username": "ghost", "password": "any"})
        assert resp.status_code == 401

    def test_login_without_secret_returns_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """密钥缺失时绝不退化签发——那种 token 任何读过源码的人都能伪造。"""
        from lingxi.config.settings import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings.auth, "jwt_secret", None, raising=False)
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": _TEST_ADMIN_PASSWORD},
        )
        assert resp.status_code == 503

    def test_me_endpoint(self, client: TestClient, auth_env: tuple[str, str]) -> None:
        _, password = auth_env
        login_resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": password}
        )
        token = login_resp.json()["access_token"]
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"
        assert data["role"] == "admin"

    def test_me_no_token_fails(self, client: TestClient) -> None:
        resp = client.get("/api/auth/me")
        assert resp.status_code == 403 or resp.status_code == 401

    def test_permissions_endpoint(self, client: TestClient, auth_env: tuple[str, str]) -> None:
        """viewer 不再是内置账号，测试里显式建一个再登录。"""
        from lingxi.auth.models import get_user_store

        store = get_user_store()
        assert store.create("viewer", "viewer-pass-for-tests", UserRole.VIEWER) is not None
        login_resp = client.post(
            "/api/auth/login", json={"username": "viewer", "password": "viewer-pass-for-tests"}
        )
        token = login_resp.json()["access_token"]
        resp = client.get("/api/auth/permissions", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert "tickets.read" in data["permissions"]
        assert "user.manage" not in data["permissions"]
