"""MCP 工具服务端。

🔗 工程逻辑
    MCP server 的核心职责:
    1. list_tools — 把内部 ToolRegistry 暴露为 MCP tools 列表
    2. call_tool — 执行单个 tool，标准化输入输出
    3. 权限门控 — 默认 L0，可设 min_permission

    工具列表 MCP schema:
    [{
        "name": "shell",
        "description": "...",
        "inputSchema": {"type": "object", "properties": {...}, "required": [...]}
    }]

🤖 AI 避坑
    - MCP tool 和直接使用 ToolRegistry 执行是两套权限模型，不要混淆
    - 如果 permission check 失败，返回 MCP error 不是 exception
    - MCP client (cursor, claude) 可能不传 id，要容错
    - 返回的 content 字段是 MCP 标准; 我们内部叫 observation — 需要映射
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lingxi.core import get_logger
from lingxi.mcp.models import JSONRPCRequest, JSONRPCResponse, MCPErrorCode

if TYPE_CHECKING:
    from lingxi.agent.tools import ToolRegistry

log = get_logger(__name__)


class MCPToolServer:
    """MCP 工具服务端 — 将 ToolRegistry 暴露为 MCP 协议。"""

    def __init__(self, registry: ToolRegistry, min_permission: int = 0) -> None:
        self._registry = registry
        self._min_permission = min_permission

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def list_tools(self) -> dict[str, Any]:
        """返回 MCP-compatible tools list result。

        Returns:
            {"tools": [{"name": ..., "description": ..., "inputSchema": {...}}, ...]}
        """
        return {"tools": self._registry.mcp_tools_list()}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """执行工具并返回 MCP-compatible result。

        Returns:
            MCP result dict with content field:
            {
                "content": [{"type": "text", "text": "..."}],
                "isError": False
            }
        """
        from lingxi.agent.tools import execute_tool

        result = await execute_tool(
            registry=self._registry,
            tool_name=name,
            arguments=arguments,
            current_permission=self._min_permission,
            timeout=30.0,
        )

        success = result.get("success", False)
        observation = result.get("observation", "")

        # MCP content format: list of content blocks
        content = [{"type": "text", "text": observation}]
        return {
            "content": content,
            "isError": not success,
        }

    async def handle_request(self, request: JSONRPCRequest) -> JSONRPCResponse:
        """处理 JSON-RPC request 并返回 response。"""
        method = request.method

        # 容空 id：MCP client 可能不传 id
        req_id: str | int | None = request.id

        if method == "tools/list":
            try:
                tools = await self.list_tools()
                return JSONRPCResponse.success(id=req_id, result=tools)
            except Exception as e:
                log.exception("mcp_list_tools_error")
                return JSONRPCResponse.failure(
                    id=req_id,
                    code=MCPErrorCode.TOOL_EXECUTION_ERROR,
                    message=f"列出工具失败: {e}",
                )

        if method == "tools/call":
            params = request.params or {}
            name = params.get("name")
            arguments = params.get("arguments", {})

            if not name:
                return JSONRPCResponse.failure(
                    id=req_id,
                    code=MCPErrorCode.INVALID_PARAMS,
                    message="缺少工具名称 (params.name)",
                    data={"params": params},
                )

            if not isinstance(arguments, dict):
                return JSONRPCResponse.failure(
                    id=req_id,
                    code=MCPErrorCode.INVALID_PARAMS,
                    message="params.arguments 必须是对象",
                    data={"arguments": arguments},
                )

            try:
                result = await self.call_tool(name=name, arguments=arguments)

                # 错误映射：工具不存在 → -32601
                content_blocks = result.get("content", [])
                first_text = content_blocks[0].get("text", "") if content_blocks else ""

                if result.get("isError", False) and "不存在" in first_text:
                    return JSONRPCResponse.failure(
                        id=req_id,
                        code=MCPErrorCode.TOOL_NOT_FOUND,
                        message=first_text,
                        data={"tool": name},
                    )

                if result.get("isError", False) and "权限不足" in first_text:
                    return JSONRPCResponse.failure(
                        id=req_id,
                        code=MCPErrorCode.PERMISSION_DENIED,
                        message=first_text,
                        data={"tool": name},
                    )

                return JSONRPCResponse.success(id=req_id, result=result)

            except Exception as e:
                log.exception("mcp_call_tool_error", tool=name)
                return JSONRPCResponse.failure(
                    id=req_id,
                    code=MCPErrorCode.TOOL_EXECUTION_ERROR,
                    message=f"工具 '{name}' 执行出错: {e}",
                    data={"tool": name},
                )

        # 未知 method
        return JSONRPCResponse.failure(
            id=req_id,
            code=MCPErrorCode.TOOL_NOT_FOUND,
            message=f"未知方法: '{method}'",
            data={"method": method},
        )


# ── 全局单例 (由 pipeline/Engine 注册 registry 时一起初始化) ──

_mcp_server: MCPToolServer | None = None


def get_mcp_server() -> MCPToolServer | None:
    """获取全局 MCP server 单例。"""
    return _mcp_server


def init_mcp_server(
    registry: ToolRegistry,
    min_permission: int = 0,
) -> MCPToolServer:
    """初始化全局 MCP server 单例。

    Args:
        registry: 工具注册表
        min_permission: 最小权限级别 (默认 L0)

    Returns:
        MCPToolServer 实例
    """
    global _mcp_server
    _mcp_server = MCPToolServer(registry, min_permission)
    log.info("mcp_server_initialized", min_permission=min_permission)
    return _mcp_server


__all__ = [
    "MCPToolServer",
    "get_mcp_server",
    "init_mcp_server",
    "_mcp_server",
]
