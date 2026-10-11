"""JWT RBAC 鉴权测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lingxi.auth import (
    Permission,
    UserRole,
    UserStore,
    create_access_token,
    decode_access_token,
)
from lingxi.auth.router import create_auth_router


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

    def test_default_users_exist(self) -> None:
        store = UserStore()
        assert store.get("admin") is not None
        assert store.get("viewer") is not None

    def test_verify_correct_password(self) -> None:
        store = UserStore()
        assert store.verify_password("admin", "admin123")
        assert store.verify_password("viewer", "view123")

    def test_verify_wrong_password(self) -> None:
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

    def test_list_users(self) -> None:
        store = UserStore()
        users = store.list_users()
        assert len(users) >= 2  # admin + viewer 默认

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
    def client(self) -> TestClient:
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(create_auth_router())
        return TestClient(app)

    def test_login_success(self, client: TestClient) -> None:
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_wrong_password(self, client: TestClient) -> None:
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client: TestClient) -> None:
        resp = client.post("/api/auth/login", json={"username": "ghost", "password": "any"})
        assert resp.status_code == 401

    def test_me_endpoint(self, client: TestClient) -> None:
        # 先登录
        login_resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        token = login_resp.json()["access_token"]
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"
        assert data["role"] == "admin"

    def test_me_no_token_fails(self, client: TestClient) -> None:
        resp = client.get("/api/auth/me")
        assert resp.status_code == 403 or resp.status_code == 401

    def test_permissions_endpoint(self, client: TestClient) -> None:
        login_resp = client.post("/api/auth/login", json={"username": "viewer", "password": "view123"})
        token = login_resp.json()["access_token"]
        resp = client.get("/api/auth/permissions", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert "tickets.read" in data["permissions"]
        assert "user.manage" not in data["permissions"]
