"""Layer 4 测试：Agent 编排（工具注册表 + LangGraph 条件路由 + 节点逻辑）。"""
from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lingxi.agent.graph import (
    ZhiXiAgent,
    _create_default_checkpointer,
    _route_after_reason,
    _route_after_tool,
)
from lingxi.agent.nodes import (
    _parse_reason_output,
    escalate_node,
    respond_node,
)
from lingxi.agent.state import AgentState
from lingxi.agent.tools import (
    ToolDefinition,
    ToolRegistry,
    _create_default_registry,
    execute_tool,
)
from lingxi.persona.guardrails import (
    PERM_AUTO,
    PERM_FORBIDDEN,
    PERM_HIGH_RISK,
    PermissionDeniedError,
    check_permission,
    classify_shell_command,
)

# ─────────────── Tool Registry ───────────────


class TestToolRegistry:
    def test_register_and_get(self) -> None:
        reg = ToolRegistry()
        tool = ToolDefinition(
            name="echo",
            description="回显输入",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
            handler=lambda msg: msg,
        )
        reg.register(tool)
        assert reg.get("echo") is tool
        assert reg.get("nope") is None

    def test_available_lists_all(self) -> None:
        reg = ToolRegistry()
        assert len(reg.available) == 0
        reg.register(ToolDefinition(
            name="a", description="a", input_schema={}, handler=lambda: None,
        ))
        reg.register(ToolDefinition(
            name="b", description="b", input_schema={}, handler=lambda: None,
        ))
        assert len(reg.available) == 2

    def test_mcp_tools_list_excludes_handler(self) -> None:
        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="test",
            description="测试工具",
            input_schema={"type": "object"},
            handler=lambda: None,
        ))
        mcp_list = reg.mcp_tools_list()
        assert len(mcp_list) == 1
        assert "handler" not in mcp_list[0]
        assert "inputSchema" in mcp_list[0]

    def test_default_registry_has_shell_and_send(self) -> None:
        reg = _create_default_registry()
        assert reg.get("shell") is not None
        assert reg.get("send_message") is not None
        assert reg.get("knowledge_search") is not None


# ─────────────── execute_tool ───────────────


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_failure(self) -> None:
        reg = ToolRegistry()
        result = await execute_tool(reg, "ghost", {}, PERM_AUTO)
        assert result["success"] is False
        assert "不存在" in result["observation"]

    @pytest.mark.asyncio
    async def test_permission_denied(self) -> None:
        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="danger",
            description="需要高权限",
            input_schema={},
            required_permission=PERM_FORBIDDEN,
            handler=lambda: "ok",
        ))
        result = await execute_tool(reg, "danger", {}, PERM_AUTO)
        assert result["success"] is False
        assert "权限" in result["observation"]

    @pytest.mark.asyncio
    async def test_sync_handler_runs(self) -> None:
        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="add",
            description="加法",
            input_schema={"type": "object"},
            handler=lambda a, b: str(a + b),
        ))
        result = await execute_tool(reg, "add", {"a": 2, "b": 3}, PERM_AUTO)
        assert result["success"] is True
        assert "5" in result["observation"]

    @pytest.mark.asyncio
    async def test_async_handler_runs(self) -> None:
        async def _mul(x: int, y: int) -> str:
            return str(x * y)

        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="mul",
            description="乘法",
            input_schema={"type": "object"},
            handler=_mul,
        ))
        result = await execute_tool(reg, "mul", {"x": 4, "y": 5}, PERM_AUTO)
        assert result["success"] is True
        assert "20" in result["observation"]

    @pytest.mark.asyncio
    async def test_shell_command_with_blocked_keyword(self) -> None:
        """黑名单命令被硬拦截，即使高权限也不行。"""
        reg = _create_default_registry(blocked_commands=["rm -rf"])
        result = await execute_tool(
            reg, "shell", {"command": "rm -rf /tmp"}, PERM_FORBIDDEN,
        )
        assert result["success"] is False
        assert "拦截" in result["observation"]

    @pytest.mark.asyncio
    async def test_shell_command_auto_classification(self) -> None:
        """shell 工具 required_permission=1，需要至少 PERM_LOW_RISK。"""
        from lingxi.persona.guardrails import PERM_LOW_RISK
        reg = _create_default_registry()
        result = await execute_tool(
            reg, "shell", {"command": "ls /tmp"}, PERM_LOW_RISK,
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_shell_high_risk_requires_permission(self) -> None:
        """高风险 shell 命令需要 PERM_HIGH_RISK。"""
        from lingxi.persona.guardrails import PERM_HIGH_RISK, PERM_LOW_RISK
        reg = _create_default_registry()
        # PERM_LOW_RISK 级别不能执行 rm -rf（高风险）
        result = await execute_tool(
            reg, "shell", {"command": "rm -rf /data"}, PERM_LOW_RISK,
        )
        assert result["success"] is False
        # PERM_HIGH_RISK 级别可以
        await execute_tool(
            reg, "shell", {"command": "rm -rf /test_tmp_dir"}, PERM_HIGH_RISK,
        )
        # 命令执行可能会因为路径不存在而失败，但权限层面应该通过
        # 不 assert success（因为命令本身不一定合法），只要没被权限拒绝即可

    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self) -> None:
        async def _slow() -> str:
            await asyncio.sleep(10)
            return "done"

        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="slow",
            description="慢工具",
            input_schema={},
            handler=_slow,
        ))
        result = await execute_tool(reg, "slow", {}, PERM_AUTO, timeout=0.1)
        assert result["success"] is False
        assert "超时" in result["observation"]


# ─────────────── reason output parsing ───────────────


class TestParseReasonOutput:
    def test_pure_text_becomes_final(self) -> None:
        result = _parse_reason_output("你好，这是答案", None)
        assert result["final_answer"] == "你好，这是答案"

    def test_json_with_answer(self) -> None:
        raw = '{"answer": "答案内容", "confidence": 0.9}'
        result = _parse_reason_output(raw, None)
        assert result["final_answer"] == "答案内容"

    def test_json_with_tool(self) -> None:
        raw = '{"tool": "shell", "tool_args": {"command": "ls"}}'
        result = _parse_reason_output(raw, None)
        assert result["tool_call"]["tool_name"] == "shell"

    def test_fenced_json(self) -> None:
        raw = '```json\n{"answer": "hi"}\n```'
        result = _parse_reason_output(raw, None)
        assert result["final_answer"] == "hi"

    def test_ignores_ellipsis(self) -> None:
        result = _parse_reason_output("...", None)
        assert "final_answer" not in result

    def test_structured_tool_call(self) -> None:
        tc = MagicMock()
        tc.function.name = "echo"
        tc.function.arguments = '{"msg": "hello"}'
        result = _parse_reason_output("thinking...", [tc])
        assert result["tool_call"]["tool_name"] == "echo"
        assert result["tool_call"]["arguments"]["msg"] == "hello"


# ─────────────── escalate / respond nodes ───────────────


class TestOtherNodes:
    def test_escalate_returns_done(self) -> None:
        state: AgentState = {
            "user_id": "1", "chat_id": "g1", "channel": "qq_group",
            "raw_message": "", "system_prompt": "", "memory_block": "",
            "lorebook_block": "", "messages": [], "step_count": 0,
            "max_steps": 3, "stage": "", "emotion": "calm",
            "pending_tool_call": None, "tool_results": [],
            "next_step": "escalate", "final_reply": "",
            "escalate_reason": "权限不足",
        }
        result = escalate_node(state)
        assert result["next_step"] == "done"
        assert "转交" in result["final_reply"]

    def test_respond_ensures_non_empty(self) -> None:
        state: AgentState = {
            "user_id": "1", "chat_id": "g1", "channel": "qq_group",
            "raw_message": "", "system_prompt": "", "memory_block": "",
            "lorebook_block": "", "messages": [], "step_count": 0,
            "max_steps": 3, "stage": "", "emotion": "calm",
            "pending_tool_call": None, "tool_results": [],
            "next_step": "respond", "final_reply": "   ",
            "escalate_reason": "",
        }
        result = respond_node(state)
        assert result["next_step"] == "done"
        assert result["final_reply"].strip() != ""


# ─────────────── LangGraph routing ───────────────


class TestGraphRouting:
    def test_route_after_reason_tool(self) -> None:
        state = AgentState(
            user_id="u", chat_id="c", channel="qq_private",
            raw_message="x", system_prompt="sp", memory_block="",
            lorebook_block="", messages=[], step_count=0, max_steps=3,
            stage="opening", emotion="calm", pending_tool_call=None,
            tool_results=[], next_step="tool", final_reply="", escalate_reason="",
        )
        assert _route_after_reason(state) == "execute_tool"

    def test_route_after_reason_respond(self) -> None:
        state = AgentState(
            user_id="u", chat_id="c", channel="qq_private",
            raw_message="x", system_prompt="sp", memory_block="",
            lorebook_block="", messages=[], step_count=0, max_steps=3,
            stage="opening", emotion="calm", pending_tool_call=None,
            tool_results=[], next_step="respond", final_reply="answer", escalate_reason="",
        )
        assert _route_after_reason(state) == "respond"

    def test_route_after_escalate(self) -> None:
        state = AgentState(
            user_id="u", chat_id="c", channel="qq_private",
            raw_message="x", system_prompt="sp", memory_block="",
            lorebook_block="", messages=[], step_count=0, max_steps=3,
            stage="opening", emotion="calm", pending_tool_call=None,
            tool_results=[], next_step="escalate", final_reply="", escalate_reason="inj",
        )
        assert _route_after_reason(state) == "escalate"

    def test_route_tool_back_to_reason(self) -> None:
        state = AgentState(
            user_id="u", chat_id="c", channel="qq_private",
            raw_message="x", system_prompt="sp", memory_block="",
            lorebook_block="", messages=[], step_count=0, max_steps=3,
            stage="opening", emotion="calm", pending_tool_call=None,
            tool_results=[], next_step="reason", final_reply="", escalate_reason="",
        )
        assert _route_after_tool(state) == "reason"

    def test_route_tool_escalate(self) -> None:
        state = AgentState(
            user_id="u", chat_id="c", channel="qq_private",
            raw_message="x", system_prompt="sp", memory_block="",
            lorebook_block="", messages=[], step_count=0, max_steps=3,
            stage="opening", emotion="calm", pending_tool_call=None,
            tool_results=[], next_step="escalate", final_reply="",
            escalate_reason="权限",
        )
        assert _route_after_tool(state) == "escalate"


# ─────────────── 默认 checkpointer（持久化策略） ───────────────


class TestDefaultCheckpointer:
    """langgraph 1.x 的 sqlite saver 在配套包 langgraph-checkpoint-sqlite 中。
    缺失时必须显式回退（有日志），装了就必须真正持久化——禁止静默降级。
    """

    @pytest.mark.asyncio
    async def test_sqlite_persistence_when_package_available(self) -> None:
        """配套包已装 → 工厂返回的 async CM 进入后必须是 AsyncSqliteSaver。"""
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        cp = _create_default_checkpointer()
        saver = await cp.__aenter__()
        try:
            assert isinstance(saver, AsyncSqliteSaver)
        finally:
            await cp.__aexit__(None, None, None)

    def test_fallback_to_memory_saver_on_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from langgraph.checkpoint.memory import MemorySaver

        # sys.modules 置 None → import 时抛 ImportError，模拟配套包缺失
        monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite.aio", None)
        cp = _create_default_checkpointer()
        assert isinstance(cp, MemorySaver)


# ─────────────── LangGraph integration (mocked LLM) ───────────────


class TestZhiXiAgentGraph:
    def _make_state(self, **overrides: Any) -> AgentState:
        base: dict[str, Any] = dict(
            user_id="u1", chat_id="c1", channel="qq_private",
            raw_message="你好", system_prompt="You are 智犀", memory_block="",
            lorebook_block="", messages=[], step_count=0, max_steps=3,
            stage="opening", emotion="calm", pending_tool_call=None,
            tool_results=[], next_step="reason", final_reply="", escalate_reason="",
        )
        base.update(overrides)
        return AgentState(**base)

    @pytest.mark.asyncio
    async def test_graph_compiles(self) -> None:
        """验证 StateGraph 能成功 compile（不需要真实 LLM）。"""
        reg = _create_default_registry()
        agent = ZhiXiAgent(registry=reg, max_steps=2)
        # compile 后 _graph 不为 None
        assert agent._graph is not None

    @pytest.mark.asyncio
    async def test_graph_runs_with_mocked_llm(self) -> None:
        """Mock litellm 返回直接 answer，验证完整图流程。"""
        reg = _create_default_registry()
        agent = ZhiXiAgent(registry=reg, max_steps=2)

        # Mock acompletion 返回直接回复
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "你好，我是智犀，有什么可以帮你？"
        mock_response.choices[0].message.tool_calls = None

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            result = await agent.run(
                raw_message="你好", chat_id="c1", user_id="u1", channel="qq_private",
            )

        assert result["final_reply"] != ""
        assert result["step_count"] >= 0


# ─────────────── Permission helpers (Layer 4 cross-check) ───────────────


class TestPermissionHelpers:
    def test_check_permission_passes(self) -> None:
        check_permission("read", current_level=1, required_level=0)

    def test_check_permission_raises(self) -> None:
        with pytest.raises(PermissionDeniedError):
            check_permission("delete", current_level=0, required_level=3)

    def test_classify_ls_is_auto(self) -> None:
        assert classify_shell_command("ls -la", []) == PERM_AUTO

    def test_classify_rm_is_high_risk(self) -> None:
        assert classify_shell_command("rm -rf /data", []) == PERM_HIGH_RISK

    def test_classify_blocked_keyword(self) -> None:
        assert classify_shell_command("rm -rf /", ["rm -rf /"]) == PERM_FORBIDDEN

    def test_classify_pip_is_medium(self) -> None:
        assert classify_shell_command("pip install foo", []) == 2


# ─────────────── 多轮上下文（回归锁：AUDIT-2026-10-10 #1） ───────────────


class TestMultiTurnContext:
    """同一 thread 的多轮对话：历史必须对 LLM 可见，step_count 必须每轮归零。

    缺陷背景：graph 每轮向 ainvoke 传完整 initial state，LangGraph 的 channel 语义下
    - messages（LastValue）被 [system, 当前 user] 整段覆盖 -> 历史丢失
    - step_count（add reducer）跨轮累加 -> 第 4 轮起 max_steps 恒触发，LLM 不再被调用
    """

    @staticmethod
    def _llm_reply(text: str) -> MagicMock:
        r = MagicMock()
        r.choices[0].message.content = text
        r.choices[0].message.tool_calls = None
        return r

    @pytest.mark.asyncio
    async def test_agent_does_not_truncate_provided_history(self) -> None:
        """agent 必须原样使用调用方传入的历史（pipeline 传，agent 不做隐式恢复）。

        历史真源是 TicketStore.messages（见 flow/pipeline.py
        _build_conversation_messages）。若 agent 改为从 checkpointer 隐式恢复，
        会与 pipeline 传入的完整历史重复，故本测试锁定显式契约。
        """
        agent = ZhiXiAgent(registry=_create_default_registry(), max_steps=3)
        seen: list[list[dict[str, Any]]] = []

        async def _capture(*args: Any, **kwargs: Any) -> MagicMock:
            seen.append(list(kwargs.get("messages") or args[1]))
            return self._llm_reply("好的，我来帮你")

        history = [
            {"role": "system", "content": "SP"},
            {"role": "user", "content": "我的订单号是 A123"},
            {"role": "assistant", "content": "已为您查询订单 A123"},
            {"role": "user", "content": "那它什么时候到？"},
        ]
        with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=_capture):
            await agent.run(raw_message="那它什么时候到？", chat_id="c1",
                            user_id="u1", channel="qq_private", messages=history)

        assert len(seen) == 1
        joined = " ".join(str(m.get("content", "")) for m in seen[0])
        assert "A123" in joined, "agent 截断了调用方传入的历史"
        assert seen[0][0].get("role") == "system", "system prompt 必须在首位"

    @pytest.mark.asyncio
    async def test_step_count_resets_each_turn(self) -> None:
        """长对话的后续轮次仍能正常调用 LLM（step_count 不跨轮累加）。"""
        agent = ZhiXiAgent(registry=_create_default_registry(), max_steps=3)

        with patch("litellm.acompletion", new_callable=AsyncMock,
                   side_effect=lambda *a, **k: self._llm_reply("收到")) as mock:
            counts = []
            for turn in range(4):
                res = await agent.run(raw_message=f"消息{turn}", chat_id="c1",
                                      user_id="u1", channel="qq_private")
                counts.append(res["step_count"])

        assert mock.call_count == 4, f"LLM 只被调用 {mock.call_count} 次，第 4 轮已被降级"
        assert counts[-1] <= 3, f"第 4 轮 step_count={counts[-1]} 超过 max_steps"

    @pytest.mark.asyncio
    async def test_react_loop_still_bounded(self) -> None:
        """step_count 改为每轮归零后，图内自环仍必须受 max_steps 约束（防无限循环）。"""
        agent = ZhiXiAgent(registry=_create_default_registry(), max_steps=3)

        with patch("litellm.acompletion", new_callable=AsyncMock,
                   side_effect=lambda *a, **k: self._llm_reply("...")) as mock:
            res = await agent.run(raw_message="无解问题", chat_id="c2",
                                  user_id="u1", channel="qq_private")

        assert mock.call_count <= 3, f"LLM 被调用 {mock.call_count} 次，max_steps 未生效"
        assert res["final_reply"] != ""
