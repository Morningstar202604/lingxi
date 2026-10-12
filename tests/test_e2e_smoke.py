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


# ── 前端代理契约（AUDIT-2026-10-10 #2） ──


class TestFrontendProxyContract:
    """前端 API 调用路径必须落在 next.config.js rewrite 规则内。"""

    @staticmethod
    def _web_dir() -> Path:
        return Path(__file__).resolve().parents[1] / "web"

    @staticmethod
    def _next_rewrites(web_dir: Path, env: dict[str, str]) -> list[dict]:
        """用 Node 真实执行 next.config.js 拿 rewrites 结果。

        不做静态文本解析：exec 会因 JS 注释含中文全角括号而 SyntaxError，
        而正则匹配抓不到 "拼两次 /api" 这类真实错误（本轮 E2E 实测踩过）。
        """
        import json as _json
        import shutil

        node = shutil.which("node")
        if node is None:
            pytest.skip("node 不可用，跳过 next.config 校验")

        # 注意：node -e 下 process.argv[2] 不是脚本路径，须用环境变量传入
        probe = (
            "const c = require(process.env.LX_CFG);\n"
            "Promise.resolve(c.rewrites()).then(r => "
            "console.log(JSON.stringify(r)));"
        )
        proc = subprocess.run(
            [node, "-e", probe],
            capture_output=True, text=True, timeout=60,
            env={
                **os.environ,
                **env,
                "LX_CFG": str(web_dir / "next.config.js"),
            },
        )
        assert proc.returncode == 0, f"next.config 执行失败: {proc.stderr[:300]}"
        return _json.loads(proc.stdout.strip().splitlines()[-1])

    def test_api_client_uses_proxy_prefix(self) -> None:
        api = (self._web_dir() / "lib" / "api.ts").read_text(encoding="utf-8")
        assert 'const API_BASE = "/api/proxy"' in api, (
            "api.ts 未固定走 /api/proxy 前缀"
        )

    def test_rewrite_maps_proxy_to_backend_root(self) -> None:
        """真实校验重写结果：/api/proxy/<p> 必须映射到后端根 /<p>。

        旧规则 destination 多拼了一层 /api，把 /api/proxy/api/health
        # 变成 /api/api/health → FastAPI 404（E2E 实测发现，字符串包含
        # 断言抓不到，故此处直接执行 next.config 的 rewrites）。
        """
        rewrites = self._next_rewrites(self._web_dir(), {})
        assert rewrites, "next.config 未定义 rewrites"

        dest = next(
            r["destination"] for r in rewrites
            if r["source"].startswith("/api/proxy")
        )
        backend_path = dest.replace(":path*", "api/health")
        assert not backend_path.endswith("/api/api/health"), (
            f"rewrite 把 /api 拼了两次: {backend_path}"
        )
        assert backend_path.endswith("/api/health"), backend_path

    def test_backend_address_configurable(self) -> None:
        """后端地址须可由 DASHBOARD_API_URL 覆盖（不改代码）。"""
        rewrites = self._next_rewrites(
            self._web_dir(), {"DASHBOARD_API_URL": "http://192.168.1.9:9000"}
        )
        assert rewrites
        assert any(
            "192.168.1.9:9000" in r["destination"] for r in rewrites
        ), f"DASHBOARD_API_URL 未被采纳: {rewrites}"

    def test_no_hardcoded_admin_token_in_client(self) -> None:
        """NEXT_PUBLIC_* 会进 bundle，禁止内置默认凭据。"""
        api = (self._web_dir() / "lib" / "api.ts").read_text(encoding="utf-8")
        assert "change-me-in-production" not in api, "前端仍内置默认 admin_token"
