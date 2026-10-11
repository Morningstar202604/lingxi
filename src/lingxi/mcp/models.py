"""MCP JSON-RPC 请求/响应模型。

🔗 工程逻辑
    MCP protocol spec:
    - request: {jsonrpc: "2.0", id: str, method: str, params: dict}
    - response: {jsonrpc: "2.0", id: str, result: dict | None, error: dict | None}

    Method types:
    - tools/list — 返回可用工具列表
    - tools/call — 执行一个工具，返回 observation

🤖 AI 避坑
    - MCP spec 不做 SSE for now (streaming 太复杂)，纯 POST
    - error 必须包含 code + message; 参考 LSP error codes
    - tool 列表返回格式严格遵循 MCP spec (name, description, inputSchema)
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class JSONRPCRequest(BaseModel):
    """MCP JSON-RPC 请求。"""

    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


class JSONRPCError(BaseModel):
    """MCP JSON-RPC 错误。"""

    code: int = 0
    message: str = ""
    data: dict[str, Any] | None = None


class JSONRPCResponse(BaseModel):
    """MCP JSON-RPC 响应。"""

    jsonrpc: str = "2.0"
    id: str | int | None = None
    result: dict[str, Any] | None = None
    error: JSONRPCError | None = None

    @classmethod
    def success(cls, id: str | int | None, result: dict[str, Any]) -> JSONRPCResponse:
        return cls(jsonrpc="2.0", id=id, result=result, error=None)

    @classmethod
    def failure(
        cls,
        id: str | int | None,
        code: int,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> JSONRPCResponse:
        return cls(
            jsonrpc="2.0",
            id=id,
            result=None,
            error=JSONRPCError(code=code, message=message, data=data),
        )


# ── 标准错误码 ──
class MCPErrorCode:
    """JSON-RPC / MCP 标准错误码。"""

    TOOL_NOT_FOUND = -32601       # 工具不存在
    INVALID_PARAMS = -32602       # 参数无效
    TOOL_EXECUTION_ERROR = -32603 # 执行失败
    PERMISSION_DENIED = -32000    # 权限拒绝
    INVALID_REQUEST = -32600      # 请求解析失败
    PARSE_ERROR = -32700          # JSON 解析失败


__all__ = [
    "JSONRPCRequest",
    "JSONRPCError",
    "JSONRPCResponse",
    "MCPErrorCode",
]
