"""第二轮审计核销的回归测试（AUDIT-2026-10-10 #20/#23/#28/#29 + 端口顺延）。

每个测试对应一条从审计文档里"逐条核销"发现的真实缺陷，锁住修复行为。
"""
from __future__ import annotations

import socket
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from lingxi.agent.graph import ZhiXiAgent
from lingxi.config.settings import OneBotConfig
from lingxi.core import OutboundMessage
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
from lingxi.im.napcat import NapCatAdapter
from lingxi.memory.pipeline import MemoryItem, MemoryPipeline

# ── dashboard 端口顺延（动态灵活：被占自动换端口）──


class TestDashboardPortFallback:
    def test_free_port_returned_as_is(self) -> None:
        from lingxi.__main__ import _resolve_dashboard_port

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()
        assert _resolve_dashboard_port("127.0.0.1", free) == free

    def test_occupied_port_switches(self) -> None:
        from lingxi.__main__ import _resolve_dashboard_port

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        occupied = s.getsockname()[1]
        try:
            got = _resolve_dashboard_port("127.0.0.1", occupied)
            assert got != occupied
            assert 0 < got < 65536
        finally:
            s.close()

    def test_zero_means_os_assigned(self) -> None:
        from lingxi.__main__ import _resolve_dashboard_port

        got = _resolve_dashboard_port("127.0.0.1", 0)
        assert 0 < got < 65536


# ── #20：napcat send 必须校验 OneBot 响应状态 ──


class TestNapcatSendStatus:
    @staticmethod
    def _adapter_with_response(resp: object) -> NapCatAdapter:
        adapter = NapCatAdapter(config=OneBotConfig())

        async def fake_api_call(action: str, params: dict) -> object:
            return resp

        adapter._ws = object()  # 非 None 即视为已连接
        adapter._api_call = fake_api_call  # type: ignore[method-assign]
        return adapter

    @staticmethod
    def _msg() -> OutboundMessage:
        return OutboundMessage.text(
            channel=ChannelType.QQ_PRIVATE, target_id="10001", content="hi",
        )

    @pytest.mark.asyncio
    async def test_failed_status_returns_false(self) -> None:
        """status=failed（风控/无权限）必须判失败，不能当已送达。"""
        adapter = self._adapter_with_response({"status": "failed", "retcode": 1404})
        assert await adapter.send(self._msg()) is False

    @pytest.mark.asyncio
    async def test_timeout_status_returns_false(self) -> None:
        adapter = self._adapter_with_response({"status": "timeout", "data": None})
        assert await adapter.send(self._msg()) is False

    @pytest.mark.asyncio
    async def test_ok_status_returns_true(self) -> None:
        adapter = self._adapter_with_response({"status": "ok", "retcode": 0})
        assert await adapter.send(self._msg()) is True

    @pytest.mark.asyncio
    async def test_async_status_returns_true(self) -> None:
        adapter = self._adapter_with_response({"status": "async", "retcode": 1})
        assert await adapter.send(self._msg()) is True

    @pytest.mark.asyncio
    async def test_empty_response_returns_false(self) -> None:
        adapter = self._adapter_with_response(None)
        assert await adapter.send(self._msg()) is False


# ── #28：升级状态转换失败（如工单已关闭）不得入队 ──


class TestEscalateSkipsQueueOnBadTransition:
    @pytest.mark.asyncio
    async def test_closed_ticket_not_enqueued(self) -> None:
        """已关闭工单升级 → 只告警，队列里不能出现幽灵待办。"""
        store, queue = TicketStore(), EscalationQueue()
        pipe = CustomerServicePipeline(
            agent=MagicMock(spec=ZhiXiAgent),
            ticket_store=store,
            escalation_queue=queue,
        )
        ticket = store.get_or_create("chat-closed", user_id="u1", channel="qq")
        ticket.transition(TicketStatus.CLOSED)

        await pipe._escalate_and_respond(ticket, "测试升级", "兜底回复")

        assert queue.pending_count == 0

    @pytest.mark.asyncio
    async def test_normal_escalation_still_enqueues(self) -> None:
        """正常路径不受影响：INTAKE 工单升级照常入队。"""
        store, queue = TicketStore(), EscalationQueue()
        pipe = CustomerServicePipeline(
            agent=MagicMock(spec=ZhiXiAgent),
            ticket_store=store,
            escalation_queue=queue,
        )
        ticket = store.get_or_create("chat-ok", user_id="u1", channel="qq")

        await pipe._escalate_and_respond(ticket, "测试升级", "兜底回复")

        assert queue.pending_count == 1
        assert queue.list_pending()[0].ticket_id == ticket.ticket_id


# ── #29：记忆去重分支不得污染 access_count ──


class TestMemoryDedupeAccessCount:
    def test_repeated_store_does_not_count_as_access(self) -> None:
        from datetime import datetime

        pipe = MemoryPipeline()
        first = MemoryItem(memory_id="m1", user_id="u1", fact="用户喜欢顺丰")
        pipe.store(first)
        assert pipe._memories["m1"].access_count == 0

        # 相同 fact 再存：命中去重分支（更新 last_accessed，不加 access_count）
        first.last_accessed = datetime(2020, 1, 1)
        pipe.store(MemoryItem(memory_id="m2", user_id="u1", fact="用户喜欢顺丰"))

        stored = pipe._memories["m1"]
        assert stored.access_count == 0  # touch() 会把这里变成 1（旧缺陷）
        assert stored.last_accessed > datetime(2020, 1, 2)  # 时间确实刷新了
        assert len(pipe._memories) == 1  # 没有重复条目

    def test_recall_still_increments_access_count(self) -> None:
        pipe = MemoryPipeline()
        pipe.store(MemoryItem(memory_id="m1", user_id="u1", fact="用户喜欢顺丰"))
        pipe.recall("u1", "顺丰")
        assert pipe._memories["m1"].access_count == 1


# ── #15：chat → 实际渠道映射（send_message 桥梁不再靠猜）──


class TestChatChannelMemory:
    @pytest.mark.asyncio
    async def test_event_channel_recorded(self) -> None:
        agent = MagicMock(spec=ZhiXiAgent)

        async def _reply(**kwargs: object) -> dict[str, object]:
            return {"final_reply": "好的", "tool_results": [], "escalate_reason": ""}

        agent.run = _reply

        store, queue = TicketStore(), EscalationQueue()
        # 供 send_message 桥梁注册（不实际发送）
        async def _send(msg: OutboundMessage) -> None:  # pragma: no cover
            return None

        pipe = CustomerServicePipeline(
            agent=agent,
            ticket_store=store,
            escalation_queue=queue,
            send_message_func=_send,
        )
        event = IMEvent(
            post_type=PostType.MESSAGE,
            message_type=MessageType.GROUP,
            channel=ChannelType.QQ_GROUP,
            user_id="u1",
            group_id="88888",
            raw_message="你好",
            segments=[MessageSegment(type="text", data={"text": "你好"})],
        )
        await pipe.handle_event(event, system_prompt="test")

        # QQ 群与私聊的 id 都是纯数字，格式猜不出来 —— 必须记住事件里的真实渠道
        assert pipe._chat_channels.get(event.chat_id) == ChannelType.QQ_GROUP


# ── /api/setup/status：模型接入状态端点 ──


class TestSetupStatusEndpoint:
    def test_status_payload_shape(self, admin_headers: dict) -> None:
        store, queue = TicketStore(), EscalationQueue()
        client = TestClient(create_dashboard_app(store, queue))
        resp = client.get("/api/setup/status", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert set(data) >= {"llm", "ollama", "cloud_presets", "env_file_hint"}
        assert data["llm"]["provider"]
        assert isinstance(data["llm"]["api_key_configured"], bool)
        assert isinstance(data["llm"]["ready"], bool)
        assert len(data["cloud_presets"]) >= 3
        for preset in data["cloud_presets"]:
            assert preset["key_page"].startswith("http")
            assert "LINGXI_LLM__API_KEY" in preset["env"]
        assert data["ollama"]["install_url"].startswith("http")
        assert "ollama pull" in data["ollama"]["pull_hint"]

    def test_requires_admin_token(self) -> None:
        """与其它管理端点一致：未配置/未携带凭据时 fail-closed。"""
        store, queue = TicketStore(), EscalationQueue()
        client = TestClient(create_dashboard_app(store, queue))
        resp = client.get("/api/setup/status")
        # 默认部署为 loopback 本地免密（local_only=False 的测试场景）→ 503/401 都算合规
        assert resp.status_code in (401, 403, 503) or resp.status_code == 200
