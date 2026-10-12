"""坐席管理 (Layer 5 扩展) + Workflow DAG 的行为锁。

🔗 工程逻辑
    这两个模块此前**从未被任何进程 include**，也**没有任何测试**：
    - seat/ 574 行 + workflow/ 774 行，属于"文件齐全但生产零可达 + 零覆盖"
    - 本文件补上行为锁，确保挂载后行为正确、鉴权不失效

🤖 AI 避坑
    - 路由顺序敏感：/seats/metrics 必须先于 /seats/{username} 注册，
      否则 metrics 会被当成 username 去查坐席（实测 404）
    - include_router 的依赖不会自动继承鉴权，模块自带 _require_admin 才有效
    - Seat 默认 OFFLINE，可分配坐席要求 IDLE + 有余量，测试里必须显式置 IDLE
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from lingxi.flow.queue import EscalationQueue
from lingxi.flow.ticket import Ticket, TicketCategory, TicketStore
from lingxi.seat.assignment import AssignmentStrategy, TicketAssigner
from lingxi.seat.models import Seat, SeatStatus, SeatStore
from lingxi.workflow.builder import build_default_pipeline_dag, to_yaml
from lingxi.workflow.models import DAGEdge
from lingxi.workflow.validator import validate_dag


def _ticket() -> Ticket:
    """构造一张最小可分配工单。"""
    return Ticket(
        chat_id="c1",
        user_id="u1",
        channel="qq_private",
        category=TicketCategory.INQUIRY,
    )


def _seat(username: str, **kw: Any) -> Seat:
    """构造一个**可被分配**的坐席（默认 OFFLINE 不可分配，显式置 IDLE）。"""
    return Seat(username=username, status=SeatStatus.IDLE, **kw)


# ── 坐席分配 ────────────────────────────────────────────────────────

class TestSeatAssignment:
    def test_least_busy_picks_least_loaded(self) -> None:
        store = SeatStore()
        store.register(_seat("alice"))
        store.register(_seat("bob"))
        bob = store.get("bob")
        assert bob is not None
        store._seats["bob"].current_ticket_count = 2  # noqa: SLF001 — 直接造负载
        assigner = TicketAssigner(store, AssignmentStrategy.LEAST_BUSY)

        ok, who = assigner.try_assign(_ticket())
        assert ok, who
        assert who == "alice"

    def test_round_robin_cycles(self) -> None:
        store = SeatStore()
        store.register(_seat("alice"))
        store.register(_seat("bob"))
        assigner = TicketAssigner(store, AssignmentStrategy.ROUND_ROBIN)

        first_ok, first = assigner.try_assign(_ticket())
        second_ok, second = assigner.try_assign(_ticket())
        assert first_ok and second_ok
        assert first != second, "轮转策略没有轮转"

    def test_full_seat_not_selected(self) -> None:
        """满员的坐席不能被分配（否则实际超载）。"""
        store = SeatStore()
        store.register(_seat("alice", max_concurrent=1))
        assigner = TicketAssigner(store)

        ok, _ = assigner.try_assign(_ticket())
        assert ok
        ok2, reason = assigner.try_assign(_ticket())
        assert not ok2
        assert "capacity" in reason or "available" in reason

    def test_offline_seat_not_selected(self) -> None:
        """OFFLINE 坐席不可分配（list_available 只认 IDLE）。"""
        store = SeatStore()
        store.register(Seat(username="alice", status=SeatStatus.OFFLINE))
        assigner = TicketAssigner(store)

        ok, reason = assigner.try_assign(_ticket())
        assert not ok
        assert "available" in reason

    def test_assignment_is_recorded_on_ticket(self) -> None:
        store = SeatStore()
        store.register(_seat("alice"))
        assigner = TicketAssigner(store)
        ticket = _ticket()
        assigner.try_assign(ticket)
        assert ticket.assigned_agent_id == "alice"

    def test_double_assignment_rejected(self) -> None:
        """防跳单：已分配的工单不能改派。"""
        store = SeatStore()
        store.register(_seat("alice"))
        assigner = TicketAssigner(store)
        ticket = _ticket()
        ticket.assigned_agent_id = "someone_else"
        ok, reason = assigner.try_assign(ticket)
        assert not ok
        assert "already assigned" in reason

    def test_release_restores_capacity(self) -> None:
        store = SeatStore()
        store.register(_seat("alice", max_concurrent=1))
        assigner = TicketAssigner(store)
        ticket = _ticket()
        assigner.try_assign(ticket)
        assigner.release(ticket)
        assert store.get("alice") is not None
        assert store.get("alice").current_ticket_count == 0  # type: ignore[union-attr]


# ── 坐席 REST 路由 ─────────────────────────────────────────────────

class TestSeatRoutes:
    @pytest.fixture
    def app(self) -> Any:
        from lingxi.flow.dashboard_api import create_dashboard_app

        return create_dashboard_app(
            TicketStore(), EscalationQueue(), seat_store=SeatStore(),
        )

    def test_metrics_not_swallowed_by_username_route(
        self, app: Any, admin_headers: dict[str, str],
    ) -> None:
        """/seats/metrics 曾被 /seats/{username} 抢先匹配 → 404。"""
        with TestClient(app) as cli:
            res = cli.get("/api/seats/metrics", headers=admin_headers)
        assert res.status_code == 200
        assert res.json()["total_seats"] == 0

    def test_unknown_seat_404(self, app: Any, admin_headers: dict[str, str]) -> None:
        with TestClient(app) as cli:
            res = cli.get("/api/seats/nobody", headers=admin_headers)
        assert res.status_code == 404

    def test_denied_without_credentials(self, app: Any) -> None:
        """无凭据必须被拒（未配置 token 时 503 fail-closed，配置了则 401）。"""
        with TestClient(app) as cli:
            read = cli.get("/api/seats")
            write = cli.post("/api/seats", json={"username": "x"})
        assert read.status_code in (401, 503)
        assert write.status_code in (401, 503)

    def test_register_seat(self, app: Any, admin_headers: dict[str, str]) -> None:
        with TestClient(app) as cli:
            ok = cli.post(
                "/api/seats",
                json={"username": "alice", "display_name": "爱丽丝"},
                headers=admin_headers,
            )
            assert ok.status_code == 201
            assert ok.json()["username"] == "alice"
            dup = cli.post(
                "/api/seats", json={"username": "alice"}, headers=admin_headers,
            )
            assert dup.status_code == 409

    def test_status_transition(self, app: Any, admin_headers: dict[str, str]) -> None:
        with TestClient(app) as cli:
            cli.post("/api/seats", json={"username": "alice"}, headers=admin_headers)
            # 新注册坐席是 OFFLINE；OFFLINE -> BUSY 不是合法转换 → 409
            bad = cli.put(
                "/api/seats/alice/status",
                json={"status": "busy"},
                headers=admin_headers,
            )
            assert bad.status_code == 409
            # 先转 IDLE 再转 BUSY
            for want in ("idle", "busy"):
                res = cli.put(
                    "/api/seats/alice/status",
                    json={"status": want},
                    headers=admin_headers,
                )
                assert res.status_code == 200, want
                assert res.json()["status"] == want

    def test_invalid_status_400(self, app: Any, admin_headers: dict[str, str]) -> None:
        with TestClient(app) as cli:
            cli.post("/api/seats", json={"username": "alice"}, headers=admin_headers)
            res = cli.put(
                "/api/seats/alice/status",
                json={"status": "nonsense"},
                headers=admin_headers,
            )
        assert res.status_code == 400


# ── Workflow DAG ───────────────────────────────────────────────────

class TestWorkflowDag:
    def test_default_dag_is_valid(self) -> None:
        assert validate_dag(build_default_pipeline_dag()) == []

    def test_mermaid_is_wellformed(self) -> None:
        from lingxi.workflow.mermaid import to_mermaid

        code = to_mermaid(build_default_pipeline_dag())
        assert code.strip().startswith("flowchart")
        assert "IM事件入口" in code

    def test_yaml_roundtrip(self) -> None:
        from lingxi.workflow.builder import from_yaml

        dag = build_default_pipeline_dag()
        restored = from_yaml(to_yaml(dag))
        assert len(restored.nodes) == len(dag.nodes)
        assert validate_dag(restored) == []

    def test_cycle_is_rejected(self) -> None:
        """引入环必须被 validate_dag 拦下，否则执行时死循环。"""
        dag = build_default_pipeline_dag()
        last = dag.nodes[-1].id
        first = dag.entry_nodes()[0] if dag.entry_nodes() else dag.nodes[0].id
        dag.edges.append(DAGEdge(source=last, target=first))
        errors = validate_dag(dag)
        assert errors, "成环的 DAG 竟然通过了校验"

    def test_unknown_node_reference_rejected(self) -> None:
        dag = build_default_pipeline_dag()
        dag.edges.append(DAGEdge(source=dag.nodes[0].id, target="ghost"))
        errors = validate_dag(dag)
        assert errors
        assert any("ghost" in e for e in errors)


class TestWorkflowRoutes:
    @pytest.fixture
    def app(self) -> Any:
        from lingxi.flow.dashboard_api import create_dashboard_app

        return create_dashboard_app(TicketStore(), EscalationQueue())

    def test_graph_returns_mermaid(
        self, app: Any, admin_headers: dict[str, str],
    ) -> None:
        with TestClient(app) as cli:
            res = cli.get("/api/workflow/graph", headers=admin_headers)
        assert res.status_code == 200
        assert res.text.strip().startswith("flowchart")

    def test_graph_denied_without_credentials(self, app: Any) -> None:
        with TestClient(app) as cli:
            assert cli.get("/api/workflow/graph").status_code in (401, 503)

    def test_preview_returns_mermaid(
        self, app: Any, admin_headers: dict[str, str],
    ) -> None:
        with TestClient(app) as cli:
            res = cli.post(
                "/api/workflow/graph/preview",
                json={"yaml": to_yaml(build_default_pipeline_dag())},
                headers=admin_headers,
            )
        assert res.status_code == 200

    def test_validate_endpoint_rejects_bad_dag(
        self, app: Any, admin_headers: dict[str, str],
    ) -> None:
        bad = {
            "name": "broken",
            "nodes": [{"id": "a", "type": "rule_engine"}],
            "edges": [{"source": "a", "target": "ghost"}],
        }
        with TestClient(app) as cli:
            res = cli.post("/api/workflow/graph/validate", json=bad, headers=admin_headers)
        assert res.status_code == 200
        assert res.json()["valid"] is False
