"""Node 4.4 — LangGraph 状态机组装 + 运行入口。

🔗 工程逻辑
    LangGraph 的核心抽象 + ReAct 图（详见原文档字符串）。

🤖 AI 避坑
    - AsyncSqliteSaver 需要 aiosqlite；sync SqliteSaver 不支持 ainvoke
    - checkpointer_factory: 首次 run() 时 async with 初始化，避免在同步 __init__ 中 await
"""
from __future__ import annotations

from functools import cache, partial
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from lingxi.agent.nodes import (
    escalate_node,
    execute_tool_node,
    reason_node,
    respond_node,
)
from lingxi.agent.state import AgentState
from lingxi.agent.tools import ToolRegistry, _create_default_registry
from lingxi.config import get_settings
from lingxi.core import get_logger
from lingxi.persona.guardrails import PermissionLevel  # noqa: TC001

log = get_logger(__name__)


def _route_after_reason(state: AgentState) -> str:
    next_step = state.get("next_step", "")
    if next_step == "escalate":
        return "escalate"
    if next_step == "respond":
        return "respond"
    if next_step == "tool":
        return "execute_tool"
    if next_step == "reason":
        return "reason"
    return END


def _route_after_tool(state: AgentState) -> str:
    next_step = state.get("next_step", "reason")
    if next_step == "escalate":
        return "escalate"
    if next_step == "done":
        return END
    return "reason"


class ZhiXiAgent:
    """智犀 (ZhiXi) ReAct Agent — LangGraph 编译后的可调用实例。"""

    def __init__(
        self,
        registry: ToolRegistry,
        permission_level: PermissionLevel = 1,
        max_steps: int = 3,
        checkpointer: Any = None,
        checkpointer_factory: Any = None,
    ) -> None:
        self._registry = registry
        self._permission_level = permission_level
        self._max_steps = max_steps
        self._checkpointer_factory = checkpointer_factory
        self._checkpointer_cm: Any = None  # async context manager（lazy init）
        self._checkpointer_saver: Any = None
        # 默认使用 MemorySaver，保证多轮会话状态在同一进程内持久化
        effective_checkpointer = checkpointer if checkpointer is not None else MemorySaver()
        self._graph = self._build_graph(effective_checkpointer)
        # 流式专用图：reason 走 token 级输出。惰性构建，跑不到流式就不付这个成本。
        self._stream_graph: Any = None

    def _build_graph(self, checkpointer: Any, stream: bool = False) -> Any:
        """组装图。

        `stream=True` 时 reason 走流式节点。两者是**同一张图、同一套状态机**，
        只是 reason 内部的 LLM 调用方式不同 —— 不存在"流式另走一套逻辑"
        导致两边行为漂移的风险。
        """
        builder = StateGraph(AgentState)
        # registry 必须绑进 reason 节点：没有它就不会向模型下发 tools=，
        # 工具调用链路整条断开（见 docs/AUDIT-2026-10-10.md #1）
        builder.add_node(
            "reason",
            partial(reason_node, registry=self._registry, stream=stream),
        )
        # 用 partial 绑定当前实例的 registry + permission，避免模块级全局变量
        builder.add_node(
            "execute_tool",
            partial(
                execute_tool_node,
                registry=self._registry,
                current_permission=self._permission_level,
            ),
        )
        builder.add_node("escalate", escalate_node)
        builder.add_node("respond", respond_node)

        builder.add_edge(START, "reason")
        builder.add_conditional_edges("reason", _route_after_reason)
        builder.add_conditional_edges("execute_tool", _route_after_tool)
        builder.add_edge("respond", END)
        builder.add_edge("escalate", END)

        return builder.compile(checkpointer=checkpointer)

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        """运行一次 Agent 推理。

        首次调用时，若 _checkpointer_factory 已设置，则 async with 初始化真实的 checkpointer
        并重建 graph（确保 LangGraph ainvoke 使用持久化的 state）。
        """
        await self._ensure_checkpointer()
        return await self._run_inner(**kwargs)

    async def resume(
        self,
        channel: str,
        chat_id: str,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        """从挂起点恢复图（L2/L3 人工审批用）。

        `decision` 形如 ``{"approved": True, "approver": "alice"}``，
        它会成为节点里 `interrupt()` 的返回值。

        thread_id 必须与首次 `run()` 一致（`f"{channel}:{chat_id}"`），
        否则恢复不到挂起点 —— 这也是审批记录里存 chat_id 的原因。
        """
        from langgraph.types import Command

        await self._ensure_checkpointer()
        thread_id = f"{channel}:{chat_id}"
        config = {"configurable": {"thread_id": thread_id}}
        try:
            final_state = await self._graph.ainvoke(
                Command(resume=decision), config=config,
            )
        except Exception as e:
            log.exception("graph_resume_failed", chat_id=chat_id, error=str(e))
            return {
                "final_reply": "",
                "escalate_reason": f"审批恢复失败: {type(e).__name__}",
                "confidence": 0.0,
                "step_count": 0,
                "tool_results": [],
                "token_usage": {},
            }

        return {
            "final_reply": final_state.get("final_reply", ""),
            "tool_results": final_state.get("tool_results", []),
            "escalate_reason": final_state.get("escalate_reason", ""),
            "step_count": final_state.get("step_count", 0),
            "confidence": final_state.get("confidence", 0.0),
            "token_usage": final_state.get("token_usage", {}),
        }

    async def aclose(self) -> None:
        """释放 checkpointer 持有的资源（SQLite 连接等）。

        lazy __aenter__ 必须配对 __aexit__，否则 sqlite 连接泄漏到进程结束
        （见 docs/AUDIT-2026-10-10.md #18）。
        """
        cm, self._checkpointer_cm = self._checkpointer_cm, None
        self._checkpointer_saver = None
        if cm is not None:
            await cm.__aexit__(None, None, None)

    async def astream(
        self,
        raw_message: str,
        chat_id: str,
        user_id: str,
        channel: str,
        system_prompt: str = "",
        messages: list[dict[str, Any]] | None = None,
        permission_level: PermissionLevel | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """逐 token 流式推理。

        🔗 工程逻辑
            走 `graph.astream(stream_mode="custom")`：这是节点内
            `get_stream_writer()` 写入内容的**唯一出口**。
            （`astream_events(v2)` 只给 on_chain_stream 的 state 增量，
            在里面看不到 writer 写的事件 —— 实测过，那条路拿不到 token。）

            与 `run()` 的区别仅在传输层：**同一张图、同一套状态机**，
            只有 reason 内部的 LLM 调用方式不同，因此不存在"流式走另一套
            逻辑"导致两边行为分叉的风险。

        调用方**必须**在自己的协程里逐个消费并转发::

            writer = get_stream_writer()
            async for ev in agent.astream(...):
                yield ev

        产出的是领域事件（token / tool_call / tool_result / error），
        调用方负责转成 SSE 或其他传输格式。
        """
        await self._ensure_checkpointer()
        if self._stream_graph is None:
            self._stream_graph = self._build_graph(
                self._checkpointer_saver or MemorySaver(), stream=True,
            )

        initial = self._initial_state(
            raw_message=raw_message,
            chat_id=chat_id,
            user_id=user_id,
            channel=channel,
            system_prompt=system_prompt,
            messages=messages,
            permission_level=permission_level,
        )
        config = {"configurable": {"thread_id": f"{channel}:{chat_id}"}}

        try:
            # stream_mode="custom" 才是节点内 get_stream_writer() 的出口。
            # astream_events(v2) 只会给 on_chain_stream（state 增量），
            # writer 写的内容在里面**看不到** —— 实测过，别再退回那条路。
            async for chunk in self._stream_graph.astream(
                initial, config=config, stream_mode="custom",
            ):
                if isinstance(chunk, dict):
                    yield chunk
        except Exception as e:  # noqa: BLE001 — 流式边界要把异常转成事件
            log.exception("astream_failed", chat_id=chat_id, error=str(e))
            yield {"type": "error", "error": f"{type(e).__name__}: {e}"}

    async def _ensure_checkpointer(self) -> None:
        """惰性初始化 checkpointer 并重建主图。

        流式图复用同一个 saver（否则两套 checkpoint 会打架，同一会话状态分裂）。
        """
        if self._checkpointer_factory is None or self._checkpointer_cm is not None:
            return
        self._checkpointer_cm = self._checkpointer_factory
        self._checkpointer_saver = await self._checkpointer_cm.__aenter__()
        self._graph = self._build_graph(self._checkpointer_saver)
        self._stream_graph = None  # 用新 saver 重建流式图

    def _initial_state(
        self,
        raw_message: str,
        chat_id: str,
        user_id: str,
        channel: str,
        system_prompt: str = "",
        memory_block: str = "",
        lorebook_block: str = "",
        messages: list[dict[str, Any]] | None = None,
        permission_level: PermissionLevel | None = None,
    ) -> AgentState:
        """构造首轮 state（run 与 astream 共用，避免两边漂移）。"""
        return {
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
            "max_steps": self._max_steps,
            "stage": "opening",
            "emotion": "calm",
            "pending_tool_call": None,
            "pending_tool_calls": [],
            "tool_results": [],
            "tool_call_signatures": [],
            "token_usage": {},
            "next_step": "reason",
            "final_reply": "",
            "escalate_reason": "",
            "confidence": 0.0,
            "current_permission": (
                int(permission_level) if permission_level is not None
                else int(self._permission_level)
            ),
        }

    async def _run_inner(
        self,
        raw_message: str,
        chat_id: str,
        user_id: str,
        channel: str,
        system_prompt: str = "",
        memory_block: str = "",
        lorebook_block: str = "",
        messages: list[dict[str, Any]] | None = None,
        permission_level: PermissionLevel | None = None,
    ) -> dict[str, Any]:
        """跑一轮推理。

        permission_level 由调用方按 user/chat 解析后传入；为 None 时退回
        引擎级默认值（SecurityConfig.default_permission_level）。
        """
        initial = self._initial_state(
            raw_message=raw_message,
            chat_id=chat_id,
            user_id=user_id,
            channel=channel,
            system_prompt=system_prompt,
            memory_block=memory_block,
            lorebook_block=lorebook_block,
            messages=messages,
            permission_level=permission_level,
        )

        # 始终使用确定性 thread_id（基于 channel + chat_id），
        # 保证同一会话多轮对话状态在 checkpointer 中连续累积
        thread_id = f"{channel}:{chat_id}"
        config = {"configurable": {"thread_id": thread_id}}

        try:
            final_state = await self._graph.ainvoke(initial, config=config)
        except Exception as e:
            log.exception("graph_run_failed", chat_id=chat_id, error=str(e))
            return {
                "final_reply": "服务暂时不可用，请稍后再试。",
                "escalate_reason": str(e),
                "confidence": 0.0,
                "step_count": 0,
                "tool_results": [],
                "token_usage": {},
            }

        return {
            "final_reply": final_state.get("final_reply", ""),
            "tool_results": final_state.get("tool_results", []),
            "escalate_reason": final_state.get("escalate_reason", ""),
            "step_count": final_state.get("step_count", 0),
            "confidence": final_state.get("confidence", 0.0),
            "token_usage": final_state.get("token_usage", {}),
        }


def _create_default_checkpointer() -> Any:
    """创建默认 checkpointer（AsyncSqliteSaver，返回 async context manager）。

    langgraph 1.x 起 sqlite saver 位于配套包 langgraph-checkpoint-sqlite
    （pyproject 已锁定依赖）；未安装时优雅降级 MemorySaver（进程内有效）。
    """
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: PLC0415

        db_path = get_settings().memory.db_path
        log.info("sqlite_checkpointer_enabled", db_path=db_path)
        return AsyncSqliteSaver.from_conn_string(db_path)
    except ImportError:
        log.warning("AsyncSqliteSaver 不可用，回退 MemorySaver（会话状态不跨重启）")
        return MemorySaver()


@cache
def get_agent(
    registry: ToolRegistry | None = None,
    permission_level: PermissionLevel = 1,
    max_steps: int = 3,
    use_checkpoint: bool = True,
) -> ZhiXiAgent:
    """全局单例 Agent。"""
    if registry is None:
        blocked = get_settings().security.blocked_commands
        registry = _create_default_registry(blocked_commands=blocked)

    if not use_checkpoint:
        return ZhiXiAgent(registry=registry, permission_level=permission_level, max_steps=max_steps)

    factory = _create_default_checkpointer()
    # 若 factory 是 async context manager → lazy init（首次 run 时 __aenter__）
    if hasattr(factory, '__aenter__') or hasattr(factory, '__aexit__'):
        return ZhiXiAgent(registry=registry, permission_level=permission_level, max_steps=max_steps, checkpointer_factory=factory)
    return ZhiXiAgent(registry=registry, permission_level=permission_level, max_steps=max_steps, checkpointer=factory)


__all__ = ["ZhiXiAgent", "get_agent"]
