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
from lingxi.agent.tools import _create_default_registry
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


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"x-admin-token": "change-me-in-production"}


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
        reg = _create_default_registry()

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
            registry=reg,
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
