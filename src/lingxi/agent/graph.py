"""Node 4.4 — LangGraph 状态机组装 + 运行入口。

🔗 工程逻辑
    LangGraph 的核心抽象 + ReAct 图（详见原文档字符串）。

🤖 AI 避坑
    - AsyncSqliteSaver 需要 aiosqlite；sync SqliteSaver 不支持 ainvoke
    - checkpointer_factory: 首次 run() 时 async with 初始化，避免在同步 __init__ 中 await
"""
from __future__ import annotations

from functools import cache, partial
from typing import Any

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
        # 默认使用 MemorySaver，保证多轮会话状态在同一进程内持久化
        effective_checkpointer = checkpointer if checkpointer is not None else MemorySaver()
        self._graph = self._build_graph(effective_checkpointer)

    def _build_graph(self, checkpointer: Any) -> Any:
        builder = StateGraph(AgentState)
        builder.add_node("reason", reason_node)
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
        if self._checkpointer_factory is not None and self._checkpointer_cm is None:
            self._checkpointer_cm = self._checkpointer_factory
            saver = await self._checkpointer_cm.__aenter__()
            self._graph = self._build_graph(saver)

        return await self._run_inner(**kwargs)

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
    ) -> dict[str, Any]:
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
            "max_steps": self._max_steps,
            "stage": "opening",
            "emotion": "calm",
            "pending_tool_call": None,
            "tool_results": [],
            "next_step": "reason",
            "final_reply": "",
            "escalate_reason": "",
            "confidence": 0.0,
        }

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
            }

        return {
            "final_reply": final_state.get("final_reply", ""),
            "tool_results": final_state.get("tool_results", []),
            "escalate_reason": final_state.get("escalate_reason", ""),
            "step_count": final_state.get("step_count", 0),
            "confidence": final_state.get("confidence", 0.0),
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
