"""MCP 服务端 — 基于 **官方 `mcp` SDK**（不再手搓 JSON-RPC 协议层）。

🔗 工程逻辑
    协议实现交给官方库（`mcp.server.mcpserver.MCPServer`，即 v1 的 FastMCP）：
    JSON-RPC 编解码、tools/resources/prompts 三原语的 schema 生成、
    `initialize` 握手、能力协商、通知、`streamable-http`/`sse` 传输
    全部由 SDK 负责 —— 这些是"协议细节多、版本演进快"的典型，手搓必然漂移。

    我们只保留**项目特有**的两件事：
    1. 工具来源是我们的 `ToolRegistry`（带分级权限门控）
    2. 资源/提示词接真实数据源（知识库 / 人格配置 / 实体图谱）

    这样既守住差异化能力，又不重复维护协议。

🤖 AI 避坑
    - `mcp` 2.x 把 `FastMCP` 改名为 `MCPServer`（`from mcp.server.mcpserver import
      MCPServer`）；不要照抄 1.x 的 `mcp.server.fastmcp` 导入路径。
    - 工具执行必须走我们的 `execute_tool`（含权限检查），不能把裸 handler
      交给 SDK —— 那会绕过 L0-L4 门控。
    - SDK 的 `streamable_http_app()` 返回 Starlette 应用：挂到 FastAPI 时用
      `app.mount(...)`，不要再包一层路由。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lingxi.core import get_logger

if TYPE_CHECKING:
    from lingxi.agent.tools import ToolRegistry

log = get_logger(__name__)

# 官方 SDK 不可用时降级（保持进程可启动；健康检查会看到 mcp 不可用）
try:
    from mcp.server.mcpserver import MCPServer

    HAS_OFFICIAL_MCP = True
except ImportError:  # pragma: no cover
    MCPServer = None  # type: ignore[assignment,misc]
    HAS_OFFICIAL_MCP = False
    log.warning(
        "official_mcp_sdk_missing",
        hint="pip install 'lingxi[mcp]'（官方 mcp SDK）—— MCP 端点将不可用",
    )


_mcp_server: MCPServer | None = None
_registry: ToolRegistry | None = None
_min_permission: int = 0


def build_mcp_server(
    registry: ToolRegistry,
    min_permission: int = 0,
    *,
    name: str = "zhixi",
) -> MCPServer | None:
    """按官方 SDK 构建 MCP 服务端；把 ToolRegistry 的工具逐个注册进去。

    Args:
        registry: 我们的工具注册表（含权限等级与 handler）
        min_permission: 调用方基线权限（低于工具所需等级会被拒）
        name: 服务端标识（MCP initialize 握手时返回给客户端）

    Returns:
        MCPServer 实例；官方 SDK 缺失时返回 None（不抛，保证进程可启动）
    """
    if not HAS_OFFICIAL_MCP:
        return None

    server = MCPServer(
        name=name,
        version="0.2.0",
        instructions=(
            "智犀（ZhiXi）数字员工引擎的工具集。"
            "调用需在 X-Miner-Permission 头声明权限等级（L0-L4）；"
            "高风险工具（shell 等）按命令内容动态定级。"
        ),
    )

    from lingxi.agent.tools import execute_tool

    # ── 工具：来源是我们的 registry，执行仍走带权限检查的 execute_tool ──
    for tool in registry.available:

        def _make_handler(tool_name: str) -> Any:
            async def _handler(**kwargs: Any) -> str:
                result = await execute_tool(
                    registry,
                    tool_name,
                    kwargs,
                    _min_permission,
                )
                if not result.get("success", False):
                    # 官方 SDK 会把返回文本包成 content block；失败前缀便于
                    # 客户端（与我们的 openai 兼容端点）一致地识别错误
                    return f"[error] {result.get('observation', '执行失败')}"
                return str(result.get("observation", ""))

            return _handler

        handler = _make_handler(tool.name)
        # SDK 从类型注解/签名推导 schema 会丢失我们精心写的
        # description 与 input_schema（模型靠它决定何时调用），
        # 因此显式传 name/description，并用 add_tool 注册已构造好的定义。
        try:
            server.add_tool(
                handler,
                name=tool.name,
                description=tool.description,
            )
        except TypeError:
            # 旧版签名兜底：装饰器形式
            server.tool(name=tool.name, description=tool.description)(handler)

    global _mcp_server, _registry, _min_permission
    _mcp_server = server
    _registry = registry
    _min_permission = min_permission
    log.info(
        "mcp_server_initialized",
        backend="official_sdk",
        tools=len(registry.available),
        min_permission=min_permission,
    )
    return server


def get_mcp_server() -> MCPServer | None:
    """获取全局 MCP server（官方 SDK 实例）。"""
    return _mcp_server


def set_min_permission(level: int) -> None:
    """覆盖调用方权限等级（HTTP 头传入时由路由设置，用完还原）。"""
    global _min_permission
    _min_permission = level


def get_min_permission() -> int:
    return _min_permission


def mcp_tools_list() -> list[dict[str, Any]]:
    """工具清单（MCP 风格），供 /mcp/tools/list 与 OpenAI 兼容端点复用。

    直接从我们的 registry 生成，保证与 SDK 注册的是同一份定义。
    """
    if _registry is None:
        return []
    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema,
        }
        for t in _registry.available
    ]


async def call_tool_via_registry(
    name: str, arguments: dict[str, Any], *, permission: int | None = None
) -> dict[str, Any]:
    """按 MCP content 格式调用工具（走权限检查）。

    这是给 HTTP 兼容端点用的入口：官方 SDK 的调用路径服务于 MCP 协议客户端
    （要经过会话/传输层），而 HTTP 直连端点需要一个轻量入口 —— 两者最终都
    汇到 `execute_tool`，权限语义完全一致。
    """
    from lingxi.agent.tools import execute_tool

    if _registry is None:
        return {
            "content": [{"type": "text", "text": "MCP 服务尚未初始化"}],
            "isError": True,
        }
    perm = _min_permission if permission is None else permission
    result = await execute_tool(_registry, name, arguments, perm)
    text = str(result.get("observation", ""))
    return {"content": [{"type": "text", "text": text}], "isError": not result.get("success", False)}


def streamable_http_app() -> Any:
    """官方 SDK 的 streamable-http ASGI 应用（挂到 FastAPI 用）。

    返回 None 表示 SDK 缺失或未初始化。
    """
    if _mcp_server is None:
        return None
    try:
        return _mcp_server.streamable_http_app()
    except Exception as e:  # noqa: BLE001
        log.warning("mcp_streamable_app_failed", error=str(e))
        return None


def mcp_status() -> dict[str, Any]:
    """MCP 子系统状态（健康检查/排障用）。"""
    return {
        "backend": "official_sdk" if HAS_OFFICIAL_MCP else "unavailable",
        "official_sdk_installed": HAS_OFFICIAL_MCP,
        "initialized": _mcp_server is not None,
        "tools": len(_registry.available) if _registry is not None else 0,
        "min_permission": _min_permission,
    }


__all__ = [
    "HAS_OFFICIAL_MCP",
    "MCPServer",
    "build_mcp_server",
    "call_tool_via_registry",
    "get_mcp_server",
    "get_min_permission",
    "mcp_status",
    "mcp_tools_list",
    "set_min_permission",
    "streamable_http_app",
]
