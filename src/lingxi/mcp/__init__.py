"""MCP (Model Context Protocol) 服务端 —— 基于**官方 `mcp` SDK**。

🔗 工程逻辑
    协议层（JSON-RPC 编解码、三原语 schema、握手、传输）全部交给官方 SDK
    （`mcp.server.mcpserver.MCPServer`，即 v1 的 FastMCP）。我们只提供
    项目特有的数据源与权限策略：

    原语:
    - **tools**     — 来源是 ToolRegistry（带 L0-L4 分级权限检查）
    - **resources** — 接真实数据源（知识库文档 / 人格配置 / 图谱统计）
    - **prompts**   — 可复用提示词模板

    端点:
    - `/mcp` 官方 streamable-http 传输（标准 MCP 客户端直连）
    - `/mcp/*` 轻量 HTTP 兼容端点（tools/list、tools/call、rpc）
    - `/v1/*`  OpenAI Function Calling 兼容格式
"""
from lingxi.mcp.primitives import (
    Prompt,
    Resource,
    ResourceRegistry,
    build_default_registry,
    get_resource_registry,
    set_resource_registry,
)
from lingxi.mcp.router import (
    create_mcp_router,
    create_openai_tools_router,
    mount_mcp_transport,
)
from lingxi.mcp.server import (
    HAS_OFFICIAL_MCP,
    build_mcp_server,
    call_tool_via_registry,
    get_mcp_server,
    mcp_status,
    mcp_tools_list,
    set_min_permission,
)

__all__ = [
    "HAS_OFFICIAL_MCP",
    "build_mcp_server",
    "call_tool_via_registry",
    "get_mcp_server",
    "mcp_status",
    "mcp_tools_list",
    "set_min_permission",
    "create_mcp_router",
    "create_openai_tools_router",
    "mount_mcp_transport",
    "Prompt",
    "Resource",
    "ResourceRegistry",
    "build_default_registry",
    "get_resource_registry",
    "set_resource_registry",
]
