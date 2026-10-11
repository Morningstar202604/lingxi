"""SSE 流式输出：Agent 推理链事件 → Server-Sent Events。

🔗 工程逻辑
    LangGraph 1.2+ 的 astream_events() 可以逐步产出每个 tool 调用、
    LLM token、chain 开始/结束等事件。
    我们把这些内部事件翻译为用户友好的 event type，
    通过 sse_starlette 推送到前端。

    SSE 事件格式 (前端消费):
    ---
    event: agent_start
    data: {"timestamp": 1729000000.123}

    event: knowledge_recall
    data: {"hits": [{"doc_id": "...", "score": 0.9, "text": "..."}]}

    event: thinking
    data: {"delta": "让我查一下订单状态..."}

    event: tool_call
    data: {"tool": "query_order", "args": {"order_id": "123"}}

    event: tool_result
    data: {"tool": "query_order", "result": "..."}

    event: message
    data: {"delta": "您的订单已经发货", "final": true}

    event: agent_end
    data: {"status": "completed"}
    ---

    三种运行模式（按优先级）:
    1. LangGraph astream_events (v2 API) — 全流式，逐 token + tool 事件
    2. 自定义 Agent.astream() — Agent 自行实现了流式接口
    3. 降级: 阻塞 run() + 模拟流式 (compatibility mode)

🤖 AI 避坑
    - astream_events 在 LangGraph 不同版本 API 不同，这里用 v1.x 兼容写法
    - 最终 message 事件可能有多个 delta，前端要拼接显示
    - 流式过程中发生异常要推 agent_error event 后关闭连接
    - 注意 backpressure — 如果消费方太慢会堆积事件
    - 不要在有 async generator 里用 threading.Lock
    - ZhiXiAgent._graph 是 LangGraph StateGraph 编译后的实例 (带下划线 = 内部)
      但流式场景必须用它调用 astream_events
    - orjson.dumps 不能处理 BaseModel/SecretStr → 用 default=str 兜底
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import orjson
from sse_starlette.sse import EventSourceResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from lingxi.agent.state import AgentState

from lingxi.core import get_logger

log = get_logger(__name__)

# LangGraph astream_events 事件类型前缀 (v2 wire format)
_LG_CHAT_STREAM = "on_chat_model_stream"
_LG_CHAT_START = "on_chat_model_start"
_LG_TOOL_START = "on_tool_start"
_LG_TOOL_END = "on_tool_end"
_LG_CHAIN_START = "on_chain_start"
_LG_CHAIN_END = "on_chain_end"


def _sse_event(event_type: str, data: Any) -> dict[str, Any]:
    """构造 SSE 事件（event + data 字段）。"""
    return {
        "event": event_type,
        "data": orjson.dumps(data, default=str).decode("utf-8"),
    }


def _extract_event_responses(event: dict[str, Any]) -> dict[str, Any]:
    """从 LangGraph on_chain_end 输出中提取 final_reply / escalate 等。

    LangGraph 链结束时的 output 字段格式不固定 —
    对于 StateGraph，它是 TypedDict 的 merge 视图 (AgentState 子集)。
    这里做容错解析。
    """
    output = event.get("data", {}).get("output", {})
    if not isinstance(output, dict):
        return {}
    return output


def stream_agent_events(
    agent: Any,
    raw_message: str,
    chat_id: str,
    user_id: str,
    channel: str,
    system_prompt: str = "",
    memory_block: str = "",
    lorebook_block: str = "",
    messages: list[dict[str, Any]] | None = None,
    *,
    knowledge_events: list[dict[str, Any]] | None = None,
) -> EventSourceResponse:
    """将 Agent 推理链事件转换为 SSE 流。

    用法::
        return stream_agent_events(
            self._agent,
            raw_message="我的订单到哪了",
            chat_id=event.chat_id,
            user_id=event.user_id,
            channel=event.channel,
            system_prompt=augmented_prompt,
            messages=history,
            knowledge_events=[{"doc_id": "doc1", "score": 0.95}],
        )

    Yields SSE events to client.

    Args:
        agent: ZhiXiAgent 实例（或兼容接口的 Agent）
        raw_message: 用户原始消息
        chat_id: 会话 ID
        user_id: 用户 ID
        channel: 渠道字符串
        system_prompt: 完整的系统提示（含 lorebook/knowledge 注入）
        memory_block: 记忆块
        lorebook_block: 世界观块
        messages: 完整对话历史 (含 system + 历史轮)
        knowledge_events: 知识库召回结果（可选）
    """
    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        yield _sse_event("agent_start", {"timestamp": time.time()})

        if knowledge_events:
            yield _sse_event("knowledge_recall", {"hits": knowledge_events})

        try:
            has_graph = hasattr(agent, "_graph") or hasattr(agent, "graph")

            if has_graph and hasattr(agent, "_run_inner"):
                # ── 模式 1: 直接拿到 LangGraph graph，构造 AgentState 并 astream_events ──
                graph = getattr(agent, "_graph", None) or getattr(agent, "graph", None)
                if graph is not None and hasattr(graph, "astream_events"):
                    async for sse_ev in _stream_via_graph(
                        agent, graph, raw_message, chat_id, user_id, channel,
                        system_prompt, memory_block, lorebook_block, messages,
                    ):
                        yield sse_ev
                else:
                    # graph 没有 astream_events（LangGraph 太旧）→ 降级模式 3
                    async for sse_ev in _stream_fallback(
                        agent, raw_message, chat_id, user_id, channel,
                        system_prompt, memory_block, lorebook_block, messages,
                    ):
                        yield sse_ev

            elif hasattr(agent, "astream"):
                # ── 模式 2: Agent 实现了 astream 方法 ──
                async for chunk in agent.astream(
                    raw_message=raw_message,
                    chat_id=chat_id,
                    user_id=user_id,
                    channel=channel,
                    system_prompt=system_prompt,
                    memory_block=memory_block,
                    lorebook_block=lorebook_block,
                    messages=messages or [],
                ):
                    chunk_type = chunk.get("type", "")
                    if chunk_type == "thinking":
                        yield _sse_event("thinking", {"delta": chunk.get("content", "")})
                    elif chunk_type == "tool_call":
                        yield _sse_event("tool_call", chunk)
                    elif chunk_type == "tool_result":
                        yield _sse_event("tool_result", chunk)
                    elif chunk_type == "message":
                        yield _sse_event("message", chunk)

            else:
                # ── 模式 3: 降级 run() ──
                async for sse_ev in _stream_fallback(
                    agent, raw_message, chat_id, user_id, channel,
                    system_prompt, memory_block, lorebook_block, messages,
                ):
                    yield sse_ev

            yield _sse_event("agent_end", {"status": "completed"})

        except Exception as e:
            log.exception("streaming_error", error=str(e))
            yield _sse_event("agent_end", {"status": "error", "error": str(e)})

    return EventSourceResponse(event_generator())


async def _stream_via_graph(
    agent: Any,
    graph: Any,
    raw_message: str,
    chat_id: str,
    user_id: str,
    channel: str,
    system_prompt: str,
    memory_block: str,
    lorebook_block: str,
    messages: list[dict[str, Any]] | None,
) -> AsyncIterator[dict[str, Any]]:
    """通过 LangGraph astream_events 实现全流式输出。"""

    # 复用 Agent._run_inner 中的 state 构造逻辑（避免 DRY）
    initial: AgentState = {
        "user_id": user_id,
        "chat_id": chat_id,
        "channel": channel,
        "raw_message": raw_message,
        "system_prompt": system_prompt,
        "memory_block": memory_block,
        "lorebook_block": lorebook_block,
        "messages": messages or [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_message},
        ],
        "step_count": 0,
        "max_steps": getattr(agent, "_max_steps", 3),
        "stage": "opening",
        "emotion": "calm",
        "pending_tool_call": None,
        "tool_results": [],
        "next_step": "reason",
        "final_reply": "",
        "escalate_reason": "",
        "confidence": 0.0,
    }

    thread_id = f"{channel}:{chat_id}"
    config = {"configurable": {"thread_id": thread_id}}

    # 追踪已经发出的 message 事件（避免重复）
    emitted_final = False

    try:
        async for event in graph.astream_events(initial, config=config, version="v2"):
            kind = event.get("event", "")
            name = event.get("name", "")
            data = event.get("data", {})

            if kind == _LG_CHAT_STREAM:
                # LLM 逐 token 输出
                chunk_info = data.get("chunk", "")
                # chunk 可能是 AIMessageChunk 对象或 str
                token = _extract_token(chunk_info)
                if token:
                    yield _sse_event("thinking", {"delta": token})

            elif kind == _LG_CHAT_START:
                yield _sse_event("thinking", {"delta": ""})

            elif kind == _LG_TOOL_START:
                tool_input = data.get("input", {})
                yield _sse_event("tool_call", {
                    "tool": name,
                    "args": tool_input,
                })

            elif kind == _LG_TOOL_END:
                output = data.get("output", "")
                # output 可能是 ToolMessage 或其它对象
                result_text = _extract_tool_output(output)
                yield _sse_event("tool_result", {
                    "tool": name,
                    "result": result_text,
                })

            elif kind == _LG_CHAIN_END:
                output = _extract_event_responses(event)
                final_reply = output.get("final_reply", "")
                if final_reply and not emitted_final:
                    emitted_final = True
                    yield _sse_event("message", {
                        "delta": final_reply,
                        "final": True,
                    })

            elif kind == _LG_CHAIN_START:
                pass  # 不需要向前端传递 chain 内部事件

            else:
                # 未知事件类型不向前传递（避免污染前端）
                log.debug("unknown_stream_event", kind=kind, name=name)

    except Exception as e:
        log.exception("astream_events_error", chat_id=chat_id, error=str(e))
        raise


async def _stream_fallback(
    agent: Any,
    raw_message: str,
    chat_id: str,
    user_id: str,
    channel: str,
    system_prompt: str,
    memory_block: str,
    lorebook_block: str,
    messages: list[dict[str, Any]] | None,
) -> AsyncIterator[dict[str, Any]]:
    """降级模式: 阻塞 run() 后模拟流式输出。"""
    yield _sse_event("thinking", {"delta": "(非流式模式) 正在分析您的问题..."})

    result: dict[str, Any] = await agent.run(
        raw_message=raw_message,
        chat_id=chat_id,
        user_id=user_id,
        channel=channel,
        system_prompt=system_prompt,
        memory_block=memory_block,
        lorebook_block=lorebook_block,
        messages=messages,
    )

    tool_results = result.get("tool_results", [])
    for tr in tool_results:
        if isinstance(tr, dict):
            yield _sse_event("tool_result", {
                "tool": tr.get("tool_name", "unknown"),
                "result": tr.get("observation", ""),
            })

    final_reply = result.get("final_reply", "")
    if final_reply:
        yield _sse_event("message", {
            "delta": final_reply,
            "final": True,
        })


def _extract_token(chunk_info: Any) -> str:
    """从 LangGraph chunk 对象中提取 token 字符串。"""
    if isinstance(chunk_info, str):
        return chunk_info
    # LangGraph v2 中 chunk 通常是 AIMessageChunk
    if hasattr(chunk_info, "content"):
        content = chunk_info.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # content blocks format
            text_parts = [c.get("text", "") for c in content if isinstance(c, dict)]
            return "".join(text_parts)
    return ""


def _extract_tool_output(output: Any) -> str:
    """从 LangGraph tool 输出中提取文本结果。"""
    if isinstance(output, str):
        return output
    if hasattr(output, "content"):
        return str(output.content)
    return str(output)


__all__ = [
    "stream_agent_events",
    "_sse_event",
    "_stream_via_graph",
    "_stream_fallback",
]
