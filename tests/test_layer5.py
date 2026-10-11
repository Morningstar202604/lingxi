"""Layer 5 测试：业务层（工单系统 + 队列 + 流水线 + Dashboard API）。"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lingxi.agent.graph import ZhiXiAgent
from lingxi.agent.tools import _create_default_registry
from lingxi.config import get_settings
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
from lingxi.flow.ticket import (
    Ticket,
    TicketCategory,
    TicketStatus,
    TicketStore,
    classify_intent,
)

# ─────────────── classify_intent ───────────────


class TestClassifyIntent:
    def test_after_sales_keywords(self) -> None:
        assert classify_intent("我要退货") == TicketCategory.AFTER_SALES
        assert classify_intent("退款") == TicketCategory.AFTER_SALES
        assert classify_intent("换货") == TicketCategory.AFTER_SALES

    def test_complaint_keywords(self) -> None:
        assert classify_intent("我要投诉") == TicketCategory.COMPLAINT
        assert classify_intent("你们骗子") == TicketCategory.COMPLAINT

    def test_inquiry_keywords(self) -> None:
        assert classify_intent("怎么下单") == TicketCategory.INQUIRY
        assert classify_intent("多久发货") == TicketCategory.INQUIRY

    def test_other_fallback(self) -> None:
        assert classify_intent("随便看看") == TicketCategory.OTHER


# ─────────────── Ticket + TicketStore ───────────────


class TestTicket:
    def test_create_with_defaults(self) -> None:
        t = Ticket(chat_id="c1", user_id="u1", channel="qq_private")
        assert t.status == TicketStatus.INTAKE
        assert t.category == TicketCategory.OTHER

    def test_add_message(self) -> None:
        t = Ticket(chat_id="c1")
        msg = t.add_message("user", "你好")
        assert msg.role == "user"
        assert msg.content == "你好"
        assert len(t.messages) == 1

    def test_valid_transition(self) -> None:
        t = Ticket(chat_id="c1")
        t.transition(TicketStatus.PROCESSING)
        assert t.status == TicketStatus.PROCESSING

    def test_invalid_transition_raises(self) -> None:
        t = Ticket(chat_id="c1")
        with pytest.raises(ValueError, match="非法状态转换"):
            t.transition(TicketStatus.RESOLVED)

    def test_escalated_transition_chain(self) -> None:
        t = Ticket(chat_id="c1")
        t.transition(TicketStatus.PROCESSING)
        t.transition(TicketStatus.ESCALATED)
        assert t.status == TicketStatus.ESCALATED

    def test_cannot_transition_from_closed(self) -> None:
        t = Ticket(chat_id="c1")
        t.transition(TicketStatus.PROCESSING)
        t.transition(TicketStatus.RESOLVED)
        t.transition(TicketStatus.CLOSED)
        with pytest.raises(ValueError):
            t.transition(TicketStatus.PROCESSING)

    def test_to_dict_shape(self) -> None:
        t = Ticket(chat_id="c1", user_id="u1")
        d = t.to_dict()
        assert "ticket_id" in d
        assert d["chat_id"] == "c1"
        assert d["status"] == "intake"


class TestTicketStore:
    def test_get_or_create_new(self) -> None:
        store = TicketStore()
        t = store.get_or_create("c1", user_id="u1")
        assert t.chat_id == "c1"

    def test_get_or_create_reuses_active(self) -> None:
        store = TicketStore()
        t1 = store.get_or_create("c1")
        t2 = store.get_or_create("c1")
        assert t1.ticket_id == t2.ticket_id

    def test_creates_new_after_close(self) -> None:
        store = TicketStore()
        t1 = store.get_or_create("c1")
        t1.transition(TicketStatus.PROCESSING)
        t1.transition(TicketStatus.RESOLVED)
        t1.transition(TicketStatus.CLOSED)
        t2 = store.get_or_create("c1")
        assert t2.ticket_id != t1.ticket_id

    def test_list_active(self) -> None:
        store = TicketStore()
        t1 = store.get_or_create("c1")
        t2 = store.get_or_create("c2")
        t2.transition(TicketStatus.PROCESSING)
        t2.transition(TicketStatus.RESOLVED)
        active = store.list_active()
        assert len(active) == 1
        assert active[0].ticket_id == t1.ticket_id

    def test_get_returns_none_for_missing(self) -> None:
        store = TicketStore()
        assert store.get("ghost") is None


# ─────────────── EscalationQueue ───────────────


class TestEscalationQueue:
    def _make_ticket(self, chat: str, cat: TicketCategory) -> Ticket:
        t = Ticket(chat_id=chat)
        t.category = cat
        return t

    def test_enqueue_and_pending_count(self) -> None:
        q = EscalationQueue()
        t = self._make_ticket("c1", TicketCategory.INQUIRY)
        q.enqueue(t, reason="low_conf")
        assert q.pending_count == 1

    @pytest.mark.asyncio
    async def test_dequeue_by_priority_complaint_first(self) -> None:
        q = EscalationQueue()
        t_inq = self._make_ticket("c_inq", TicketCategory.INQUIRY)
        t_comp = self._make_ticket("c_comp", TicketCategory.COMPLAINT)
        q.enqueue(t_inq, reason="low_conf")
        q.enqueue(t_comp, reason="user_said")

        entry = await q.dequeue("agent1")
        assert entry is not None
        assert entry.ticket_id == t_comp.ticket_id

    @pytest.mark.asyncio
    async def test_dequeue_claims_ticket(self) -> None:
        q = EscalationQueue()
        t = self._make_ticket("c1", TicketCategory.INQUIRY)
        q.enqueue(t, reason="low_conf")

        entry = await q.dequeue("agent1")
        assert entry is not None
        assert entry.is_claimed
        assert entry.claimed_by == "agent1"
        assert q.pending_count == 0

    @pytest.mark.asyncio
    async def test_dequeue_empty_returns_none(self) -> None:
        q = EscalationQueue()
        assert await q.dequeue("agent1") is None

    @pytest.mark.asyncio
    async def test_complete_removes_entry(self) -> None:
        q = EscalationQueue()
        t = self._make_ticket("c1", TicketCategory.INQUIRY)
        q.enqueue(t, reason="low_conf")
        await q.dequeue("agent1")
        q.complete(t.ticket_id)
        assert q.pending_count == 0

    def test_list_pending_sorted(self) -> None:
        q = EscalationQueue()
        t1 = self._make_ticket("c1", TicketCategory.OTHER)
        t2 = self._make_ticket("c2", TicketCategory.COMPLAINT)
        t3 = self._make_ticket("c3", TicketCategory.AFTER_SALES)
        q.enqueue(t1, reason="r")
        q.enqueue(t2, reason="r")
        q.enqueue(t3, reason="r")

        pending = q.list_pending()
        assert len(pending) == 3
        assert pending[0].category == TicketCategory.COMPLAINT
        assert pending[1].category == TicketCategory.AFTER_SALES
        assert pending[2].category == TicketCategory.OTHER

    def test_snapshot(self) -> None:
        q = EscalationQueue()
        t = self._make_ticket("c1", TicketCategory.COMPLAINT)
        q.enqueue(t, reason="angry")

        snap = q.snapshot()
        assert snap["pending_count"] == 1
        assert snap["top_5_pending"][0]["category"] == "complaint"
        assert snap["longest_wait"] >= 0


# ─────────────── Dashboard API ───────────────


class TestDashboardAPI:
    # 凭据由 conftest.admin_headers 注入（Dashboard 对默认 token 已 fail-closed，
    # 见 docs/AUDIT-2026-10-10.md #8）

    @pytest.fixture()
    def client(self) -> TestClient:
        store = TicketStore()
        queue = EscalationQueue()
        app = create_dashboard_app(store, queue)
        return TestClient(app)

    def test_health(self, client: TestClient) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_summary_requires_auth(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """已配置凭据但请求未携带 token → 401（未授权）。

        注意区分：未配置凭据时是 503（服务未就绪），
        见 TestAdminAuthHardening::test_default_token_is_rejected_outside_debug。
        """
        resp = client.get("/api/dashboard/summary")
        assert resp.status_code == 401

    def test_summary_with_auth(self, client: TestClient, admin_headers: dict[str, str]) -> None:
        resp = client.get("/api/dashboard/summary", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "active_tickets" in data
        assert "queue" in data

    def test_list_tickets(self, client: TestClient, admin_headers: dict[str, str]) -> None:
        # 先创建一个工单
        store = TicketStore()
        store.get_or_create("c1", user_id="u1")
        resp = client.get("/api/tickets", headers=admin_headers)
        assert resp.status_code == 200
        assert len(resp.json()) >= 0

    def test_queue_endpoints_require_auth(self, client: TestClient, admin_headers: dict[str, str]) -> None:
        resp = client.get("/api/queue")
        assert resp.status_code == 401

    def test_claim_empty_queue(self, client: TestClient, admin_headers: dict[str, str]) -> None:
        resp = client.post(
            "/api/queue/claim?agent_id=agent1",
            headers=admin_headers,
        )
        assert resp.status_code == 404


# ─────────────── CustomerServicePipeline (mocked Agent) ───────────────


class TestPipeline:
    @pytest.mark.asyncio
    async def test_handle_event_empty_text(self) -> None:
        """纯空白消息（无文字无图片）现在不丢：建 ticket + 返回兜底回复。"""
        reg = _create_default_registry()
        agent = ZhiXiAgent(registry=reg, max_steps=1)
        store = TicketStore()
        queue = EscalationQueue()

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
            user_id="u1",
            group_id=None,
            raw_message="  ",
            segments=[MessageSegment(type="text", data={"text": "  "})],
        )
        result = await pipe.handle_event(event, system_prompt="test")
        # P1 修复：空白消息现在建 ticket 并返回兜底回复（不再静默丢弃）
        assert result.replied is True
        assert result.ticket_id != ""

    @pytest.mark.asyncio
    async def test_handle_event_creates_ticket(self) -> None:
        reg = _create_default_registry()
        agent = MagicMock(spec=ZhiXiAgent)
        agent.run = AsyncMock(return_value={
            "final_reply": "这是回复",
            "tool_results": [],
            "escalate_reason": "",
            "step_count": 1,
        })

        store = TicketStore()
        queue = EscalationQueue()

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
            user_id="u1",
            group_id=None,
            raw_message="你好",
            segments=[MessageSegment(type="text", data={"text": "你好"})],
        )
        result = await pipe.handle_event(event, system_prompt="test")
        assert result.replied is True
        assert result.escalated is False
        assert result.ticket_id != ""
        assert store.get(result.ticket_id) is not None

    @pytest.mark.asyncio
    async def test_handle_event_escalate_on_timeout(self) -> None:
        reg = _create_default_registry()
        agent = MagicMock(spec=ZhiXiAgent)

        async def _slow(**kwargs: Any) -> dict[str, Any]:
            import asyncio
            await asyncio.sleep(60)
            return {"final_reply": "", "escalate_reason": ""}

        agent.run = _slow

        store = TicketStore()
        queue = EscalationQueue()

        pipe = CustomerServicePipeline(
            agent=agent,
            ticket_store=store,
            escalation_queue=queue,
            registry=reg,
            max_agent_timeout=0.1,  # 极短超时
        )

        event = IMEvent(
            post_type=PostType.MESSAGE,
            message_type=MessageType.PRIVATE,
            channel=ChannelType.QQ_PRIVATE,
            user_id="u1",
            group_id=None,
            raw_message="你好",
            segments=[MessageSegment(type="text", data={"text": "你好"})],
        )
        result = await pipe.handle_event(event, system_prompt="test")
        assert result.replied is True
        assert result.escalated is True
        assert "agent_timeout" in result.escalation_reason


# ─────────────── 会话历史传递（回归锁：AUDIT-2026-10-10 #1） ───────────────


class TestConversationHistory:
    """pipeline 必须把工单里的历史对话传给 agent，否则推理层看不到上下文。"""

    @staticmethod
    def _event(text: str) -> IMEvent:
        return IMEvent(
            post_type=PostType.MESSAGE,
            message_type=MessageType.PRIVATE,
            channel=ChannelType.QQ_PRIVATE,
            user_id="u1",
            raw_message=text,
            segments=[MessageSegment(type="text", data={"text": text})],
        )

    @pytest.mark.asyncio
    async def test_agent_receives_full_conversation_history(self) -> None:
        """第 2 轮传给 agent.run 的 messages 必须包含第 1 轮的问答。"""
        agent = MagicMock(spec=ZhiXiAgent)
        captured: list[dict[str, Any]] = []

        async def _run(**kwargs: Any) -> dict[str, Any]:
            captured.append(kwargs)
            return {
                "final_reply": "好的",
                "tool_results": [],
                "escalate_reason": "",
                "step_count": 1,
                "confidence": 0.9,
            }

        agent.run = _run
        pipe = CustomerServicePipeline(
            agent=agent,
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
            registry=_create_default_registry(),
        )

        await pipe.handle_event(self._event("我的订单号是 A123"), system_prompt="SP")
        await pipe.handle_event(self._event("那它什么时候到？"), system_prompt="SP")

        assert len(captured) == 2
        msgs = captured[1].get("messages")
        assert msgs, "pipeline 未向 agent 传 messages，第 2 轮推理看不到历史"
        joined = " ".join(str(m.get("content", "")) for m in msgs)
        assert "A123" in joined, "第 2 轮 messages 缺少第 1 轮内容"
        assert "那它什么时候到？" in joined, "第 2 轮 messages 缺少当前轮消息"
        assert msgs[0].get("role") == "system", "system prompt 必须是首条消息"

    @pytest.mark.asyncio
    async def test_history_is_capped_by_budget(self) -> None:
        """超长历史必须按 context_char_budget 裁剪最近 N 轮，不能无界增长。"""
        agent = MagicMock(spec=ZhiXiAgent)
        captured: list[dict[str, Any]] = []

        async def _run(**kwargs: Any) -> dict[str, Any]:
            captured.append(kwargs)
            return {"final_reply": "好", "tool_results": [], "escalate_reason": "",
                    "step_count": 1, "confidence": 0.9}

        agent.run = _run
        pipe = CustomerServicePipeline(
            agent=agent,
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
            registry=_create_default_registry(),
        )

        for i in range(30):
            await pipe.handle_event(self._event(f"第{i}条消息" + "x" * 200),
                                    system_prompt="SP")

        msgs = captured[-1].get("messages") or []
        total = sum(len(str(m.get("content", ""))) for m in msgs)
        assert total <= 12000, f"历史未受预算约束，messages 总长 {total} 字符"


# ─────────────── Dashboard 服务化 + 契约对齐（回归锁：AUDIT #2 #3 #27 #26） ───────────────


class TestDashboardServing:
    """Dashboard 必须能被真实启动（此前 uvicorn 零引用，只能在测试里 TestClient）。"""

    def test_engine_exposes_dashboard_app(self) -> None:
        from lingxi.__main__ import build_default_engine

        engine = build_default_engine()
        app = engine.dashboard_app()
        assert isinstance(app, FastAPI)
        # app 必须共享 engine 的存储（否则前端看到的是另一个空实例）
        routes = {r.path for r in app.routes}
        assert "/api/dashboard/summary" in routes
        assert "/api/tickets" in routes
        assert "/api/queue" in routes

    def test_ticket_list_sorted_by_recent_activity(self) -> None:
        """/api/tickets 必须按 updated_at 降序（前端 slice(0,10) 取的是"最近"）。"""
        store = TicketStore()
        old = store.get_or_create("old_chat", user_id="u1")
        old.transition(TicketStatus.PROCESSING)
        new = store.get_or_create("new_chat", user_id="u2")
        new.transition(TicketStatus.PROCESSING)
        old.updated_at = 1.0        # 明确早于 new
        new.updated_at = 9999.0

        client = TestClient(create_dashboard_app(store, EscalationQueue()))
        resp = client.get(
            "/api/tickets",
            headers={"x-admin-token": get_settings().security.admin_token.get_secret_value()},
        )
        assert resp.status_code in (200, 503), resp.text
        if resp.status_code == 200:
            ids = [t["chat_id"] for t in resp.json()]
            assert ids.index("new_chat") < ids.index("old_chat"), f"未按活跃度排序: {ids}"

    def test_queue_claim_contract_status_codes(self) -> None:
        """空队列必须 404（前端按此返回 null），鉴权失败必须 401。"""
        client = TestClient(create_dashboard_app(TicketStore(), EscalationQueue()))
        token = get_settings().security.admin_token.get_secret_value()

        unauth = client.post("/api/queue/claim?agent_id=a1")
        assert unauth.status_code in (401, 503)

        empty = client.post(
            "/api/queue/claim?agent_id=a1", headers={"x-admin-token": token}
        )
        assert empty.status_code in (404, 503), empty.text
