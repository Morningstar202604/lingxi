"""workflow DAG 执行引擎的行为锁。

🔗 工程逻辑
    `workflow/` 此前只有模型/校验/Mermaid/YAML —— 整块是**纸面工作流**：
    能看能改，没有一行代码按这张图走过。这组用例锁住 executor 的关键语义，
    尤其是三条最容易写错、且错了会**静默走错分支**的：

    1. 条件边一条都没命中且无无条件边 → **停下并记录原因**，
       不能随便挑一条走下去，也不能静默结束。
    2. 组件缺失 → skipped，不是 failed，整条 DAG 仍能跑完。
    3. 未校验的 DAG（成环）→ 不能死循环。

🤖 AI 避坑
    - 断言 trace 时要同时断言 `skipped`，否则"节点没跑"会被当成
      "跑成功了但没效果"
    - 默认 DAG 里 confidence_check 是 `mode="threshold"` 而非
      `mode="rules"` —— 用 rules 模式会重跑 RuleEngine 并覆盖
      rule_match，让 keyword/rule 两条条件边的语义塌掉
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from lingxi.workflow.builder import build_default_pipeline_dag
from lingxi.workflow.executor import (
    MAX_EXECUTION_STEPS,
    WorkflowContext,
    WorkflowExecutor,
    build_default_executor,
    evaluate_condition,
)
from lingxi.workflow.models import DAGEdge, DAGNode, EdgeCondition, NodeType, WorkflowDAG
from lingxi.workflow.validator import validate_dag

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    Handler = Callable[[WorkflowContext, Any], Awaitable[str]]


# ── 条件求值 ────────────────────────────────────────────────────────

class TestConditionEvaluation:
    def test_unconditional_edge_always_passes(self) -> None:
        ctx = WorkflowContext()
        ok, why = evaluate_condition(None, ctx)
        assert ok
        assert "无条件" in why
        assert evaluate_condition(EdgeCondition(), ctx)[0]

    @pytest.mark.parametrize((
        "field_name", "op", "expected_value", "setup", "expected",
    ), [
        ("confidence", "gte", 0.6, {"confidence": 0.9}, True),
        ("confidence", "gte", 0.6, {"confidence": 0.6}, True),
        ("confidence", "gte", 0.6, {"confidence": 0.59}, False),
        ("confidence", "lt", 0.6, {"confidence": 0.3}, True),
        ("confidence", "gt", 0.6, {"confidence": 0.3}, False),
        ("rule.match", "equals", "escalate", {"rule_match": "escalate"}, True),
        ("rule.match", "equals", "pass", {"rule_match": "escalate"}, False),
        ("keyword.matched", "equals", True, {"keyword_matched": True}, True),
        ("keyword_reason", "contains", "退款", {"keyword_reason": "用户要求退款"}, True),
        ("ticket.status", "equals", "escalated", {"ticket_status": "escalated"}, True),
    ])
    def test_operators(
        self,
        field_name: str,
        op: str,
        expected_value: Any,
        setup: dict[str, Any],
        expected: bool,
    ) -> None:
        ctx = WorkflowContext(**setup)
        ok, _ = evaluate_condition(
            EdgeCondition(field=field_name, operator=op, value=expected_value), ctx,
        )
        assert ok is expected, f"{field_name} {op} {expected_value!r}"

    def test_missing_field_fails_closed(self) -> None:
        """字段缺失必须不通过 —— 当成默认值通过会让分支被误激活。"""
        ctx = WorkflowContext()
        ok, why = evaluate_condition(
            EdgeCondition(field="nonexistent", operator="equals", value="x"), ctx,
        )
        assert not ok
        assert "不存在" in why

    def test_exists_distinguishes_missing_from_none(self) -> None:
        ctx = WorkflowContext()
        assert evaluate_condition(
            EdgeCondition(field="confidence", operator="exists"), ctx,
        )[0]
        assert not evaluate_condition(
            EdgeCondition(field="ghost", operator="exists"), ctx,
        )[0]

    def test_unknown_operator_fails_closed(self) -> None:
        """拼错算子名不能让某条分支被激活。"""
        ok, why = evaluate_condition(
            EdgeCondition(field="confidence", operator="approximate", value=0.6),
            WorkflowContext(),
        )
        assert not ok
        assert "未知算子" in why

    def test_bool_is_not_comparable_as_number(self) -> None:
        """bool 是 int 子类；True >= 0 会让门限判断全通过。"""
        ctx = WorkflowContext()
        ctx.rule_match = "escalate"
        ok, why = evaluate_condition(
            EdgeCondition(field="rule_match", operator="gte", value=0), ctx,
        )
        assert not ok
        assert "非数值" in why


# ── 执行语义 ────────────────────────────────────────────────────────

def _linear_dag(node_ids: list[str]) -> WorkflowDAG:
    return WorkflowDAG(
        name="linear",
        nodes=[DAGNode(id=n, type=NodeType.REPLY, label=n) for n in node_ids],
        edges=[DAGEdge(source=a, target=b) for a, b in zip(node_ids, node_ids[1:], strict=False)],
    )


def _trace_for(result: Any, node_id: str) -> Any:
    return next(t for t in result.traces if t.node_id == node_id)


class TestExecutionSemantics:
    @pytest.mark.asyncio
    async def test_linear_dag_runs_all_in_order(self) -> None:
        ex = build_default_executor()
        result = await ex.run(_linear_dag(["a", "b", "c"]), WorkflowContext(raw_message="x"))
        assert result.succeeded
        assert result.visited == ["a", "b", "c"]
        assert result.skipped == []
        assert not result.stopped_reason

    @pytest.mark.asyncio
    async def test_terminal_node_is_normal_completion(self) -> None:
        """跑到终点节点是**正常完成**，不算异常中止。

        早期实现把"没有出边"也写进 stopped_reason，导致每次成功执行
        都报一个假异常，调用方无法用它判断成败。
        """
        ex = build_default_executor()
        result = await ex.run(_linear_dag(["only"]), WorkflowContext(raw_message="x"))
        assert result.succeeded
        assert not result.stopped_reason
        assert result.ended_at == "only"

    @pytest.mark.asyncio
    async def test_no_branch_matched_stops_with_reason(self) -> None:
        """条件边全不命中且无无条件边 → 停下并说清为什么。

        不能挑一条边走下去（走到未验证的分支比停下更糟）。
        """
        dag = WorkflowDAG(
            name="deadend",
            nodes=[
                DAGNode(id="a", type=NodeType.REPLY),
                DAGNode(id="b", type=NodeType.REPLY),
            ],
            edges=[DAGEdge(
                source="a", target="b",
                condition=EdgeCondition(
                    field="rule_match", operator="equals", value="never",
                ),
            )],
        )
        result = await build_default_executor().run(dag, WorkflowContext())
        assert result.visited == ["a"]
        assert "b" in result.skipped
        assert "均未命中" in result.stopped_reason
        # 边决策必须记录，否则无从查起
        assert result.edge_decisions
        assert result.edge_decisions[0]["taken"] is False

    @pytest.mark.asyncio
    async def test_unconditional_edge_is_fallback_when_conditional_misses(self) -> None:
        dag = WorkflowDAG(
            name="fallback",
            nodes=[
                DAGNode(id="a", type=NodeType.REPLY),
                DAGNode(id="cond", type=NodeType.REPLY),
                DAGNode(id="fall", type=NodeType.REPLY),
            ],
            edges=[
                DAGEdge(source="a", target="cond", condition=EdgeCondition(
                    field="rule_match", operator="equals", value="never",
                )),
                DAGEdge(source="a", target="fall"),
            ],
        )
        result = await build_default_executor().run(dag, WorkflowContext())
        assert result.visited == ["a", "fall"]
        assert "cond" in result.skipped

    @pytest.mark.asyncio
    async def test_unregistered_node_type_is_skipped_not_failed(self) -> None:
        ex = WorkflowExecutor()
        ex._handlers.clear()  # noqa: SLF001 — 故意清空模拟未注册场景
        result = await ex.run(_linear_dag(["a"]), WorkflowContext())
        assert result.succeeded
        assert _trace_for(result, "a").status == "skipped"
        assert "未注册处理器" in _trace_for(result, "a").detail

    @pytest.mark.asyncio
    async def test_node_exception_does_not_kill_dag(self) -> None:
        async def boom(_ctx: WorkflowContext, _node: Any) -> str:
            raise ValueError("炸了")

        ex = WorkflowExecutor()
        ex.register(NodeType.REPLY, boom)
        result = await ex.run(_linear_dag(["a"]), WorkflowContext())
        assert not result.succeeded
        assert _trace_for(result, "a").status == "failed"
        assert "炸了" in result.stopped_reason

    @pytest.mark.asyncio
    async def test_cyclic_dag_is_rejected_before_execution(self) -> None:
        """成环 DAG 必须在校验期被拒 —— 不能靠步数上限硬撑。"""
        dag = WorkflowDAG(
            name="cyclic",
            nodes=[DAGNode(id="a", type=NodeType.REPLY), DAGNode(id="b", type=NodeType.REPLY)],
            edges=[DAGEdge(source="a", target="b"), DAGEdge(source="b", target="a")],
        )
        assert validate_dag(dag), "成环 DAG 应当被 validate_dag 判为非法"
        result = await build_default_executor().run(dag, WorkflowContext())
        assert not result.visited
        assert "校验失败" in result.stopped_reason

    @pytest.mark.asyncio
    async def test_step_cap_protects_unvalidated_dag(self) -> None:
        """validate=False 也必须有步数兜底（对外接口不能信任调用方）。"""
        dag = WorkflowDAG(
            name="chain",
            nodes=[DAGNode(id=f"n{i}", type=NodeType.REPLY) for i in range(MAX_EXECUTION_STEPS + 10)],
            edges=[
                DAGEdge(source=f"n{i}", target=f"n{i + 1}")
                for i in range(MAX_EXECUTION_STEPS + 9)
            ],
        )
        result = await build_default_executor().run(dag, WorkflowContext(), validate=False)
        assert len(result.visited) <= MAX_EXECUTION_STEPS
        assert "步数上限" in result.stopped_reason

    @pytest.mark.asyncio
    async def test_empty_dag_rejected_by_validation(self) -> None:
        result = await build_default_executor().run(
            WorkflowDAG(name="empty"), WorkflowContext(),
        )
        assert not result.visited
        assert "校验失败" in result.stopped_reason


# ── 真实组件接线 ────────────────────────────────────────────────────

class _FakeKnowledge:
    async def recall(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        return [{"source": "faq.md", "text": f"关于{query}的政策说明"}]


class _FakeAgent:
    def __init__(self, reply: str = "好的", confidence: float = 0.9) -> None:
        self._reply = reply
        self._confidence = confidence
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "final_reply": self._reply,
            "confidence": self._confidence,
            "escalate_reason": "",
            "tool_results": [],
            "step_count": 1,
            "token_usage": {},
        }


class TestDefaultPipelineDAG:
    @pytest.mark.asyncio
    async def test_high_confidence_takes_reply_branch(self) -> None:
        from lingxi.flow.ticket import TicketStore

        agent = _FakeAgent(reply="您的订单已发货", confidence=0.92)
        ex = build_default_executor({
            "ticket_store": TicketStore(),
            "knowledge": _FakeKnowledge(),
            "agent": agent,
        })
        result = await ex.run(
            build_default_pipeline_dag(),
            WorkflowContext(raw_message="订单到哪了", chat_id="c1", user_id="u1"),
        )
        assert result.succeeded, result.stopped_reason
        assert "reply" in result.visited, "高置信度必须走回复分支"
        assert "seat_assign" not in result.visited
        assert result.context.reply_text == "您的订单已发货"
        assert agent.calls, "LLM 节点应真实调用 agent"

    @pytest.mark.asyncio
    async def test_low_confidence_takes_seat_branch(self) -> None:
        from lingxi.flow.queue import EscalationQueue
        from lingxi.flow.ticket import TicketStore

        tickets = TicketStore()
        queue = EscalationQueue()
        ex = build_default_executor({
            "ticket_store": tickets,
            "queue": queue,
            "knowledge": _FakeKnowledge(),
            "agent": _FakeAgent(confidence=0.2),
        })
        result = await ex.run(
            build_default_pipeline_dag(),
            WorkflowContext(raw_message="说不清的问题", chat_id="c2", user_id="u2"),
        )
        assert result.succeeded, result.stopped_reason
        assert "seat_assign" in result.visited
        assert "reply" not in result.visited
        assert queue.pending_count >= 1, "低置信度应真的入队"

    @pytest.mark.asyncio
    async def test_missing_components_degrade_gracefully(self) -> None:
        """一个组件都不注入，DAG 仍应跑完（只是大部分节点 skipped）。"""
        result = await build_default_executor().run(
            build_default_pipeline_dag(),
            WorkflowContext(raw_message="你好", chat_id="c3"),
        )
        assert result.succeeded, "组件缺失不得让整条 DAG 失败"
        skipped = {t.node_id for t in result.traces if t.status == "skipped"}
        assert {"ticket_intake", "knowledge_recall", "llm_inference", "seat_assign"} <= skipped, (
            f"降级必须体现在 status 上，实际 skipped={skipped}"
        )

    @pytest.mark.asyncio
    async def test_knowledge_failure_degrades_not_raises(self) -> None:
        class _BrokenKnowledge:
            async def recall(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
                raise ConnectionError("vector db down")

        from lingxi.flow.ticket import TicketStore

        ex = build_default_executor({
            "ticket_store": TicketStore(),
            "knowledge": _BrokenKnowledge(),
            "agent": _FakeAgent(),
        })
        result = await ex.run(
            build_default_pipeline_dag(),
            WorkflowContext(raw_message="问题", chat_id="c4"),
        )
        assert result.succeeded
        assert "召回失败" in _trace_for(result, "knowledge_recall").detail

    @pytest.mark.asyncio
    async def test_output_pii_is_masked(self) -> None:
        ctx = WorkflowContext(
            raw_message="x", llm_output="您的手机号 13812345678 已实名，邮箱 bob@example.com",
        )
        await build_default_executor().run(
            _linear_dag(["reply"]), ctx,
        )
        assert "13812345678" not in ctx.reply_text, "手机号必须真的被遮蔽"
        assert "138****5678" in ctx.reply_text
        assert "bob@example.com" not in ctx.reply_text
        assert "***@example.com" in ctx.reply_text

    @pytest.mark.asyncio
    async def test_knowledge_hits_truncated_to_max_chars(self) -> None:
        class _BigKnowledge:
            async def recall(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
                return [{"source": f"s{i}", "text": "X" * 500} for i in range(10)]

        from lingxi.flow.ticket import TicketStore

        ex = build_default_executor({
            "ticket_store": TicketStore(),
            "knowledge": _BigKnowledge(),
            "agent": _FakeAgent(),
        })
        result = await ex.run(
            build_default_pipeline_dag(),
            WorkflowContext(raw_message="q", chat_id="c5"),
        )
        assert result.context.memory_block
        assert len(result.context.memory_block) <= 2100, "召回内容必须被 max_chars 截断"


class TestSeatAssignNode:
    """坐席分配节点必须对齐 TicketAssigner 真实 API。

    此前调了不存在的 `assigner.assign(...)` —— 单测只注入
    queue 不注入 assigner，测不到；真实装配（dashboard_api
    传入 assigner）下节点直接 AttributeError 崩掉整条 DAG。
    """

    @pytest.mark.asyncio
    async def test_free_seat_is_assigned(self) -> None:
        from lingxi.flow.ticket import TicketStore
        from lingxi.seat.assignment import TicketAssigner
        from lingxi.seat.models import Seat, SeatStatus, SeatStore

        store = SeatStore()
        store.register(Seat(username="alice", display_name="Alice"))
        store.update_status("alice", SeatStatus.IDLE)
        assigner = TicketAssigner(store)
        ex = build_default_executor({
            "ticket_store": TicketStore(),
            "assigner": assigner,
            "knowledge": _FakeKnowledge(),
            "agent": _FakeAgent(confidence=0.2),
        })
        result = await ex.run(
            build_default_pipeline_dag(),
            WorkflowContext(
                raw_message="说不清的问题",
                chat_id="c_seat1", user_id="u_seat1",
            ),
        )
        assert result.succeeded, result.stopped_reason
        assert result.context.seat_id == "alice"

    @pytest.mark.asyncio
    async def test_no_free_seat_falls_back_to_queue(self) -> None:
        """无空闲坐席是业务常态：必须落升级队列，而不是 failed。"""
        from lingxi.flow.queue import EscalationQueue
        from lingxi.flow.ticket import TicketStore
        from lingxi.seat.assignment import TicketAssigner
        from lingxi.seat.models import SeatStore

        assigner = TicketAssigner(SeatStore())  # 空坐席池
        queue = EscalationQueue()
        ex = build_default_executor({
            "ticket_store": TicketStore(),
            "assigner": assigner,
            "queue": queue,
            "knowledge": _FakeKnowledge(),
            "agent": _FakeAgent(confidence=0.2),
        })
        result = await ex.run(
            build_default_pipeline_dag(),
            WorkflowContext(
                raw_message="说不清的问题",
                chat_id="c_seat2", user_id="u_seat2",
            ),
        )
        assert result.succeeded, result.stopped_reason
        assert result.context.seat_id == "queued"
        assert queue.pending_count >= 1, "分配失败必须真实入队"

    @pytest.mark.asyncio
    async def test_no_components_degrades_not_fails(self) -> None:
        result = await build_default_executor().run(
            build_default_pipeline_dag(),
            WorkflowContext(
                raw_message="x", chat_id="c_seat3", user_id="u_seat3",
            ),
        )
        assert result.succeeded
        assert _trace_for(result, "seat_assign").status == "skipped"


class TestWorkflowExecuteEndpoint:
    @pytest.fixture
    def app(self) -> Any:
        from lingxi.flow.dashboard_api import create_dashboard_app
        from lingxi.flow.queue import EscalationQueue
        from lingxi.flow.ticket import TicketStore

        return create_dashboard_app(TicketStore(), EscalationQueue())

    def test_requires_auth(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as cli:
            res = cli.post("/api/workflow/execute", json={"message": "x"})
        assert res.status_code in (401, 503)

    def test_missing_message_422(self, app: Any, admin_headers: dict[str, str]) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as cli:
            res = cli.post("/api/workflow/execute", json={}, headers=admin_headers)
        assert res.status_code == 422

    def test_executes_with_admin(self, app: Any, admin_headers: dict[str, str]) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as cli:
            res = cli.post(
                "/api/workflow/execute",
                json={"message": "你好", "chat_id": "c9", "user_id": "u9"},
                headers=admin_headers,
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["succeeded"] is True
        assert body["traces"], "必须返回逐节点 trace"
        assert "visited" in body and "skipped" in body

    def test_invalid_yaml_422(self, app: Any, admin_headers: dict[str, str]) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as cli:
            res = cli.post(
                "/api/workflow/execute",
                json={"message": "x", "yaml": "name: [broken"},
                headers=admin_headers,
            )
        assert res.status_code == 422

    def test_cyclic_yaml_rejected(self, app: Any, admin_headers: dict[str, str]) -> None:
        from fastapi.testclient import TestClient

        cyclic = {
            "name": "c",
            "nodes": [
                {"id": "a", "type": "reply"},
                {"id": "b", "type": "reply"},
            ],
            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        }
        with TestClient(app) as cli:
            res = cli.post(
                "/api/workflow/execute",
                json={"message": "x", "yaml": __import__("json").dumps(cyclic)},
                headers=admin_headers,
            )
        assert res.status_code == 422
