"""MCP (Model Context Protocol) 服务端 —— 暴露三原语。

通过标准 JSON-RPC 协议将 ToolRegistry 与只读上下文暴露给外部 Agent/IDE
(Cursor, Claude Desktop, VS Code Copilot)。

原语:
- **tools**     — list_tools / call_tool（动作）
- **resources** — list_resources / read_resource（知识库文档 / 人格配置 / 图谱统计）
- **prompts**   — list_prompts / get_prompt（可复用提示词模板）

端点:
- /mcp/*: MCP 原生 JSON-RPC (initialize / tools / resources / prompts / rpc)
- /v1/*:  OpenAI Function Calling 兼容格式
"""
from lingxi.mcp.models import JSONRPCError, JSONRPCRequest, JSONRPCResponse, MCPErrorCode
from lingxi.mcp.primitives import (
    Prompt,
    Resource,
    ResourceRegistry,
    build_default_registry,
    get_resource_registry,
    set_resource_registry,
)
from lingxi.mcp.router import create_mcp_router, create_openai_tools_router
from lingxi.mcp.server import MCPToolServer, get_mcp_server, init_mcp_server

__all__ = [
    "MCPToolServer",
    "get_mcp_server",
    "init_mcp_server",
    "create_mcp_router",
    "create_openai_tools_router",
    "Prompt",
    "Resource",
    "ResourceRegistry",
    "build_default_registry",
    "get_resource_registry",
    "set_resource_registry",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCError",
    "MCPErrorCode",
]
