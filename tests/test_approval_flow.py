"""人工审批流（L2/L3）+ shell 分级修正的行为锁。

🔗 工程逻辑
    审批流用 LangGraph 的 `interrupt()` 实现挂起-恢复，是本项目里最容易
    写错的一块：节点会被**整体重放**、`interrupt()` 的异常不能被吞、
    thread_id 必须和首次 run 一致。本文件逐条锁住这些。

    另外锁住分级判定修掉的两个真实缺陷：
    - `rm -rf /tmp/x` 因黑名单子串匹配被判 L4 红线（误伤，本该可审批）
    - `cat /etc/shadow` 因 `cat` 在只读白名单被判 L0（凭据外泄）

🤖 AI 避坑
    - 断言审批行为必须走**真实图 + resume**，直接调节点测不到挂起
    - 黑名单是 argv 前缀语义，不是子串语义 —— 用例里要同时覆盖两者
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from lingxi.agent.approval import (
    ApprovalStore,
    get_approval_store,
    reset_approval_store,
)
from lingxi.agent.graph import ZhiXiAgent
from lingxi.agent.tools import (
    ToolRegistry,
    _create_default_registry,
    needs_approval,
    resolve_required_level,
)
from lingxi.persona.guardrails import (
    PERM_FORBIDDEN,
    PERM_HIGH_RISK,
    PERM_LOW_RISK,
    PERM_MEDIUM_RISK,
    classify_shell_command,
)

if TYPE_CHECKING:
    from lingxi.agent.state import AgentState


@pytest.fixture(autouse=True)
def _clean_store() -> None:
    reset_approval_store()
    yield
    reset_approval_store()


def _resp(content: str = "", tool_calls: list[Any] | None = None) -> Any:
    m = AsyncMock()
    m.content = content
    m.tool_calls = tool_calls
    c = AsyncMock()
    c.message = m
    r = AsyncMock()
    r.choices = [c]
    return r


def _tool_call(name: str, args: dict[str, Any], cid: str = "c1") -> Any:
    f = AsyncMock()
    f.name = name
    f.arguments = json.dumps(args)
    t = AsyncMock()
    t.function = f
    t.id = cid
    return t


async def _run(
    agent: ZhiXiAgent,
    *,
    chat_id: str,
    responses: list[Any],
    permission: int = 1,
    message: str = "删掉那个目录",
) -> dict[str, Any]:
    with patch("litellm.acompletion", new=AsyncMock(side_effect=list(responses))):
        return await agent.run(
            raw_message=message,
            chat_id=chat_id,
            user_id="u1",
            channel="qq_private",
            system_prompt="SP",
            permission_level=permission,  # type: ignore[arg-type]
        )


# ── 审批存储 ────────────────────────────────────────────────────────

class TestApprovalStore:
    def test_create_is_idempotent_per_call(self) -> None:
        """节点重放会再调 create —— 必须复用同一条，否则原记录永远 pending。"""
        store = ApprovalStore()
        kw = {
            "tool_name": "shell", "arguments": {"command": "rm -rf /tmp/x"},
            "required_level": 3, "requester_level": 1,
            "chat_id": "c1", "user_id": "u1",
        }
        a = store.create(**kw)  # type: ignore[arg-type]
        b = store.create(**kw)  # type: ignore[arg-type]
        assert a.approval_id == b.approval_id
        assert len(store.list_all()) == 1

    def test_different_args_get_separate_approvals(self) -> None:
        store = ApprovalStore()
        base = {
            "tool_name": "shell", "required_level": 3,
            "requester_level": 1, "chat_id": "c1", "user_id": "u1",
        }
        a = store.create(arguments={"command": "rm -rf /a"}, **base)  # type: ignore[arg-type]
        b = store.create(arguments={"command": "rm -rf /b"}, **base)  # type: ignore[arg-type]
        assert a.approval_id != b.approval_id

    def test_decide_once_only(self) -> None:
        """重复点击不能覆盖已做出的决策。"""
        store = ApprovalStore()
        item = store.create(
            tool_name="t", arguments={}, required_level=3,
            requester_level=1, chat_id="c", user_id="u",
        )
        assert store.decide(item.approval_id, True, "alice") is not None
        assert store.decide(item.approval_id, False, "bob") is None
        assert store.get(item.approval_id).status == "approved"  # type: ignore[union-attr]
        assert store.get(item.approval_id).approver == "alice"  # type: ignore[union-attr]

    def test_expiry_converges_pending(self) -> None:
        store = ApprovalStore(ttl_seconds=-1)
        store.create(
            tool_name="t", arguments={}, required_level=3,
            requester_level=1, chat_id="c", user_id="u",
        )
        assert store.list_pending() == []
        assert store.list_all()[0].status == "expired"

    def test_long_arguments_truncated(self) -> None:
        store = ApprovalStore()
        item = store.create(
            tool_name="t", arguments={"cmd": "X" * 5000},
            required_level=3, requester_level=1, chat_id="c", user_id="u",
        )
        assert len(item.arguments["cmd"]) < 600

    def test_thread_id_matches_graph(self) -> None:
        """thread_id 必须等于 f"{channel}:{chat_id}"，否则恢复不到挂起点。"""
        store = ApprovalStore()
        item = store.create(
            tool_name="t", arguments={}, required_level=3,
            requester_level=1, chat_id="c1", user_id="u", channel="qq_private",
        )
        assert item.thread_id == "qq_private:c1"

    def test_purge_keeps_recent(self) -> None:
        store = ApprovalStore()
        ids = []
        for i in range(5):
            it = store.create(
                tool_name=f"t{i}", arguments={}, required_level=3,
                requester_level=1, chat_id="c", user_id="u",
            )
            store.decide(it.approval_id, True, "x")
            ids.append(it.approval_id)
        store.purge(keep=2)
        assert len(store.list_all()) == 2


# ── 分级判定 ────────────────────────────────────────────────────────

class TestPermissionResolution:
    def test_needs_approval_window(self) -> None:
        """L2/L3 进审批流；L4 红线不进。"""
        assert needs_approval(2, 1)
        assert needs_approval(3, 1)
        assert not needs_approval(4, 1), "红线不是批一下就能做"
        assert not needs_approval(1, 1), "权限够就不该打扰审批人"
        assert not needs_approval(0, 3)

    def test_required_level_follows_command_not_tool(self) -> None:
        """shell 的风险等级由命令决定，不由工具声明决定。"""
        reg = _create_default_registry(blocked_commands=["rm -rf /", "mkfs"])
        assert resolve_required_level(reg, "shell", {"command": "ls"}) == 0
        assert resolve_required_level(reg, "shell", {"command": "rm -rf /tmp/x"}) == PERM_HIGH_RISK
        assert resolve_required_level(reg, "shell", {"command": "rm -rf /"}) == PERM_FORBIDDEN

    def test_unknown_tool_is_redline(self) -> None:
        """未知工具宁可判红线，也不给开绿灯。"""
        assert resolve_required_level(ToolRegistry(), "ghost", {}) == PERM_FORBIDDEN


class TestShellClassificationFixes:
    """修掉的两个真实缺陷"""

    DEFAULT_BLOCKED = ["rm -rf /", "mkfs", "dd if=/dev/zero", ":(){ :|:& };:"]

    def test_tmp_delete_is_not_redline(self) -> None:
        """`rm -rf /tmp/x` 曾因黑名单子串匹配被判 L4 —— 误伤，导致无法走审批。"""
        got = classify_shell_command("rm -rf /tmp/xyz", self.DEFAULT_BLOCKED)
        assert got == PERM_HIGH_RISK, "临时目录删除应可走人工审批，而非红线"

    @pytest.mark.parametrize("cmd", [
        "rm -rf /",
        "rm  -rf  /",
        "mkfs.ext4 /dev/sda1",
        "/sbin/mkfs.xfs /dev/sdb1",
        "dd if=/dev/zero of=/tmp/f",
        ":(){ :|:& };:",
    ])
    def test_redlines_still_blocked(self, cmd: str) -> None:
        """修前缀匹配后，红线不能被削弱。"""
        assert classify_shell_command(cmd, self.DEFAULT_BLOCKED) == PERM_FORBIDDEN

    @pytest.mark.parametrize("cmd", [
        "cat /etc/shadow",
        "cat /etc/gshadow",
        "cat ~/.ssh/id_rsa",
        "cat ~/.aws/credentials",
        "head /etc/sudoers",
    ])
    def test_credential_reads_are_redline(self, cmd: str) -> None:
        """`cat` 在只读白名单 → 过去 `cat /etc/shadow` 判 L0，凭据可直接外泄。"""
        assert classify_shell_command(cmd, self.DEFAULT_BLOCKED) == PERM_FORBIDDEN

    def test_key_file_extensions_are_redline(self) -> None:
        for cmd in ("openssl genrsa -out server.pem", "keytool -keystore a.jks"):
            assert classify_shell_command(cmd, self.DEFAULT_BLOCKED) == PERM_FORBIDDEN

    def test_harmless_reads_still_auto(self) -> None:
        """凭据兜底不能误伤正常只读。"""
        for cmd in ("ls -la", "cat /etc/hosts", "echo hi", "pwd"):
            assert classify_shell_command(cmd, self.DEFAULT_BLOCKED) == 0, cmd

    def test_blacklist_argv_prefix_semantics(self) -> None:
        """黑名单写 "rm -rf" 要能拦住所有 rm -rf（这是用户配置时的预期）。"""
        assert classify_shell_command("rm -rf /tmp/x", ["rm -rf"]) == PERM_FORBIDDEN
        assert classify_shell_command("ls -la /tmp", ["ls -la"]) == PERM_FORBIDDEN


# ── 端到端挂起 / 恢复 ──────────────────────────────────────────────

class TestApprovalFlowEndToEnd:
    @pytest.mark.asyncio
    async def test_l3_command_suspends_and_creates_pending(self) -> None:
        registry = _create_default_registry(blocked_commands=["rm -rf /"])
        agent = ZhiXiAgent(registry=registry, max_steps=4)
        await _run(
            agent, chat_id="c1",
            responses=[_resp("", [_tool_call("shell", {"command": "rm -rf /tmp/xyz"})])],
        )
        pending = get_approval_store().list_pending()
        assert len(pending) == 1
        assert pending[0].required_level == PERM_HIGH_RISK
        assert pending[0].thread_id == "qq_private:c1"

    @pytest.mark.asyncio
    async def test_approve_then_tool_executes(self) -> None:
        registry = _create_default_registry(blocked_commands=["rm -rf /"])
        agent = ZhiXiAgent(registry=registry, max_steps=4)
        await _run(
            agent, chat_id="c2",
            responses=[_resp("", [_tool_call("shell", {"command": "rm -rf /tmp/xyz"})])],
        )
        pending = get_approval_store().list_pending()
        assert pending

        with patch("litellm.acompletion", new=AsyncMock(return_value=_resp("已删除"))):
            out = await agent.resume(
                "qq_private", "c2", {"approved": True, "approver": "bob"},
            )
        assert len(out["tool_results"]) == 1
        assert out["final_reply"]
        assert get_approval_store().get(pending[0].approval_id).status == "approved"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_reject_escalates_without_executing(self) -> None:
        registry = _create_default_registry(blocked_commands=["rm -rf /"])
        agent = ZhiXiAgent(registry=registry, max_steps=4)
        await _run(
            agent, chat_id="c3",
            responses=[_resp("", [_tool_call("shell", {"command": "rm -rf /tmp/xyz"})])],
        )
        with patch("litellm.acompletion", new=AsyncMock(return_value=_resp("好的"))):
            out = await agent.resume(
                "qq_private", "c3", {"approved": False, "approver": "carol"},
            )
        assert "carol" in out["escalate_reason"]
        # 拒绝会留下一条「未执行」记录；关键是没有任何一次成功
        assert all(not r["success"] for r in out["tool_results"]), out["tool_results"]

    @pytest.mark.asyncio
    async def test_redline_never_reaches_approval(self) -> None:
        """L4 红线直接拒绝，不能通过审批放行。

        用带黑名单的 registry —— 默认空黑名单时 mkfs 只是 L3。
        """
        registry = _create_default_registry(blocked_commands=["mkfs", "rm -rf /"])
        agent = ZhiXiAgent(registry=registry, max_steps=3)
        out = await _run(
            agent, chat_id="c4", permission=3,
            responses=[_resp("", [_tool_call("shell", {"command": "mkfs.ext4 /dev/sda1"})])],
        )
        assert get_approval_store().list_pending() == [], "红线不该出现在审批列表"
        assert "红线" in out["escalate_reason"]

    @pytest.mark.asyncio
    async def test_l0_command_passes_without_approval(self) -> None:
        """权限够的只读命令不该打扰审批人。"""
        agent = ZhiXiAgent(registry=_create_default_registry(), max_steps=4)
        out = await _run(
            agent, chat_id="c5", permission=0, message="列一下目录",
            responses=[
                _resp("", [_tool_call("shell", {"command": "ls -la"})]),
                _resp("目录里有 3 个文件"),
            ],
        )
        assert get_approval_store().list_pending() == []
        assert out["final_reply"] == "目录里有 3 个文件"

    @pytest.mark.asyncio
    async def test_resume_on_dead_thread_is_safe(self) -> None:
        """没有挂起记录时 resume 不应抛异常。"""
        agent = ZhiXiAgent(registry=_create_default_registry(), max_steps=3)
        out = await agent.resume("qq_private", "nobody", {"approved": True})
        assert isinstance(out, dict)

    def test_node_outside_graph_denies_rather_than_grants(self) -> None:
        """不在图里执行（单测直调节点）时必须拒绝，不能假装审批通过。"""
        import asyncio as _asyncio

        from lingxi.agent.nodes import execute_tool_node

        state: AgentState = {
            "user_id": "u", "chat_id": "c", "channel": "qq_private",
            "raw_message": "x", "system_prompt": "", "memory_block": "",
            "lorebook_block": "", "messages": [], "step_count": 0,
            "max_steps": 3, "stage": "opening", "emotion": "calm",
            "pending_tool_call": {"tool_name": "shell", "arguments": {"command": "rm -rf /tmp/x"}, "id": "k"},
            "pending_tool_calls": [{"tool_name": "shell", "arguments": {"command": "rm -rf /tmp/x"}, "id": "k"}],
            "tool_results": [], "tool_call_signatures": [], "token_usage": {},
            "next_step": "tool", "final_reply": "", "escalate_reason": "",
            "confidence": 0.0, "current_permission": PERM_LOW_RISK,
        }
        reg = _create_default_registry(blocked_commands=["rm -rf /"])
        out = _asyncio.run(execute_tool_node(state, reg, PERM_LOW_RISK))
        assert out["next_step"] == "escalate"
        assert all(
            not r["success"] for r in out.get("tool_results", [])
        ), "图外执行不应真的跑起工具"


class TestApprovalRouting:
    """审批端点接线"""

    @pytest.fixture
    def app(self) -> Any:
        from lingxi.flow.dashboard_api import create_dashboard_app
        from lingxi.flow.queue import EscalationQueue
        from lingxi.flow.ticket import TicketStore

        return create_dashboard_app(TicketStore(), EscalationQueue())

    def test_list_requires_auth(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as cli:
            assert cli.get("/api/approvals").status_code in (401, 503)

    def test_list_returns_pending(self, app: Any, admin_headers: dict[str, str]) -> None:
        from fastapi.testclient import TestClient

        store = get_approval_store()
        store.create(
            tool_name="shell", arguments={"command": "rm -rf /tmp/x"},
            required_level=3, requester_level=1, chat_id="c", user_id="u",
        )
        with TestClient(app) as cli:
            res = cli.get("/api/approvals", headers=admin_headers)
        assert res.status_code == 200
        body = res.json()
        assert len(body) == 1
        assert body[0]["required_level"] == 3
        assert body[0]["thread_id"] if "thread_id" in body[0] else body[0]["chat_id"]

    def test_decide_unknown_id_404(self, app: Any, admin_headers: dict[str, str]) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as cli:
            res = cli.post(
                "/api/approvals/nope/approve", json={}, headers=admin_headers,
            )
        assert res.status_code == 404

    def test_bad_action_400(self, app: Any, admin_headers: dict[str, str]) -> None:
        from fastapi.testclient import TestClient

        item = get_approval_store().create(
            tool_name="t", arguments={}, required_level=3,
            requester_level=1, chat_id="c", user_id="u",
        )
        with TestClient(app) as cli:
            res = cli.post(
                f"/api/approvals/{item.approval_id}/maybe",
                json={}, headers=admin_headers,
            )
        assert res.status_code == 400


class TestApprovalUnusedImports:
    def test_medium_risk_constant_available(self) -> None:
        assert PERM_MEDIUM_RISK == 2


__all__: list[str] = []
