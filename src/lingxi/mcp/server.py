"""MCP 工具服务端 —— 暴露 MCP 三原语。

🔗 工程逻辑
    MCP 定义三个原语，本服务端全部实现：
      1. **tools**   —— list_tools / call_tool：把 ToolRegistry 暴露为动作
      2. **resources** —— list_resources / read_resource：知识库文档、人格配置、图谱统计
      3. **prompts** —— list_prompts / get_prompt：可复用的人格提示词模板

    另有 initialize / ping 两个握手方法（MCP 客户端连上后的第一发）。

    权限门控：tools 走 min_permission；resources/prompts 是**只读上下文**，
    不含副作用，因此不设权限门，但仍要求 MCP 层凭据（见 router）。

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
    - resources/prompts 未注册时必须返回**错误**，返回空列表会被调用方
      误读成"确实没有"
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lingxi.core import get_logger
from lingxi.mcp.models import JSONRPCRequest, JSONRPCResponse, MCPErrorCode
from lingxi.mcp.primitives import get_resource_registry

if TYPE_CHECKING:
    from lingxi.agent.tools import ToolRegistry

log = get_logger(__name__)

# MCP 协议版本 / 服务器标识。客户端 initialize 握手时声明这两个字段，
# 版本不匹配时客户端会自行降级，因此这里跟着 LangChain MCP 的实现走。
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SERVER_VERSION = "1.0.0"


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

    # ── MCP 三原语：resources / prompts ──────────────────────────
    #
    # 数据源由 ResourceRegistry 注入；未注册时返回结构化错误而不是空列表 ——
    # 空列表会被调用方误读成「服务器确实没有这个资源」。

    async def list_resources(self) -> dict[str, Any]:
        reg = get_resource_registry()
        if reg is None:
            raise RuntimeError("资源注册表未初始化")
        return {"resources": reg.list_resources()}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        reg = get_resource_registry()
        if reg is None:
            raise RuntimeError("资源注册表未初始化")
        return await reg.read_resource(uri)

    async def list_prompts(self) -> dict[str, Any]:
        reg = get_resource_registry()
        if reg is None:
            raise RuntimeError("提示词注册表未初始化")
        return {"prompts": reg.list_prompts()}

    async def get_prompt(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        reg = get_resource_registry()
        if reg is None:
            raise RuntimeError("提示词注册表未初始化")
        prompt = reg.get_prompt(name)
        if prompt is None:
            raise KeyError(name)
        return {
            "description": prompt.description,
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": prompt.render(args)},
                },
            ],
        }

    async def handle_request(self, request: JSONRPCRequest) -> JSONRPCResponse:
        """处理 JSON-RPC request 并返回 response。"""
        method = request.method

        # 容空 id：MCP client 可能不传 id
        req_id: str | int | None = request.id

        # ── initialize：MCP 握手，客户端连上后第一发必是它 ──
        if method == "initialize":
            return JSONRPCResponse.success(id=req_id, result={
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": "lingxi", "version": MCP_SERVER_VERSION},
            })

        if method == "ping":
            return JSONRPCResponse.success(id=req_id, result={})

        if method == "resources/list":
            try:
                return JSONRPCResponse.success(
                    id=req_id, result=await self.list_resources(),
                )
            except RuntimeError as e:
                return JSONRPCResponse.failure(
                    id=req_id, code=MCPErrorCode.INVALID_REQUEST, message=str(e),
                )

        if method == "resources/read":
            uri = (request.params or {}).get("uri", "")
            if not isinstance(uri, str) or not uri:
                return JSONRPCResponse.failure(
                    id=req_id, code=MCPErrorCode.INVALID_PARAMS,
                    message="缺少资源 URI (params.uri)",
                )
            try:
                return JSONRPCResponse.success(
                    id=req_id, result=await self.read_resource(uri),
                )
            except KeyError:
                return JSONRPCResponse.failure(
                    id=req_id, code=MCPErrorCode.TOOL_NOT_FOUND,
                    message=f"资源不存在: {uri}", data={"uri": uri},
                )
            except Exception as e:  # noqa: BLE001 — 统一转结构化错误
                log.warning("mcp_read_resource_error", uri=uri, error=str(e))
                return JSONRPCResponse.failure(
                    id=req_id, code=MCPErrorCode.TOOL_EXECUTION_ERROR,
                    message=f"读取资源失败: {e}", data={"uri": uri},
                )

        if method == "prompts/list":
            try:
                return JSONRPCResponse.success(
                    id=req_id, result=await self.list_prompts(),
                )
            except RuntimeError as e:
                return JSONRPCResponse.failure(
                    id=req_id, code=MCPErrorCode.INVALID_REQUEST, message=str(e),
                )

        if method == "prompts/get":
            params = request.params or {}
            name = params.get("name")
            args = params.get("arguments", {})
            if not isinstance(name, str) or not name:
                return JSONRPCResponse.failure(
                    id=req_id, code=MCPErrorCode.INVALID_PARAMS,
                    message="缺少提示词名称 (params.name)",
                )
            if not isinstance(args, dict):
                return JSONRPCResponse.failure(
                    id=req_id, code=MCPErrorCode.INVALID_PARAMS,
                    message="params.arguments 必须是对象",
                )
            try:
                return JSONRPCResponse.success(
                    id=req_id, result=await self.get_prompt(name, args),
                )
            except KeyError:
                return JSONRPCResponse.failure(
                    id=req_id, code=MCPErrorCode.TOOL_NOT_FOUND,
                    message=f"提示词不存在: {name}", data={"name": name},
                )
            except RuntimeError as e:
                return JSONRPCResponse.failure(
                    id=req_id, code=MCPErrorCode.INVALID_REQUEST, message=str(e),
                )

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
