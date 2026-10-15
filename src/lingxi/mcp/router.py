"""MCP 路由：官方 SDK transport + 轻量 HTTP 兼容端点。

🔗 工程逻辑
    **协议层交给官方 SDK**（`mcp.server.mcpserver.MCPServer`）：
    - `/mcp` → 官方 `streamable_http_app()`（MCP 标准传输，Cursor/Claude
      Desktop/VS Code 等客户端直接对接）
    - `/mcp/sse` → 官方 SSE 传输（旧客户端）

    **HTTP 兼容端点保留**（我们自己的客户端契约，已有测试与前端依赖）：
    - `GET  /mcp/tools/list`、`POST /mcp/tools/call`、`POST /mcp/rpc`
    - `GET  /v1/tools`、`POST /v1/tools/call`（OpenAI function-calling 风格）

    两条路径最终都汇到 `execute_tool`，权限检查语义完全一致。

🤖 AI 避坑
    - `/mcp/rpc` 是通用 JSON-RPC 入口，不要再手写方法分发表 ——
      按官方 method 命名（tools/list、tools/call、resources/list、
      prompts/list）转发到我们的注册表/资源注册表即可。
    - 权限头解析失败一律降级 L0（最小特权），不要 403 也不要放行。
    - 官方 transport 用 app.mount() 挂载；它自带路由，不要再套 APIRouter。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from lingxi.core import get_logger
from lingxi.core.admin_auth import require_admin

log = get_logger(__name__)


def _require_admin(x_admin_token: str | None) -> None:
    """管理凭据校验（委托 core.admin_auth —— 全项目唯一实现）。

    🔗 安全
        这些端点此前只看 X-Miner-Permission 头就放行 —— 那是权限等级，
        不是凭据，等于任何人都能声称 L4 直接调 shell。挂载前必须有这一层。
    """
    require_admin(x_admin_token)


def _resolve_permission(x_permission: int | None) -> int:
    """解析权限头（失败/缺省 → L0 最小特权）。"""
    if x_permission is None:
        return 0
    try:
        return max(0, min(4, int(x_permission)))
    except (TypeError, ValueError):
        return 0


def _rpc_ok(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _rpc_err(req_id: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": err})


def create_mcp_router() -> APIRouter:
    """MCP HTTP 兼容路由（prefix=/mcp）。官方 transport 在 mount_mcp_transport 挂。"""
    router = APIRouter(prefix="/mcp", tags=["mcp"])

    @router.get("/tools/list")
    async def mcp_list_tools(
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        """工具清单（MCP tools/list 结果形状）。"""
        _require_admin(x_admin_token)
        from lingxi.mcp.server import mcp_tools_list

        return {"tools": mcp_tools_list()}

    @router.post("/tools/call")
    async def mcp_call_tool(
        body: dict[str, Any],
        x_permission: int | None = Header(default=None, alias="X-Miner-Permission"),
        x_admin_token: str | None = Header(None),
    ) -> JSONResponse:
        """MCP 风格调用：{name, arguments}。"""
        _require_admin(x_admin_token)
        from lingxi.mcp.server import call_tool_via_registry

        name = body.get("name")
        arguments = body.get("arguments") or {}
        if not name:
            return _rpc_err(body.get("id"), -32602, "缺少工具名称 (name)")
        if not isinstance(arguments, dict):
            return _rpc_err(body.get("id"), -32602, "arguments 必须是对象")
        result = await call_tool_via_registry(
            name, arguments, permission=_resolve_permission(x_permission)
        )
        return _rpc_ok(body.get("id"), result)

    @router.post("/rpc")
    async def mcp_rpc(
        body: dict[str, Any],
        x_permission: int | None = Header(default=None, alias="X-Miner-Permission"),
        x_admin_token: str | None = Header(None),
    ) -> JSONResponse:
        """通用 JSON-RPC 入口（方法名遵循 MCP 规范）。

        支持：initialize / tools/list / tools/call / resources/list /
        resources/read / prompts/list / prompts/get / ping
        """
        _require_admin(x_admin_token)
        from lingxi.mcp import primitives
        from lingxi.mcp.server import call_tool_via_registry, mcp_tools_list

        req_id = body.get("id")
        method = str(body.get("method") or "")
        params = body.get("params") or {}
        perm = _resolve_permission(x_permission)

        if method == "initialize":
            return _rpc_ok(req_id, {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "zhixi", "version": "0.2.0"},
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            })
        if method == "ping":
            return _rpc_ok(req_id, {})
        if method == "tools/list":
            return _rpc_ok(req_id, {"tools": mcp_tools_list()})
        if method == "tools/call":
            name = params.get("name")
            if not name:
                return _rpc_err(req_id, -32602, "缺少参数 params.name")
            result = await call_tool_via_registry(
                str(name), params.get("arguments") or {}, permission=perm
            )
            if result.get("isError"):
                text = result["content"][0].get("text", "") if result.get("content") else ""
                return _rpc_err(req_id, -32603, text, {"tool": name})
            return _rpc_ok(req_id, result)
        if method == "resources/list":
            reg = primitives.get_resource_registry()
            if reg is None:
                # 未装配 ≠ "确实没有资源"：返回空列表会让 MCP 客户端
                # 直接放弃该能力且不重试，必须用错误码表达"服务未就绪"
                return _rpc_err(req_id, -32601, "资源注册表未初始化")
            return _rpc_ok(req_id, {"resources": reg.list_resources()})
        if method == "resources/read":
            uri = str(params.get("uri") or "")
            if not uri:
                # 参数缺失（-32602）与资源不存在（-32601）是两种错，别混
                return _rpc_err(req_id, -32602, "缺少参数 params.uri")
            reg = primitives.get_resource_registry()
            if reg is None:
                return _rpc_err(req_id, -32601, "资源注册表未初始化")
            try:
                # read_resource 已返回 MCP 规范的 {"contents": [{uri, mimeType, text}]}
                content = await reg.read_resource(uri)
            except KeyError:
                # 不存在的 URI → 结构化错误，而不是 500
                return _rpc_err(req_id, -32601, f"资源不存在: {uri}", {"uri": uri})
            except Exception as e:  # noqa: BLE001
                return _rpc_err(req_id, -32603, f"读取资源失败: {e}", {"uri": uri})
            return _rpc_ok(req_id, content)
        if method == "prompts/list":
            reg = primitives.get_resource_registry()
            if reg is None:
                return _rpc_err(req_id, -32601, "资源注册表未初始化")
            return _rpc_ok(req_id, {"prompts": reg.list_prompts()})
        if method == "prompts/get":
            name = str(params.get("name") or "")
            reg = primitives.get_resource_registry()
            if reg is None:
                return _rpc_err(req_id, -32601, "资源注册表未初始化")
            prompt = reg.get_prompt(name)
            if prompt is None:
                return _rpc_err(req_id, -32601, f"提示词不存在: {name}", {"name": name})
            return _rpc_ok(req_id, prompt.to_get_result(params.get("arguments") or {}))

        return _rpc_err(req_id, -32601, f"未知方法: '{method}'", {"method": method})

    return router


def create_openai_tools_router() -> APIRouter:
    """OpenAI function-calling 兼容路由（prefix=/v1）。"""
    router = APIRouter(prefix="/v1", tags=["openai-tools"])

    @router.get("/tools")
    async def openai_list_tools(
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        """OpenAI 风格工具列表。"""
        _require_admin(x_admin_token)
        from lingxi.mcp.server import mcp_tools_list

        tools = mcp_tools_list()
        if not tools:
            # 未初始化与"初始化了但没工具"语义不同，前者 503 更准确
            from lingxi.mcp.server import get_mcp_server

            if get_mcp_server() is None:
                raise HTTPException(status_code=503, detail="MCP 服务尚未初始化")

        return {
            "object": "list",
            "data": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["inputSchema"],
                    },
                }
                for t in tools
            ],
        }

    @router.post("/tools/call")
    async def openai_call_tool(
        body: dict[str, Any],
        x_permission: int | None = Header(default=None, alias="X-Miner-Permission"),
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        """OpenAI 风格调用：{name, arguments} → {content, isError}。"""
        _require_admin(x_admin_token)
        from lingxi.mcp.server import call_tool_via_registry

        name = body.get("name")
        if not name:
            raise HTTPException(status_code=400, detail="缺少工具名称 (name)")
        result = await call_tool_via_registry(
            str(name),
            body.get("arguments") or {},
            permission=_resolve_permission(x_permission),
        )
        blocks = result.get("content", [])
        return {
            "content": blocks[0].get("text", "") if blocks else "",
            "isError": result.get("isError", False),
        }

    return router


def mount_mcp_transport(app: Any) -> bool:
    """把官方 SDK 的标准 MCP 传输挂到 FastAPI（/mcp）。

    挂载失败不阻断启动（返回 False，健康检查会显示 mcp 未初始化）。
    """
    from lingxi.mcp.server import streamable_http_app

    sub = streamable_http_app()
    if sub is None:
        return False
    try:
        # 官方 app 自带 /mcp 前缀处理；挂到根路径，避免双重前缀
        app.mount("/mcp", sub)
        log.info("mcp_official_transport_mounted", path="/mcp")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("mcp_transport_mount_failed", error=str(e))
        return False


__all__ = [
    "create_mcp_router",
    "create_openai_tools_router",
    "mount_mcp_transport",
]
