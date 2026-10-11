"""MCP (Model Context Protocol) 工具服务端。

通过标准 JSON-RPC 协议将 ToolRegistry 暴露给外部 Agent/IDE
(Cursor, Claude Desktop, VS Code Copilot)。

支持两类端点:
- /mcp/*: MCP 原生 JSON-RPC (tools/list, tools/call, rpc)
- /v1/*:  OpenAI Function Calling 兼容格式
"""
from lingxi.mcp.models import JSONRPCError, JSONRPCRequest, JSONRPCResponse, MCPErrorCode
from lingxi.mcp.router import create_mcp_router, create_openai_tools_router
from lingxi.mcp.server import MCPToolServer, get_mcp_server, init_mcp_server

__all__ = [
    "MCPToolServer",
    "get_mcp_server",
    "init_mcp_server",
    "create_mcp_router",
    "create_openai_tools_router",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCError",
    "MCPErrorCode",
]
