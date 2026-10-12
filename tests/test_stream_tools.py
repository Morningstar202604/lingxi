"""流式工具调用（function calling over SSE）的行为锁。

🔗 工程逻辑
    网关把一次 tool_call 拆成多片：第 1 片带 id + name，后续片只带
    arguments 的一段（id/name 为 null）。不按 index 归并就会得到一堆
    半截 arguments —— `json.loads` 直接炸，流式会话里工具永远用不了。

    另外要验证：流式路径注入的是**同一套**受信任参数与解析逻辑
    （current_user_id 由代码注入，不接受模型指定），否则流式就成了
    绕过权限与隔离的后门。
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from lingxi.agent.graph import ZhiXiAgent
from lingxi.agent.llm import _ToolCallAccumulator, stream_llm
from lingxi.agent.tools import ToolDefinition, ToolRegistry

# ── 流式 chunk 替身 ─────────────────────────────────────────────────

class _Delta:
    def __init__(self, content: str | None = None, tool_calls: list[Any] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta: _Delta) -> None:
        self.delta = delta


class _Chunk:
    def __init__(self, delta: _Delta) -> None:
        self.choices = [_Choice(delta)]


def _tool_stream(tool_name: str, args: dict[str, Any], call_id: str = "call_1") -> list[_Chunk]:
    """构造一次被拆成 3 片的 tool_call 流。"""
    raw = json.dumps(args, ensure_ascii=False)
    half = len(raw) // 2
    return [
        _Chunk(_Delta("我查一下", None)),
        _Chunk(_Delta(None, [
            {"index": 0, "id": call_id, "function": {"name": tool_name, "arguments": ""}},
        ])),
        _Chunk(_Delta(None, [
            {"index": 0, "id": None, "function": {"name": None, "arguments": raw[:half]}},
        ])),
        _Chunk(_Delta(None, [
            {"index": 0, "id": None, "function": {"name": None, "arguments": raw[half:]}},
        ])),
    ]


# ── 归并器 ──────────────────────────────────────────────────────────

class TestToolCallAccumulator:
    def test_merges_split_arguments(self) -> None:
        acc = _ToolCallAccumulator()
        acc.feed([{"index": 0, "id": "c1", "function": {"name": "f", "arguments": ""}}])
        acc.feed([{"index": 0, "id": None, "function": {"name": None, "arguments": '{"a":'}}])
        acc.feed([{"index": 0, "id": None, "function": {"name": None, "arguments": '1}'}}])
        out = acc.drain()
        assert len(out) == 1
        assert out[0]["id"] == "c1"
        assert out[0]["tool_name"] == "f"
        assert out[0]["arguments"] == '{"a":1}'

    def test_multiple_calls_by_index(self) -> None:
        """两个并行工具调用必须各自成条，不能串成一条。"""
        acc = _ToolCallAccumulator()
        acc.feed([
            {"index": 0, "id": "a", "function": {"name": "f1", "arguments": '{"x":'}},
            {"index": 1, "id": "b", "function": {"name": "f2", "arguments": '{"y":'}},
        ])
        acc.feed([
            {"index": 0, "function": {"arguments": "1}"}},
            {"index": 1, "function": {"arguments": "2}"}},
        ])
        out = acc.drain()
        assert len(out) == 2
        assert [o["tool_name"] for o in out] == ["f1", "f2"]
        assert out[0]["arguments"] == '{"x":1}'
        assert out[1]["arguments"] == '{"y":2}'

    def test_drain_clears(self) -> None:
        acc = _ToolCallAccumulator()
        acc.feed([{"index": 0, "id": "a", "function": {"name": "f", "arguments": "{}"}}])
        assert len(acc.drain()) == 1
        assert acc.drain() == []

    def test_object_form_chunks(self) -> None:
        """litellm 归一化后是对象而非 dict，两种形态都要吃得下。"""
        acc = _ToolCallAccumulator()

        class _Fn:
            def __init__(self, name, arguments) -> None:
                self.name = name
                self.arguments = arguments

        class _TC:
            def __init__(self, index, id_, fn) -> None:
                self.index = index
                self.id = id_
                self.function = fn

        acc.feed([_TC(0, "z", _Fn("f", ""))])
        acc.feed([_TC(0, None, _Fn(None, "{}"))])
        out = acc.drain()
        assert out[0]["id"] == "z"
        assert out[0]["tool_name"] == "f"


# ── stream_llm ──────────────────────────────────────────────────────

class TestStreamLLMToolCalls:
    @pytest.mark.asyncio
    async def test_yields_tokens_then_tool_call(self) -> None:
        async def gen(**kw: Any) -> Any:
            for c in _tool_stream("knowledge_search", {"query": "订单"}):
                yield c

        events = []
        with patch("litellm.acompletion", new=AsyncMock(return_value=gen())):
            async for ev in stream_llm([{"role": "user", "content": "x"}]):
                events.append(ev)

        assert [e["type"] for e in events] == ["token", "tool_call"]
        assert events[0]["content"] == "我查一下"
        assert events[1]["tool_name"] == "knowledge_search"
        assert events[1]["arguments"] == {"query": "订单"}
        assert events[1]["id"] == "call_1"

    @pytest.mark.asyncio
    async def test_malformed_arguments_fall_back_to_empty(self) -> None:
        """半截 JSON 不能把整次推理炸掉。"""
        async def gen(**kw: Any) -> Any:
            yield _Chunk(_Delta(None, [
                {"index": 0, "id": "c", "function": {"name": "f", "arguments": "{broken"}},
            ]))

        events = []
        with patch("litellm.acompletion", new=AsyncMock(return_value=gen())):
            async for ev in stream_llm([{"role": "user", "content": "x"}]):
                events.append(ev)
        assert events[0]["arguments"] == {}

    @pytest.mark.asyncio
    async def test_text_only_stream_unchanged(self) -> None:
        """纯文本流的行为不能因为加了 tool_call 归并而改变。"""
        async def gen(**kw: Any) -> Any:
            for t in ["你", "好"]:
                yield _Chunk(_Delta(t))

        events = []
        with patch("litellm.acompletion", new=AsyncMock(return_value=gen())):
            async for ev in stream_llm([{"role": "user", "content": "x"}]):
                events.append(ev)
        assert [e["content"] for e in events] == ["你", "好"]


# ── 端到端：流式会话真的能调工具 ────────────────────────────────────

class TestStreamingToolExecution:
    @pytest.mark.asyncio
    async def test_stream_tool_call_reaches_execute(self) -> None:
        """流式 → tool → reason 全链路打通，且工具真的被执行。"""
        registry = ToolRegistry()
        seen: dict[str, Any] = {}

        async def recall(query: str = "", *, current_user_id: str = "") -> str:
            seen["query"] = query
            seen["user_id"] = current_user_id
            return "命中：订单 A123 已发货"

        registry.register(ToolDefinition(
            name="knowledge_search",
            description="搜索用户记忆",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            required_permission=0,
            handler=recall,
        ))
        agent = ZhiXiAgent(registry=registry, max_steps=4)

        calls: list[Any] = []

        async def stream_1(**kw: Any) -> Any:
            for c in _tool_stream("knowledge_search", {"query": "订单 A123"}, "call_7"):
                yield c

        async def finish(**kw: Any) -> Any:
            msg = AsyncMock()
            msg.content = "您的订单已发货"
            msg.tool_calls = None
            ch = AsyncMock()
            ch.message = msg
            r = AsyncMock()
            r.choices = [ch]
            return r

        with patch(
            "litellm.acompletion",
            new=AsyncMock(side_effect=[stream_1(), finish()]),
        ):
            async for ev in agent.astream("查订单", "cs", "us", "qq_private", "SP"):
                calls.append(ev)

        assert seen.get("query") == "订单 A123", "流式工具没有被执行"
        # 受信任参数由代码注入，模型无法指定他人
        assert seen.get("user_id") == "us"
        assert any(e["type"] == "tool_call" for e in calls)
        assert any(e["type"] == "tool_result" for e in calls)
