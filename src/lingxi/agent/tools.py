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
    PERM_FORBIDDEN,
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

    def to_openai_tool(self) -> dict[str, Any]:
        """OpenAI function-calling 规范的工具声明。

        input_schema 本身就是 JSON Schema，与 OpenAI 的 parameters 字段同构，
        直接复用即可（不重复维护第二份 schema）。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolRegistry:
    """中央工具注册表。"""

    def __init__(self, blocked_commands: list[str] | None = None) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._blocked_commands: list[str] = blocked_commands or []

    @property
    def blocked_commands(self) -> list[str]:
        """黑名单命令（只读；原先只能摸 `_blocked_commands` 私有属性）。"""
        return list(self._blocked_commands)

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    @property
    def available(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def mcp_tools_list(self) -> list[dict[str, Any]]:
        return [t.to_mcp_dict() for t in self.available]

    def openai_tools(self) -> list[dict[str, Any]]:
        """OpenAI function-calling 工具清单 — 传给 litellm 的 ``tools=`` 参数。

        这是工具调用链路接通的关键：模型只有在收到该清单时才可能返回
        tool_calls，否则只能输出散文（见 docs/AUDIT-2026-10-10.md #1）。
        """
        return [t.to_openai_tool() for t in self.available]


def resolve_required_level(
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, Any],
) -> int:
    """本次调用真正需要的权限等级。

    🔗 工程逻辑
        shell 的风险等级取决于**具体命令**（`ls` 是 L0，`rm -rf` 是 L3），
        不能只看工具声明上的 required_permission=1。原先这段判定只存在于
        `execute_tool` 内部，审批流拿不到 —— 抽出后调用方（execute_tool_node）
        与执行器共用同一份判定，避免"审批时算一个级别、执行时算另一个"。

        工具不存在时返回 PERM_FORBIDDEN：宁可判到红线，也不给未知工具开绿灯。
    """
    tool = registry.get(tool_name)
    if tool is None:
        return PERM_FORBIDDEN
    if tool_name == "shell":
        command = arguments.get("command", "")
        if isinstance(command, str) and command.strip():
            return classify_shell_command(command, registry.blocked_commands)
    return tool.required_permission


def needs_approval(required: int, current: int) -> bool:
    """该调用是否需要人工审批（BLUEPRINT 权限分级模型）。

    L2 需用户确认 / L3 需管理员审批 / L4 始终拒绝。
    L4 不进审批流 —— 红线操作不是"批一下就能做"。
    """
    return current < required < PERM_FORBIDDEN


def truncate_observation(text: str, max_chars: int) -> str:
    """截断过长的工具结果。

    🔗 工程逻辑
        实测：shell 返回 30 万字符时，observation 会**全量**进入 messages，
        下一轮请求的上下文被单个工具结果撑爆 —— 既超出模型上下文窗口，
        也放大了注入风险（工具输出里的文本会被当成上下文读）。

        头尾各留一段：头部保留"是什么命令/什么结果"，尾部保留实际结论
        （报错信息、退出码往往在末尾）。
    """
    if len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars - head - 40
    dropped = len(text) - head - tail
    return (
        f"{text[:head]}\n"
        f"…（中间 {dropped} 字符已省略，共 {len(text)} 字符）…\n"
        f"{text[-tail:]}"
    )


async def execute_tool(
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, Any],
    current_permission: int,
    timeout: float = 30.0,
    max_observation_chars: int | None = None,
) -> dict[str, Any]:
    """执行一次工具调用（带权限检查 + 超时）。

    权限判定统一走 `resolve_required_level`，与审批流共用同一套等级口径 —
    避免"审批时算一个级别、执行时算另一个"。
    """
    tool = registry.get(tool_name)
    if tool is None:
        return {"success": False, "observation": f"工具 '{tool_name}' 不存在"}

    required = resolve_required_level(registry, tool_name, arguments)
    # L4 红线：无论谁调用一律拒绝（BLUEPRINT「始终拒绝，仅代码层操作」）
    if required >= PERM_FORBIDDEN:
        detail = (
            "命令被黑名单拦截，已拒绝执行。"
            if tool_name == "shell"
            else f"工具 '{tool_name}' 属红线操作，已拒绝执行。"
        )
        return {"success": False, "observation": detail}

    try:
        check_permission(tool_name, current_permission, required)
    except Exception as e:
        log.warning("permission_denied", tool=tool_name, error=str(e))
        return {"success": False, "observation": f"权限不足：{e}"}

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
        # 超长结果截断：不截的话下一个 LLM 请求的上下文会被单个工具撑爆
        limit = max_observation_chars
        if limit is None:
            from lingxi.config import get_settings

            limit = get_settings().agent.tool_result_max_chars
        raw_len = len(observation)
        observation = truncate_observation(observation, limit)
        if raw_len > limit:
            log.info("tool_result_truncated", tool=tool_name, raw=raw_len, kept=limit)
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
    "needs_approval",
    "register_tool_bridge",
    "resolve_required_level",
    "TOOL_BRIDGE",
    "truncate_observation",
]
