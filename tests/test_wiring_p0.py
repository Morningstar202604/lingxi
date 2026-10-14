"""P0 接线回归 — 锁住"代码存在但生产不可达"这类缺陷。

🔗 工程逻辑
    本文件锁 5 条曾经全部失守、且**门禁全绿也测不出来**的链路：
    1. Agent 工具调用：tools= 必须真的下发给模型（否则 tool_calls 永不触发）
    2. tool_calls 回填：assistant.tool_calls 与 tool 消息必须一一配对
    3. 生产进程挂载：build_default_engine().dashboard_app() 必须含
       stream / experiments / seats / mcp / graph 路由
    4. /metrics 必须鉴权（暴露端点名与状态码分布）
    5. 分级权限按 user/chat 解析，而非进程级常量

    前 4 条都是"单测自己显式传参所以测得到、生产入口没传所以测不到"的类型，
    这正是 docs/AUDIT-2026-10-10.md 的核心教训：门禁绿 ≠ 系统可用。

🤖 AI 避坑
    - 断言挂载要用「真的构造生产装配」，不要用测试自己拼的 app
    - mock LLM 时必须显式给 tool_calls=None，否则 MagicMock 的自动属性
      会被当成真 tool_calls
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from lingxi.agent.nodes import execute_tool_node, reason_node
from lingxi.agent.tools import _create_default_registry
from lingxi.flow.dashboard_api import create_dashboard_app
from lingxi.flow.queue import EscalationQueue
from lingxi.flow.ticket import TicketStore
from lingxi.persona.guardrails import resolve_permission_level

if TYPE_CHECKING:
    from lingxi.agent.state import AgentState


# ── 1. tools= 真的下发给模型 ────────────────────────────────────────

def _state(**over: Any) -> AgentState:
    base: dict[str, Any] = {
        "user_id": "u1",
        "chat_id": "c1",
        "channel": "qq_private",
        "raw_message": "订单到哪了",
        "system_prompt": "SP",
        "memory_block": "",
        "lorebook_block": "",
        "messages": [{"role": "system", "content": "SP"}, {"role": "user", "content": "订单到哪了"}],
        "step_count": 0,
        "max_steps": 3,
        "stage": "opening",
        "emotion": "calm",
        "pending_tool_call": None,
        "pending_tool_calls": [],
        "tool_results": [],
        "next_step": "reason",
        "final_reply": "",
        "escalate_reason": "",
        "confidence": 0.0,
        "current_permission": 1,
    }
    base.update(over)
    return base  # type: ignore[return-value]


def _llm_response(*, content: str = "", tool_calls: list[Any] | None = None) -> Any:
    msg = AsyncMock()
    msg.content = content
    msg.tool_calls = tool_calls
    choice = AsyncMock()
    choice.message = msg
    resp = AsyncMock()
    resp.choices = [choice]
    return resp


def _tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> Any:
    fn = AsyncMock()
    fn.name = name
    fn.arguments = json.dumps(args)
    tc = AsyncMock()
    tc.function = fn
    tc.id = call_id
    return tc


class TestToolCallingWired:
    """1) 工具清单真的进了请求体。"""

    @pytest.mark.asyncio
    async def test_reason_node_passes_tools_to_llm(self) -> None:
        """没有 tools= → 模型不可能返回 tool_calls，ReAct 整条链断掉。"""
        registry = _create_default_registry()
        mock_resp = _llm_response(content="你的订单已发货")

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)) as m:
            out = await reason_node(_state(), registry=registry)

        assert m.await_count == 1
        sent = m.await_args.kwargs
        assert sent.get("tool_choice") == "auto"
        names = [t["function"]["name"] for t in sent["tools"]]
        assert {"shell", "send_message", "knowledge_search"} <= set(names)
        # schema 必须是 OpenAI function 形态
        for t in sent["tools"]:
            assert t["type"] == "function"
            assert isinstance(t["function"]["parameters"], dict)
        assert out["next_step"] == "respond"

    @pytest.mark.asyncio
    async def test_tool_calling_switch_off_sends_no_tools(self) -> None:
        """开关关掉时不得下发 tools（兼容不支持 function calling 的私有端点）。"""
        from lingxi.config import get_settings

        settings = get_settings()
        old = settings.agent.tool_calling
        settings.agent.tool_calling = False
        try:
            registry = _create_default_registry()
            mock_resp = _llm_response(content="好的")
            with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)) as m:
                await reason_node(_state(), registry=registry)
            assert "tools" not in m.await_args.kwargs
        finally:
            settings.agent.tool_calling = old

    @pytest.mark.asyncio
    async def test_tool_call_routes_to_tool_node(self) -> None:
        """模型返回 tool_calls → next_step=tool，且 state 带上 id 与参数。"""
        registry = _create_default_registry()
        mock_resp = _llm_response(tool_calls=[_tool_call("knowledge_search", {"query": "订单"}, "call_x")])

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            out = await reason_node(_state(), registry=registry)

        assert out["next_step"] == "tool"
        assert out["pending_tool_call"]["tool_name"] == "knowledge_search"
        assert out["pending_tool_call"]["id"] == "call_x"
        # assistant 消息必须保留 tool_calls，否则下一轮配不上 tool 消息
        assistant = out["messages"][-1]
        assert assistant["tool_calls"][0]["id"] == "call_x"


# ── 2. tool 消息回填与权限 ──────────────────────────────────────────

class TestToolResultWiring:
    """2) 工具结果按 id 回填成配对的 tool 消息。"""

    @pytest.mark.asyncio
    async def test_tool_message_paired_with_tool_call_id(self) -> None:
        registry = _create_default_registry()
        state = _state(
            pending_tool_call={"tool_name": "knowledge_search", "arguments": {"query": "x"}, "id": "call_x"},
            pending_tool_calls=[{"tool_name": "knowledge_search", "arguments": {"query": "x"}, "id": "call_x"}],
            next_step="tool",
        )
        # 新 registry 默认无桥梁 → knowledge_search 返回"未配置"，无需 mock
        out = await execute_tool_node(state, registry, 1)

        tool_msg = out["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "call_x"
        assert out["pending_tool_calls"] == []

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_all_executed(self) -> None:
        """一轮多个 tool_calls 必须全部执行并逐个回填（漏一个就 400）。"""
        registry = _create_default_registry()
        calls = [
            {"tool_name": "knowledge_search", "arguments": {"query": "a"}, "id": "c1"},
            {"tool_name": "knowledge_search", "arguments": {"query": "b"}, "id": "c2"},
        ]
        state = _state(pending_tool_calls=calls, next_step="tool")
        out = await execute_tool_node(state, registry, 1)

        assert len(out["tool_results"]) == 2
        ids = [m.get("tool_call_id") for m in out["messages"][-2:]]
        assert ids == ["c1", "c2"]

    @pytest.mark.asyncio
    async def test_permission_denied_escalates(self) -> None:
        """权限不足 → escalate，不再让 LLM 反复重试到 max_steps。"""
        registry = _create_default_registry()
        state = _state(
            pending_tool_call={"tool_name": "shell", "arguments": {"command": "rm -rf /"}, "id": "c9"},
            pending_tool_calls=[{"tool_name": "shell", "arguments": {"command": "rm -rf /"}, "id": "c9"}],
            next_step="tool",
            current_permission=1,
        )
        out = await execute_tool_node(state, registry, 1)
        assert out["next_step"] == "escalate"
        assert out["escalate_reason"]


# ── 3. 生产装配真的挂上了全部路由 ──────────────────────────────────

class TestProductionWiring:
    """3) 用真实生产装配验证路由挂载（不是测试自拼的 app）。"""

    def test_engine_dashboard_app_mounts_all_routers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from lingxi.__main__ import build_default_engine

        monkeypatch.setenv("LINGXI_SECURITY__ADMIN_TOKEN", "wiring-check-token-1234567890")
        from lingxi.config.settings import get_settings

        get_settings.cache_clear()
        try:
            engine = build_default_engine()
            paths = {r.path for r in engine.dashboard_app().routes}
        finally:
            get_settings.cache_clear()

        # 这 5 组曾经因为 __main__ 没传参而**一个都没挂上**
        assert "/api/stream/chat" in paths, "SSE 流式端点未挂载"
        assert "/api/sessions/{chat_id}/stream" in paths, "会话 SSE 端点未挂载"
        assert "/api/experiments" in paths, "A/B 实验端点未挂载"
        assert "/experiments" not in paths, (
            "A/B 实验端点漏了 /api 前缀：前端请求 /api/experiments，"
            "挂在根路径会让整个 A/B 实验页 404"
        )
        assert "/api/seats" in paths, "坐席端点未挂载"
        assert "/mcp/tools/list" in paths and "/v1/tools" in paths, "MCP 端点未挂载"
        assert "/api/workflow/graph" in paths, "工作流 DAG 端点未挂载"
        assert "/api/workflow/execute" in paths, "工作流执行端点未挂载"


# ── 4. /metrics 与 MCP 必须鉴权 ────────────────────────────────────

class TestSensitiveEndpointsAuthed:
    def test_metrics_requires_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from lingxi.config.settings import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings.metrics, "scrape_token", SecretStr("scrape-me-please-123456"), raising=False)
        app = create_dashboard_app(TicketStore(), EscalationQueue())
        with TestClient(app) as cli:
            assert cli.get("/metrics").status_code == 401
            assert cli.get("/metrics", headers={"x-admin-token": "wrong"}).status_code == 401
            ok = cli.get("/metrics", headers={"x-admin-token": "scrape-me-please-123456"})
        assert ok.status_code == 200
        assert "zhixi" in ok.text or "# HELP" in ok.text

    def test_metrics_falls_back_to_admin_token(self, admin_headers: dict[str, str]) -> None:
        app = create_dashboard_app(TicketStore(), EscalationQueue())
        with TestClient(app) as cli:
            assert cli.get("/metrics").status_code == 401
            assert cli.get("/metrics", headers=admin_headers).status_code == 200


# ── 3b. 路由清单不倒退 ─────────────────────────────────────────────

class TestRouteInventory:
    """锁住完整路由集合，防止后续重构把某个模块的挂载改没了。

    这份清单同时是「README 声称的能力」的机器可验证版本：
    文档写了 ≠ 挂上了，这里把两者绑在一起。
    """

    def test_full_route_inventory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from lingxi.__main__ import build_default_engine

        monkeypatch.setenv("LINGXI_SECURITY__ADMIN_TOKEN", "route-check-token-1234567890")
        from lingxi.config.settings import get_settings

        get_settings.cache_clear()
        try:
            engine = build_default_engine()
            paths = {r.path for r in engine.dashboard_app().routes}
        finally:
            get_settings.cache_clear()

        # 显式端点全清单（不含 /openapi.json /docs 等自动生成项）
        expected = {
            # 核心业务
            "/api/health",
            "/api/dashboard/summary",
            "/api/tickets",
            "/api/tickets/{ticket_id}",
            "/api/queue",
            "/api/queue/claim",
            "/api/queue/complete",
            "/api/sessions",
            "/api/sessions/summarize",
            "/api/sessions/{chat_id}/stream",
            "/api/events",
            "/api/stream/chat",
            "/api/logs",
            # 知识库
            "/api/knowledge/stats",
            "/api/knowledge/docs",
            "/api/knowledge/docs/{doc_id}",
            "/api/knowledge/upload",
            "/api/knowledge/recall",
            "/api/knowledge/reset",
            # 规则 / SLA / 审计
            "/api/rules",
            "/api/rules/{name}",
            "/api/rules/evaluate",
            "/api/sla/status",
            "/api/audit",
            "/api/audit/export",
            # 人格
            "/api/persona",
            "/api/persona/preview",
            # 坐席
            "/api/seats",
            "/api/seats/{username}",
            "/api/seats/{username}/status",
            "/api/seats/assign",
            "/api/seats/metrics",
            # A/B 实验
            "/api/experiments",
            "/api/experiments/{name}",
            "/api/experiments/{name}/status",
            "/api/experiments/{name}/metrics",
            "/api/experiments/{name}/assign/{user_id}",
            # 工作流 DAG
            "/api/workflow/graph",
            "/api/workflow/graph/yaml",
            "/api/workflow/graph/validate",
            "/api/workflow/graph/preview",
            "/api/workflow/execute",
            # MCP / OpenAI 兼容
            "/mcp/tools/list",
            "/mcp/tools/call",
            "/mcp/rpc",
            "/v1/tools",
            "/v1/tools/call",
            # 指标
            "/metrics",
        }
        missing = expected - paths
        assert not missing, f"路由丢失（挂载被改没了）: {sorted(missing)}"

    def test_mcp_tools_require_admin_token(self, admin_headers: dict[str, str]) -> None:
        """/v1/tools/call 以前只看 X-Miner-Permission（那是等级不是凭据）→ 任何人可跑 shell。"""
        app = create_dashboard_app(TicketStore(), EscalationQueue())
        with TestClient(app) as cli:
            no_cred = cli.post(
                "/v1/tools/call",
                json={"name": "shell", "arguments": {"command": "echo hi"}},
                headers={"X-Miner-Permission": "4"},
            )
            assert no_cred.status_code == 401, "无凭据 + 声明 L4 就能执行 shell = 后门"

            with_cred = cli.post(
                "/v1/tools/call",
                json={"name": "shell", "arguments": {"command": "echo hi"}},
                headers=admin_headers,
            )
        assert with_cred.status_code == 200

    def test_mcp_list_requires_admin_token(self, admin_headers: dict[str, str]) -> None:
        app = create_dashboard_app(TicketStore(), EscalationQueue())
        with TestClient(app) as cli:
            assert cli.get("/mcp/tools/list").status_code == 401
            assert cli.get("/mcp/tools/list", headers=admin_headers).status_code == 200

    def test_mcp_fails_closed_without_configured_token(self) -> None:
        """未改默认 token 且非 debug → 503 拒绝，而不是把默认串当凭据放行。"""
        from lingxi.config.settings import get_settings

        settings = get_settings()
        old_token, old_debug = settings.security.admin_token, settings.debug
        settings.security.admin_token = SecretStr("change-me-in-production")
        settings.debug = False
        try:
            app = create_dashboard_app(TicketStore(), EscalationQueue())
            with TestClient(app) as cli:
                res = cli.get(
                    "/mcp/tools/list",
                    headers={"x-admin-token": "change-me-in-production"},
                )
            assert res.status_code == 503
        finally:
            settings.security.admin_token = old_token
            settings.debug = old_debug


# ── 5. 分级权限按会话给到 ──────────────────────────────────────────

class TestPermissionResolution:
    def test_default_level_used_when_no_rules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from lingxi.config.settings import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings.security, "permission_rules", {}, raising=False)
        monkeypatch.setattr(settings.security, "default_permission_level", 1, raising=False)
        assert resolve_permission_level("u1", "c1") == 1

    def test_user_rule_wins_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from lingxi.config.settings import get_settings

        settings = get_settings()
        monkeypatch.setattr(
            settings.security,
            "permission_rules",
            {"u_vip": 3, "c_lobby": 0},
            raising=False,
        )
        assert resolve_permission_level("u_vip", "c_lobby") == 3
        assert resolve_permission_level("u_other", "c_lobby") == 0
        assert resolve_permission_level("u_other", "c_none") == 1  # 未命中 → 基线

    def test_global_rule_applies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from lingxi.config.settings import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings.security, "permission_rules", {"@all": 0}, raising=False)
        assert resolve_permission_level("anyone", "anywhere") == 0

    def test_out_of_range_rule_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置笔误把人提到 L4 之上必须被忽略，不能静默放行。"""
        from lingxi.config.settings import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings.security, "permission_rules", {"u_bad": 9}, raising=False)
        assert resolve_permission_level("u_bad", "c1") == 1
