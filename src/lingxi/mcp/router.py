"""MCP JSON-RPC 路由 + OpenAI 兼容工具路由。

🔗 工程逻辑
    两类客户端接入：

    1. MCP 原生客户端 (Cursor / Claude Desktop / VS Code Copilot)
       → POST /mcp/rpc  通用 JSON-RPC 端点
       → GET  /mcp/tools/list  直接返回工具列表
       → POST /mcp/tools/call  JSON-RPC style 调用工具

    2. OpenAI Function Calling 兼容客户端
       → GET  /v1/tools       工具列表 (OpenAI style)
       → POST /v1/tools/call  工具调用 (OpenAI style)

    安全: 读取 X-Miner-Permission header 作为权限上下文；
    未传则默认 L0 (只读)，遵循最小特权原则。

🤖 AI 避坑
    - /mcp/rpc 是通用端点，不要在里面硬编码方法 — 委托给 MCPToolServer
    - permission header 解析失败 → 默认降级到 L0，不是 403
    - OpenAI style 的 tool call 格式与 MCP 不同，注意参数映射
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from lingxi.core import get_logger
from lingxi.mcp.models import JSONRPCRequest, JSONRPCResponse, MCPErrorCode

log = get_logger(__name__)

# 与 SecurityConfig.admin_token 的默认值一致，用于识别"未改配置"
_DEFAULT_ADMIN_TOKEN = "change-me-in-production"


def _require_admin(x_admin_token: str | None) -> None:
    """管理凭据校验（与 dashboard_api.py / workflow/router.py 同一套策略）。

    🔗 安全
        此前这些端点**只看 X-Miner-Permission 头就放行** —— 那是一个权限等级，
        不是凭据，等于任何人都能声称自己是 L4 并直接调 shell。把接口挂上
        FastAPI 之前必须先补这一层，否则挂载本身就是在开后门。
    """
    from lingxi.config.settings import get_settings

    settings = get_settings()
    expected = settings.security.admin_token.get_secret_value()
    if expected == _DEFAULT_ADMIN_TOKEN and not settings.debug:
        raise HTTPException(
            status_code=503,
            detail="未配置管理凭据：请设置 LINGXI_SECURITY__ADMIN_TOKEN",
        )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def create_mcp_router() -> APIRouter:
    """工厂函数：创建 MCP JSON-RPC 路由。

    Returns:
        prefix="/mcp" 的 APIRouter
    """
    router = APIRouter(prefix="/mcp", tags=["mcp"])

    @router.get("/tools/list")
    async def mcp_list_tools(
        x_permission: int = Header(default=0, alias="X-Miner-Permission"),
        x_admin_token: str | None = Header(None),
    ) -> JSONRPCResponse:
        """返回可用工具列表 (MCP 风格)。"""
        _require_admin(x_admin_token)
        from lingxi.mcp.server import get_mcp_server

        server = get_mcp_server()
        if server is None:
            return JSONRPCResponse.failure(
                id="1",
                code=MCPErrorCode.TOOL_EXECUTION_ERROR,
                message="MCP 服务尚未初始化",
            )

        try:
            tools = await server.list_tools()
            return JSONRPCResponse.success(id="1", result=tools)
        except Exception as e:
            log.exception("mcp_list_tools_endpoint_error")
            return JSONRPCResponse.failure(
                id="1",
                code=MCPErrorCode.TOOL_EXECUTION_ERROR,
                message=f"列出工具失败: {e}",
            )

    @router.post("/tools/call")
    async def mcp_call_tool(
        body: JSONRPCRequest,
        x_permission: int = Header(default=0, alias="X-Miner-Permission"),
        x_admin_token: str | None = Header(None),
    ) -> JSONRPCResponse:
        """MCP 风格调用一个工具: {...method:"tools/call", params:{name, arguments}}。"""
        _require_admin(x_admin_token)
        from lingxi.mcp.server import get_mcp_server

        server = get_mcp_server()
        if server is None:
            return JSONRPCResponse.failure(
                id=body.id,
                code=MCPErrorCode.TOOL_EXECUTION_ERROR,
                message="MCP 服务尚未初始化",
            )

        # 临时覆盖权限 (header 传入或默认 L0)
        original_perm = server._min_permission
        server._min_permission = x_permission
        try:
            return await server.handle_request(body)
        finally:
            server._min_permission = original_perm

    @router.post("/rpc")
    async def mcp_rpc(
        body: JSONRPCRequest,
        x_permission: int = Header(default=0, alias="X-Miner-Permission"),
        x_admin_token: str | None = Header(None),
    ) -> JSONRPCResponse:
        """通用 JSON-RPC 端点: 根据 method 字段分发。"""
        _require_admin(x_admin_token)
        from lingxi.mcp.server import get_mcp_server

        server = get_mcp_server()
        if server is None:
            return JSONRPCResponse.failure(
                id=body.id,
                code=MCPErrorCode.TOOL_EXECUTION_ERROR,
                message="MCP 服务尚未初始化",
            )

        original_perm = server._min_permission
        server._min_permission = x_permission
        try:
            return await server.handle_request(body)
        finally:
            server._min_permission = original_perm

    return router


def create_openai_tools_router() -> APIRouter:
    """工厂函数: 创建 OpenAI 兼容工具路由。

    Returns:
        prefix="/v1" 的 APIRouter
    """
    router = APIRouter(prefix="/v1", tags=["openai-tools"])

    @router.get("/tools")
    async def openai_list_tools(
        x_permission: int = Header(default=0, alias="X-Miner-Permission"),
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        """OpenAI 风格工具列表。

        把 MCP tools/list 的结果包装成 OpenAI function calling 格式:
        {"object": "list", "data": [{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}]}
        """
        _require_admin(x_admin_token)
        from lingxi.mcp.server import get_mcp_server

        server = get_mcp_server()
        if server is None:
            raise HTTPException(status_code=503, detail="MCP 服务尚未初始化")

        tools = await server.list_tools()
        mcp_tools = tools.get("tools", [])

        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["inputSchema"],
                },
            }
            for t in mcp_tools
        ]

        return {"object": "list", "data": openai_tools}

    @router.post("/tools/call")
    async def openai_call_tool(
        body: dict[str, Any],
        x_permission: int = Header(default=0, alias="X-Miner-Permission"),
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        """OpenAI 风格工具调用。

        请求格式:
        {
            "name": "shell",
            "arguments": {"command": "echo hello"}
        }

        响应格式:
        {
            "content": "echo 输出内容",
            "isError": false
        }
        """
        _require_admin(x_admin_token)
        from lingxi.mcp.server import get_mcp_server

        server = get_mcp_server()
        if server is None:
            raise HTTPException(status_code=503, detail="MCP 服务尚未初始化")

        name = body.get("name")
        arguments = body.get("arguments", {})

        if not name:
            raise HTTPException(
                status_code=400,
                detail="缺少工具名称 (name)",
            )

        original_perm = server._min_permission
        server._min_permission = x_permission
        try:
            result = await server.call_tool(name=name, arguments=arguments)
        finally:
            server._min_permission = original_perm

        # 映射 MCP content 格式 → 简化文本
        content_blocks = result.get("content", [])
        text = content_blocks[0].get("text", "") if content_blocks else ""

        return {
            "content": text,
            "isError": result.get("isError", False),
        }

    return router


__all__ = [
    "create_mcp_router",
    "create_openai_tools_router",
]
