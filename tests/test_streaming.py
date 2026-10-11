"""SSE 流式推理事件测试。"""
from __future__ import annotations

import asyncio
from typing import Any

from sse_starlette.sse import EventSourceResponse

from lingxi.streaming import _sse_event, stream_agent_events


def test_sse_event_format() -> None:
    """SSE 事件格式正确。"""
    ev = _sse_event("test_event", {"key": "value"})
    assert ev["event"] == "test_event"
    assert "key" in ev["data"]
    assert "value" in ev["data"]


def test_sse_event_with_complex_data() -> None:
    """复杂数据能正确序列化。"""
    ev = _sse_event("tool_call", {"tool": "query", "args": {"id": 123}})
    import json
    data = json.loads(ev["data"])
    assert data["tool"] == "query"


class _MockAgent:
    """模拟无 streaming 能力的 Agent。"""

    async def run(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        return {"response": "Hello, I'mAgent!"}


class _MockAgentWithStreaming:
    """模拟支持 astream 的 Agent。"""

    async def astream(self, raw_message: str, **kwargs: Any) -> Any:
        yield {"type": "thinking", "content": "正在分析..."}
        yield {"type": "message", "content": "回复内容", "delta": "回复内容"}


def test_stream_with_fallback_agent() -> None:
    """不支持 streaming 的 Agent 回退到阻塞 run。"""
    agent = _MockAgent()
    response = stream_agent_events(
        agent=agent,
        raw_message="你好",
        chat_id="test_123",
        user_id="user_1",
        channel="qq_private",
        system_prompt="test prompt",
    )
    assert isinstance(response, EventSourceResponse)


def test_stream_emits_events() -> None:
    """验证流包含标准事件序列。"""
    agent = _MockAgent()
    response = stream_agent_events(
        agent=agent,
        raw_message="test",
        chat_id="chat_1",
        user_id="u1",
        channel="test",
    )

    # 收集所有事件
    events: list[dict[str, Any]] = []
    async def collect() -> None:
        async def fake_send(msg: dict) -> None:
            events.append(msg)
        async def fake_receive() -> dict:
            return {"type": "http.request"}
        # 直接消费 response 内部的 body_iterator
        async for item in response.body_iterator:
            events.append(item)

    asyncio.run(collect())

    event_types = [e.get("event") for e in events]
    assert "agent_start" in event_types
    assert "agent_end" in event_types
    # 应该有 thinking 或 message
    assert "thinking" in event_types or "message" in event_types


def test_stream_with_knowledge_events() -> None:
    """知识库事件被包含在流中。"""
    agent = _MockAgent()
    knowledge_hits = [
        {"source": "doc1", "score": 0.92},
        {"source": "doc2", "score": 0.85},
    ]

    response = stream_agent_events(
        agent=agent,
        raw_message="查订单",
        chat_id="chat_1",
        user_id="u1",
        channel="test",
        knowledge_events=knowledge_hits,
    )

    events_data: list[dict[str, Any]] = []
    async def collect() -> None:
        async for item in response.body_iterator:
            events_data.append(item)


    asyncio.run(collect())

    event_types = [e.get("event") for e in events_data]
    assert "knowledge_recall" in event_types

    # 找到 knowledge_recall 事件验证 hits
    for ev in events_data:
        if ev.get("event") == "knowledge_recall":
            import json
            data = json.loads(ev["data"])
            assert len(data["hits"]) == 2


def test_stream_with_custom_messages() -> None:
    """messages 参数被正确传递。"""
    agent = _MockAgentWithStreaming()
    messages: list[dict[str, str]] = [{"role": "user", "content": "Hello"}]

    response = stream_agent_events(
        agent=agent,
        raw_message="test",
        chat_id="c",
        user_id="u",
        channel="ch",
        messages=messages,
    )

    events_data: list[dict[str, Any]] = []
    async def collect() -> None:
        async for item in response.body_iterator:
            events_data.append(item)


    asyncio.run(collect())

    event_types = [e.get("event") for e in events_data]
    assert "agent_start" in event_types
    assert "agent_end" in event_types


def test_stream_agent_error_recovery() -> None:
    """Agent 抛异常时应推 agent_end 而非挂起。"""

    class _BrokenAgent:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("LLM API timeout")

    agent = _BrokenAgent()
    response = stream_agent_events(
        agent=agent,
        raw_message="test",
        chat_id="c",
        user_id="u",
        channel="t",
    )

    events_data: list[dict[str, Any]] = []
    async def collect() -> None:
        async for item in response.body_iterator:
            events_data.append(item)

    asyncio.run(collect())

    event_types = [e.get("event") for e in events_data]
    assert "agent_start" in event_types
    # 有错误时也应当结束
    assert "agent_end" in event_types
