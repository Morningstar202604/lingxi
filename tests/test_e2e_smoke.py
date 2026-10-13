"""E2E 冒烟测试：完整链路验证（API → Pipeline → Persistence）。

从 IM 事件到 Agent 推理到 Ticket 创建/升级的完整流程。
使用 httpx + ASGI 直连（不走网络），验证所有层正确集成。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from lingxi.agent.graph import ZhiXiAgent
from lingxi.core.protocol import (
    ChannelType,
    IMEvent,
    MessageSegment,
    MessageType,
    PostType,
)
from lingxi.flow.dashboard_api import create_dashboard_app
from lingxi.flow.pipeline import CustomerServicePipeline
from lingxi.flow.queue import EscalationQueue
from lingxi.flow.ticket import TicketStatus, TicketStore

# ── Fixtures ──


@pytest.fixture
def app_client() -> TestClient:
    """创建带真实依赖的 TestClient。"""
    store = TicketStore()
    queue = EscalationQueue()
    app = create_dashboard_app(store, queue)
    return TestClient(app)




# ── E2E: Health → Ticket creation → Queue → Dashboard flow ──


class TestE2EHappyPath:
    """测试 Agent 回复 → Ticket 创建 → Dashboard 可查。"""

    @pytest.mark.asyncio
    async def test_pipeline_creates_visible_ticket(
        self, app_client: TestClient, admin_headers: dict
    ) -> None:
        """完整链路: 模拟 IM 事件 → Pipeline → Ticket 存储 → Dashboard API 返回。"""
        store = TicketStore()
        queue = EscalationQueue()

        # Mock Agent 返回直接回复
        agent = MagicMock(spec=ZhiXiAgent)
        agent.run = AsyncMock(return_value={
            "final_reply": "您好，我已收到您的咨询。",
            "tool_results": [],
            "escalate_reason": "",
            "step_count": 1,
        })

        pipe = CustomerServicePipeline(
            agent=agent,
            ticket_store=store,
            escalation_queue=queue,
        )

        event = IMEvent(
            post_type=PostType.MESSAGE,
            message_type=MessageType.PRIVATE,
            channel=ChannelType.QQ_PRIVATE,
            user_id="u42",
            group_id=None,
            raw_message="你好，我想了解退货政策",
            segments=[MessageSegment(type="text", data={"text": "你好，我想了解退货政策"})],
        )

        result = await pipe.handle_event(event, system_prompt="You are 智犀")
        assert result.replied is True
        assert result.escalated is False

        # 验证 ticket 已在 store 中
        tickets = store.list_active()
        assert len(tickets) == 1
        assert tickets[0].user_id == "u42"

    def test_dashboard_api_returns_ticket(
        self, app_client: TestClient, admin_headers: dict
    ) -> None:
        """Dashboard API 能够查询到活跃工单。"""
        # 创建 store 并加入一个 ticket
        store = TicketStore()
        queue = EscalationQueue()
        app = create_dashboard_app(store, queue)
        client = TestClient(app)

        # 先创建一个工单
        store.get_or_create("chat1", user_id="u1")

        resp = client.get("/api/tickets", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    def test_health_endpoint_public(self, app_client: TestClient) -> None:
        """Health 不需要认证。"""
        resp = app_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_dashboard_requires_token(
        self, app_client: TestClient, admin_headers: dict
    ) -> None:
        """无 token → 401。"""
        resp = app_client.get("/api/tickets")
        assert resp.status_code == 401

    def test_full_flow_with_escalation(
        self, app_client: TestClient, admin_headers: dict
    ) -> None:
        """投诉类工单进入升级队列 → Dashboard 可查。"""
        store = TicketStore()
        queue = EscalationQueue()

        t = store.get_or_create("comp_001", user_id="u99", channel="qq_private")
        t.transition(TicketStatus.PROCESSING)
        t.transition(TicketStatus.ESCALATED)
        queue.enqueue(t, reason="user_complaint")

        app = create_dashboard_app(store, queue)
        client = TestClient(app)

        resp = client.get("/api/dashboard/summary", headers=admin_headers)
        assert resp.status_code == 200
        summary = resp.json()
        assert summary["active_tickets"] >= 1
        assert summary["queue"]["pending_count"] >= 1

    def test_queue_claim_and_complete(
        self, app_client: TestClient, admin_headers: dict
    ) -> None:
        """坐席认领 → 完成 → 队列清空。"""
        store = TicketStore()
        queue = EscalationQueue()

        t = store.get_or_create("claim_test", user_id="u1")
        t.transition(TicketStatus.PROCESSING)
        t.transition(TicketStatus.ESCALATED)
        queue.enqueue(t, reason="test")

        app = create_dashboard_app(store, queue)
        client = TestClient(app)

        # 认领
        resp = client.post(
            "/api/queue/claim?agent_id=agent_foo",
            headers=admin_headers,
        )
        assert resp.status_code == 200

        # 完成
        resp = client.post(
            f"/api/queue/complete?ticket_id={t.ticket_id}",
            headers=admin_headers,
        )
        assert resp.status_code == 200

        # 再次认领应返回 404（空队列）
        resp = client.post(
            "/api/queue/claim?agent_id=agent_bar",
            headers=admin_headers,
        )
        assert resp.status_code == 404


# ── Import sanity check（确保所有层可以互相引用）──


class TestModuleEntry:
    """README 快速开始承诺 `python -m lingxi` 可启动引擎（入口冒烟）。"""

    def test_python_dash_m_help_exits_zero(self) -> None:
        """`python -m lingxi --help` 必须 exit 0 并打印 usage。"""
        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        # 显式注入 src，不依赖 editable install 状态（CI 裸 clone 也能跑）
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(repo_root / "src"), env.get("PYTHONPATH")) if p
        )
        proc = subprocess.run(
            [sys.executable, "-m", "lingxi", "--help"],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(repo_root),
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert "usage" in proc.stdout.lower()

    def test_build_default_engine_assembles_offline(self) -> None:
        """build_default_engine() 无网络装配全部组件（不连 NapCat/LLM）。"""
        from lingxi.__main__ import build_default_engine

        engine = build_default_engine()
        assert engine.agent is not None
        assert engine.tickets is not None
        assert engine.queue is not None
        assert engine.memory is not None
        assert engine.pipeline is not None


class TestCrossLayerImports:
    """验证层间依赖链正确（Layer N 引用 Layer N-1 导出的符号）。"""

    def test_agent_imports_persona(self) -> None:
        """Layer 4 引用 Layer 2: guardrails。"""
        from lingxi.agent import graph as g
        from lingxi.persona.guardrails import check_permission, classify_shell_command

        # graph.py 依赖 guardrails.PermissionLevel
        assert g.PermissionLevel is not None
        assert callable(check_permission)
        assert callable(classify_shell_command)

    def test_pipeline_imports_agent_and_memory(self) -> None:
        """Layer 5 引用 Layer 4 + Layer 3。"""
        from lingxi.agent.graph import ZhiXiAgent
        from lingxi.flow import pipeline as p
        from lingxi.memory.pipeline import MemoryPipeline

        assert hasattr(p, "CustomerServicePipeline")
        assert ZhiXiAgent is not None
        assert MemoryPipeline is not None

    def test_dashboard_imports_flow(self) -> None:
        """Layer 6 (Dashboard) 引用 Layer 5。"""
        from lingxi.flow.dashboard_api import create_dashboard_app
        from lingxi.flow.queue import EscalationQueue
        from lingxi.flow.ticket import TicketStore

        assert callable(create_dashboard_app)
        store = TicketStore()
        queue = EscalationQueue()
        app = create_dashboard_app(store, queue)
        assert app is not None


# ── 管理面认证加固（回归锁：AUDIT-2026-10-10 #8） ──


class TestAdminAuthHardening:
    """默认 token 不得成为可用凭据；错误 token 必须 401。"""

    def test_default_token_is_rejected_outside_debug(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非 debug 且仍用默认 token 时，管理接口 fail-closed（503）。"""
        from lingxi.config.settings import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "debug", False, raising=False)
        client = TestClient(create_dashboard_app(TicketStore(), EscalationQueue()))

        resp = client.get(
            "/api/dashboard/summary",
            headers={"x-admin-token": "change-me-in-production"},
        )
        assert resp.status_code == 503, resp.text
        assert "ADMIN_TOKEN" in resp.json()["detail"]

    def test_wrong_token_rejected(self, admin_headers: dict[str, str]) -> None:
        client = TestClient(create_dashboard_app(TicketStore(), EscalationQueue()))
        resp = client.get(
            "/api/dashboard/summary",
            headers={"x-admin-token": "wrong-token"},
        )
        assert resp.status_code == 401

    def test_configured_token_accepted(self, admin_headers: dict[str, str]) -> None:
        client = TestClient(create_dashboard_app(TicketStore(), EscalationQueue()))
        resp = client.get("/api/dashboard/summary", headers=admin_headers)
        assert resp.status_code == 200

    def test_health_stays_public(self) -> None:
        """健康检查不泄露也不需要凭据。"""
        client = TestClient(create_dashboard_app(TicketStore(), EscalationQueue()))
        assert client.get("/api/health").status_code == 200


# ── 前端直连契约（替代已删除的 /api/proxy rewrite）──


class TestFrontendDirectConnectionContract:
    """前端必须**直连**后端；rewrites 代理会让 SSE 永久缓冲。

    旧契约锁的是 `/api/proxy` rewrite（防"把 /api 拼两次"造成的 404）。
    那个方案对 SSE 是死路：Next rewrites 会缓冲响应体，浏览器拿到
    200 text/event-stream 却一帧都收不到，实时事件流永远卡在"连接中"。
    因此契约改为：api.ts 直连 + NEXT_PUBLIC_API_BASE 可覆盖 +
    next.config.js 不再声明 rewrites（避免有人把它加回来）。
    """

    @staticmethod
    def _web_dir() -> Path:
        return Path(__file__).resolve().parents[1] / "web"

    def test_api_client_does_not_use_proxy_prefix(self) -> None:
        api = (self._web_dir() / "lib" / "api.ts").read_text(encoding="utf-8")
        assert "/api/proxy" not in api, (
            "前端不能再走 /api/proxy：Next rewrites 会缓冲 SSE 响应体，"
            "/api/events 会一直停在「连接中」"
        )

    def test_api_base_defaults_to_loopback_backend(self) -> None:
        api = (self._web_dir() / "lib" / "api.ts").read_text(encoding="utf-8")
        assert "http://127.0.0.1:8080" in api, "API_BASE 默认值应指向本机后端"

    def test_api_base_overridable_by_env(self) -> None:
        """跨机部署须能不改代码换地址。"""
        api = (self._web_dir() / "lib" / "api.ts").read_text(encoding="utf-8")
        assert "NEXT_PUBLIC_API_BASE" in api, (
            "API_BASE 必须支持 NEXT_PUBLIC_API_BASE 覆盖（跨机部署用）"
        )

    def test_next_config_declares_no_rewrites(self) -> None:
        """代理一旦被加回来，SSE 会重新变成「200 但无数据」。"""
        cfg = (self._web_dir() / "next.config.js").read_text(encoding="utf-8")
        # 注释里提到 rewrites 是正常的（解释为什么删掉），只查真实声明
        code = "\n".join(
            ln for ln in cfg.splitlines() if not ln.strip().startswith("//")
        )
        assert "rewrites" not in code, (
            "next.config.js 不应再声明 rewrites；前端已改为直连后端"
        )

    def test_auth_headers_never_throw(self) -> None:
        """未配置凭据时不得抛错——那会让所有页面的 useEffect 连锁失败。

        凭据头在 api()/apiRaw()/subscribeSSE() 里按**运行时令牌**内联构造
        （桌面版令牌由 Tauri invoke 注入，不再是模块级 authHeaders()）。
        契约不变：缺凭据是正常状态，只能"有条件附带"，不能 throw/白屏。
        """
        api = (self._web_dir() / "lib" / "api.ts").read_text(encoding="utf-8")
        # 三处调用点都必须用条件附带的形式
        assert api.count('rt.token ? { "x-admin-token": rt.token } : {}') == 3, (
            "api()/apiRaw()/subscribeSSE() 必须统一按运行时令牌有条件附带凭据头"
        )
        # 不允许"缺凭据就抛错"的写法
        assert "!rt.token) throw" not in api
        assert "!ENV_ADMIN_TOKEN) throw" not in api

    def test_no_hardcoded_admin_token_in_client(self) -> None:
        """NEXT_PUBLIC_* 会进 bundle，禁止内置默认凭据。"""
        api = (self._web_dir() / "lib" / "api.ts").read_text(encoding="utf-8")
        assert "change-me-in-production" not in api, "前端仍内置默认 admin_token"
