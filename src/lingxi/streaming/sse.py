"""SSE 流式输出：Agent 推理链事件 → Server-Sent Events。

🔗 工程逻辑
    把 Agent 的推理链事件（token / tool_call / tool_result / error）
    翻译成用户友好的 event type，通过 sse_starlette 推给前端。

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

    两种运行模式（按优先级）:
    1. Agent.astream() — 真流式，逐 token + tool 事件（复用非流式同一套图）
    2. 降级: 阻塞 run() 后回放（Agent 未实现 astream 时）

🤖 AI 避坑
    - **不要**用 `agent._graph.astream_events()` 取 token：那条路依赖 LangGraph
      的 on_chat_model_stream 事件，而本项目 reason 节点直连 litellm.acompletion
      （不经过 LangChain Runnable），该事件永不触发 —— 实测只产出 3 个事件。
    - 最终 message 事件可能有多个 delta，前端要拼接显示
    - 流式过程中发生异常要推 agent_end(status=error) 后关闭连接
    - 注意 backpressure — 如果消费方太慢会堆积事件
    - 不要在 async generator 里用 threading.Lock
    - orjson.dumps 不能处理 BaseModel/SecretStr → 用 default=str 兜底
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import orjson
from sse_starlette.sse import EventSourceResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from lingxi.core import get_logger

log = get_logger(__name__)


def _sse_event(event_type: str, data: Any) -> dict[str, Any]:
    """构造 SSE 事件（event + data 字段）。"""
    return {
        "event": event_type,
        "data": orjson.dumps(data, default=str).decode("utf-8"),
    }


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
            # 优先走 Agent.astream()：它复用与非流式同一套图与状态机，
            # 且在 reason 节点里用 stream=True 逐 token 产出。
            #
            # 曾优先走 _stream_via_graph（直接读 agent._graph），那条路依赖
            # LangGraph 的 on_chat_model_stream 事件 —— 而本项目的 reason 节点
            # 是直接调 litellm.acompletion（不经过 LangChain Runnable），
            # 该事件永不触发，实测只产出 agent_start/message/agent_end 三个事件，
            # 所谓"逐 token 流式"是假的。
            if hasattr(agent, "astream"):
                async for sse_ev in _stream_native(
                    agent, raw_message, chat_id, user_id, channel,
                    system_prompt, messages,
                ):
                    yield sse_ev
            else:
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




async def _stream_native(
    agent: Any,
    raw_message: str,
    chat_id: str,
    user_id: str,
    channel: str,
    system_prompt: str,
    messages: list[dict[str, Any]] | None,
) -> AsyncIterator[dict[str, Any]]:
    """主路径：消费 Agent.astream() 的 token 级事件。

    Agent 侧已把图、状态机、护栏、权限全部复用了非流式实现，
    这里只做「领域事件 → SSE」的一层映射。

    🔗 契约：**零内容流不允许**
        如果整条流既没有 message 也没有 error，前端只会收到
        agent_start + agent_end，用户表现为"发完消息什么都没有"。
        历史上真发生过：上层节点漏处理某个事件类型 → 图照常走完 →
        流是空的。因此这里兜底补一个显式 error，而不是把问题留给用户发现。
    """
    collected: list[str] = []
    saw_error = False
    async for ev in agent.astream(
        raw_message=raw_message,
        chat_id=chat_id,
        user_id=user_id,
        channel=channel,
        system_prompt=system_prompt,
        messages=messages,
    ):
        kind = ev.get("type")
        if kind == "token":
            content = ev.get("content", "")
            if content:
                collected.append(content)
                yield _sse_event("message", {"delta": content, "final": False})
        elif kind == "tool_call":
            yield _sse_event("tool_call", {
                "tool": ev.get("tool_name", ""),
                "arguments": ev.get("arguments", {}),
            })
        elif kind == "tool_result":
            yield _sse_event("tool_result", {
                "tool": ev.get("tool_name", ""),
                "result": ev.get("observation", ""),
                "success": ev.get("success", False),
            })
        elif kind == "error":
            saw_error = True
            yield _sse_event("error", {"error": ev.get("error", "")})

    if collected:
        yield _sse_event("message", {"delta": "", "final": True})
    elif not saw_error:
        log.warning(
            "agent_stream_empty",
            chat_id=chat_id,
            detail="astream 未产出任何 message/error 事件",
        )
        yield _sse_event(
            "error",
            {"error": "模型没有返回任何内容，请稍后重试或转接人工。"},
        )


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
    """降级路径：Agent 没有 astream 时阻塞 run() 再回放。

    明确告知"非流式模式" —— 不假装逐 token，否则前端会把一次性输出
    当成流式渲染，用户观感与实际行为不符。
    """
    yield _sse_event("thinking", {"delta": "正在分析您的问题..."})

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

    for tr in result.get("tool_results", []):
        if isinstance(tr, dict):
            yield _sse_event("tool_result", {
                "tool": tr.get("tool_name", "unknown"),
                "result": tr.get("observation", ""),
            })

    final_reply = result.get("final_reply", "")
    if final_reply:
        yield _sse_event("message", {"delta": final_reply, "final": True})


__all__ = [
    "stream_agent_events",
    "_sse_event",
    "_stream_native",
    "_stream_fallback",
]
