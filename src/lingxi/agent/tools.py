"""Node 4.2 — MCP 工具注册表 + 分级权限工具总线。

🔗 工程逻辑
    MCP（Model Context Protocol）定义了 Tools / Resources / Prompts 三个原语。
    这里聚焦 Tools：Agent 能调用的能力列表。

    工具处理的两种模式：
    - 纯本地执行（sync handler）：如 shell、calculator
    - 需要 pipeline 能力的异步工具：send_message（需要 IM adapter）、knowledge_search（需要 MemoryPipeline）

    异步工具通过 TOOL_BRIDGE 与 pipeline 通信，延迟绑定避免循环依赖。

🤖 AI 避坑
    - 不要让 LLM 自己判断权限 → 由 check_permission 硬检查
    - 权限检查必须在代码层执行，不依赖 prompt 软约束
    - tool 描述必须让 LLM 知道何时该用、何时不该用
"""
from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING, Any

from lingxi.core import get_logger
from lingxi.persona.guardrails import (
    check_permission,
    classify_shell_command,
)

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

# ── 工具桥梁：允许 pipeline 延迟绑定 IM adapter / memory backend ──
# 避免 tools.py 反向依赖 pipeline.py（循环导入）
TOOL_BRIDGE: dict[str, Any] = {
    "send_message": None,        # Callable[[str, str], Awaitable[bool]] | None
    "knowledge_search": None,    # Callable[[str, str], Awaitable[str]] | None (user_id, query) -> result_text
}


def register_tool_bridge(name: str, handler: Any) -> None:
    """注册工具桥梁（由 pipeline 调用）。

    Args:
        name: 工具名 ("send_message" 或 "knowledge_search")
        handler: 异步回调函数
    """
    TOOL_BRIDGE[name] = handler
    log.info("tool_bridge_registered", tool=name)


# ── 工具定义 ──

class ToolDefinition:
    """MCP-compatible 工具定义。"""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        required_permission: int = 0,
        handler: Callable[..., Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.required_permission = required_permission
        self.handler = handler

    def to_mcp_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class ToolRegistry:
    """中央工具注册表。"""

    def __init__(self, blocked_commands: list[str] | None = None) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._blocked_commands: list[str] = blocked_commands or []

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    @property
    def available(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def mcp_tools_list(self) -> list[dict[str, Any]]:
        return [t.to_mcp_dict() for t in self.available]


async def execute_tool(
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, Any],
    current_permission: int,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """执行一次工具调用（带权限检查 + 超时）。"""
    tool = registry.get(tool_name)
    if tool is None:
        return {"success": False, "observation": f"工具 '{tool_name}' 不存在"}

    # ── 权限检查 ──
    try:
        check_permission(tool_name, current_permission, tool.required_permission)
    except Exception as e:
        log.warning("permission_denied", tool=tool_name, error=str(e))
        return {"success": False, "observation": f"权限不足：{e}"}

    # ── Shell 命令特殊处理 ──
    if tool_name == "shell" and "command" in arguments:
        cmd_level = classify_shell_command(arguments["command"], registry._blocked_commands)
        # PERM_FORBIDDEN(4) 的命令无论用户权限如何一律拦截
        if cmd_level >= 4:
            return {"success": False, "observation": "命令被黑名单拦截，已拒绝执行。"}
        try:
            check_permission("shell", current_permission, cmd_level)
        except Exception:
            return {"success": False, "observation": f"Shell 命令权限被拒绝（级别 {cmd_level}）"}

    # ── 执行 handler ──
    try:
        if tool.handler is None:
            return {"success": False, "observation": f"工具 '{tool_name}' 没有注册 handler"}

        result = tool.handler(**arguments)

        # handler 是 coroutine → await；是普通值/ Future → 等它 resolve
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=timeout)
        elif asyncio.isfuture(result):
            result = await asyncio.wait_for(asyncio.shield(result), timeout=timeout)

        observation = str(result) if not isinstance(result, str) else result
        return {"success": True, "observation": observation}

    except TimeoutError:
        return {"success": False, "observation": f"工具 '{tool_name}' 执行超时（{timeout}s）"}
    except Exception as e:
        log.exception("tool_error", tool=tool_name)
        return {"success": False, "observation": f"工具 '{tool_name}' 执行出错：{e}"}


# ── 异步工具 handlers（通过 TOOL_BRIDGE 与 pipeline 通信）──


async def _send_message_handler(
    content: str,
    chat_id: str = "",
    *,
    current_chat_id: str = "",
) -> str:
    """通过 TOOL_BRIDGE 发送消息到 IM 渠道。

    安全：目标会话由 execute_tool_node 从 AgentState 注入（current_chat_id），
    模型既看不到该参数也无法覆盖 —— 对应 Semantic Kernel CVE-2026-25592
    的根治方式 "Removed AI access"（详见 docs/AUDIT-2026-10-10.md #5）。
    """
    target = current_chat_id or chat_id
    if not target:
        return "（无法确定目标会话，消息未发送）"
    bridge = TOOL_BRIDGE.get("send_message")
    if bridge is None:
        return f"（消息发送器尚未配置，消息未实际发送）\n目标: {target}\n内容: {content[:200]}"
    try:
        ok = await bridge(chat_id=target, content=content)
        return f"消息已发送到 {target}" if ok else f"发送到 {target} 失败"
    except Exception as e:
        return f"发送异常: {e}"


async def _knowledge_search_handler(
    query: str,
    *,
    current_user_id: str = "",
) -> str:
    """通过 TOOL_BRIDGE 查询当前用户的记忆。

    安全：user_id 由 execute_tool_node 从 AgentState 注入，模型不可指定，
    避免跨用户读取他人记忆（见 docs/AUDIT-2026-10-10.md #5）。
    """
    bridge = TOOL_BRIDGE.get("knowledge_search")
    if bridge is None:
        return "（知识库未配置，无法执行搜索）"
    try:
        return str(await bridge(user_id=current_user_id, query=query))
    except Exception as e:
        return f"知识库查询异常: {e}"


async def _shell_handler(command: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        limit=1024 * 100,
    )
    stdout, stderr = await proc.communicate()
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return f"[exit={proc.returncode}] {err or out}"
    return out


def _create_default_registry(blocked_commands: list[str] | None = None) -> ToolRegistry:
    registry = ToolRegistry(blocked_commands=blocked_commands)

    registry.register(ToolDefinition(
        name="shell",
        description="执行 Shell 命令。仅当用户明确要求且权限足够时使用。",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要执行的 shell 命令"}},
            "required": ["command"],
        },
        required_permission=1,
        handler=_shell_handler,
    ))

    registry.register(ToolDefinition(
        name="send_message",
        description=(
            "向**当前会话**发送文本消息。只提供 content；"
            "目标会话由系统自动确定，无需也无法指定他人会话。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "消息内容", "maxLength": 2000},
            },
            "required": ["content"],
        },
        required_permission=1,  # 写操作至少 LOW_RISK
        handler=_send_message_handler,
    ))

    registry.register(ToolDefinition(
        name="knowledge_search",
        description=(
            "在当前用户的记忆中搜索与 query 相关的条目。"
            "检索范围自动限定为当前会话用户。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词或自然语言问题"},
            },
            "required": ["query"],
        },
        required_permission=0,
        handler=_knowledge_search_handler,
    ))

    return registry


__all__ = [
    "ToolDefinition",
    "ToolRegistry",
    "execute_tool",
    "register_tool_bridge",
    "TOOL_BRIDGE",
]
