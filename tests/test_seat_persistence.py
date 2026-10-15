"""坐席持久化与生命周期测试（对应两个真实缺口）。

缺口 1：SeatStore 本是纯内存 dict —— 进程重启后坐席、负载计数全部归零。
缺口 2：total_handled / avg_response_seconds 无写入点，控制台"平均响应"
        永远是 0（假数据）；分配也没有配对的归还路径。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from lingxi.seat.assignment import TicketAssigner
from lingxi.seat.models import Seat, SeatStatus, SeatStore

if TYPE_CHECKING:
    from pathlib import Path


def _seat(username: str = "alice", max_concurrent: int = 2) -> Seat:
    return Seat(username=username, display_name=username.upper(), max_concurrent=max_concurrent)


def _wait_flush(seconds: float = 0.4) -> None:
    """等 fire-and-forget 写通落盘（_SqliteWriter 是后台线程消费）。"""
    time.sleep(seconds)


class TestSeatPersistence:
    def test_registered_seat_survives_restart(self, tmp_path: Path) -> None:
        db = str(tmp_path / "seats.db")
        s1 = SeatStore.with_sqlite(db)
        assert s1.register(_seat("alice")) is True
        s1.update_status("alice", SeatStatus.IDLE)
        _wait_flush()

        s2 = SeatStore.with_sqlite(db)
        restored = s2.get("alice")
        assert restored is not None, "重启后坐席必须还在"
        assert restored.display_name == "ALICE"
        assert restored.status == SeatStatus.IDLE
        assert restored.max_concurrent == 2

    def test_counters_survive_restart(self, tmp_path: Path) -> None:
        db = str(tmp_path / "seats.db")
        s1 = SeatStore.with_sqlite(db)
        s1.register(_seat("bob"))
        s1.update_status("bob", SeatStatus.IDLE)
        s1.assign_ticket("bob")
        time.sleep(0.05)
        s1.release_ticket("bob")
        _wait_flush()

        s2 = SeatStore.with_sqlite(db)
        r = s2.get("bob")
        assert r is not None
        assert r.total_handled == 1, "累计处理数必须持久化"
        assert r.avg_response_seconds > 0, "应答时长必须持久化且非 0"

    def test_memory_store_unaffected(self) -> None:
        """不传 writer 时行为与旧版完全一致（单测/内存模式）。"""
        store = SeatStore()
        assert store.register(_seat("carol")) is True
        assert store.get("carol") is not None
        assert store.hydrate() == 0  # 无 writer 时水合为空操作

    def test_duplicate_register_rejected(self, tmp_path: Path) -> None:
        store = SeatStore.with_sqlite(str(tmp_path / "s.db"))
        assert store.register(_seat("dup")) is True
        assert store.register(_seat("dup")) is False


class TestResponseTiming:
    def test_assign_release_computes_avg(self) -> None:
        store = SeatStore()
        store.register(_seat("dave"))
        store.update_status("dave", SeatStatus.IDLE)

        assert store.assign_ticket("dave") is True
        time.sleep(0.06)
        assert store.release_ticket("dave") is True

        seat = store.get("dave")
        assert seat is not None
        assert seat.total_handled == 1
        assert seat.avg_response_seconds >= 0.05, (
            f"应答时长应反映真实耗时，实际 {seat.avg_response_seconds}"
        )

    def test_avg_is_mean_of_rounds(self) -> None:
        store = SeatStore()
        store.register(_seat("erin"))
        store.update_status("erin", SeatStatus.IDLE)
        for _ in range(2):
            store.assign_ticket("erin")
            time.sleep(0.03)
            store.release_ticket("erin")
        seat = store.get("erin")
        assert seat is not None
        assert seat.total_handled == 2
        assert seat.avg_response_seconds > 0

    def test_release_without_assign_does_not_crash(self) -> None:
        """未分配就释放（异常路径）不应抛错，也不该虚增耗时。"""
        store = SeatStore()
        store.register(_seat("frank"))
        assert store.release_ticket("frank") is True
        seat = store.get("frank")
        assert seat is not None
        assert seat.total_handled == 1  # 计数仍 +1（调用方认为完成了一次）
        assert seat.response_total_seconds == 0.0  # 但没有耗时数据

    def test_capacity_full_transitions_to_busy(self) -> None:
        store = SeatStore()
        store.register(_seat("gina", max_concurrent=1))
        store.update_status("gina", SeatStatus.IDLE)
        store.assign_ticket("gina")
        seat = store.get("gina")
        assert seat is not None and seat.status == SeatStatus.BUSY
        # 满载后不能再接
        assert store.assign_ticket("gina") is False


class TestAutoCloseReleasesSeat:
    @pytest.mark.asyncio
    async def test_idle_ticket_close_frees_seat(self) -> None:
        """自动关单必须释放坐席槽位（此前只占用不归还）。"""
        from lingxi.__main__ import _auto_close_idle_tickets
        from lingxi.flow.ticket import TicketStatus, TicketStore

        store = SeatStore()
        store.register(_seat("henry", max_concurrent=1))
        store.update_status("henry", SeatStatus.IDLE)
        assigner = TicketAssigner(store)

        tickets = TicketStore()
        ticket = tickets.get_or_create("chat-1", user_id="u1", channel="qq")
        assert assigner.try_assign(ticket) == (True, "henry")
        assert store.get("henry").current_ticket_count == 1  # type: ignore[union-attr]

        # 让工单变成 idle（idle_seconds=0 → 立即视为空闲）
        import asyncio

        task = asyncio.create_task(
            _auto_close_idle_tickets(tickets, interval=0.05, idle_seconds=0, assigner=assigner)
        )
        await asyncio.sleep(0.25)
        task.cancel()

        assert ticket.status == TicketStatus.CLOSED
        seat = store.get("henry")
        assert seat is not None
        assert seat.current_ticket_count == 0, "关单后坐席槽位必须被释放"
        assert seat.status == SeatStatus.IDLE, "释放后应回到 IDLE 可再接单"

    @pytest.mark.asyncio
    async def test_complete_endpoint_releases_seat(self, admin_headers: dict) -> None:
        """坐席在控制台点"完成"也要释放槽位。"""
        from fastapi.testclient import TestClient

        from lingxi.flow.dashboard_api import create_dashboard_app
        from lingxi.flow.queue import EscalationQueue
        from lingxi.flow.ticket import TicketStore

        ticket_store, queue = TicketStore(), EscalationQueue()
        store = SeatStore()
        store.register(_seat("iris", max_concurrent=1))
        store.update_status("iris", SeatStatus.IDLE)
        assigner = TicketAssigner(store)

        ticket = ticket_store.get_or_create("chat-2", user_id="u2", channel="qq")
        assigner.try_assign(ticket)
        assert store.get("iris").current_ticket_count == 1  # type: ignore[union-attr]

        app = create_dashboard_app(
            ticket_store, queue, seat_store=store, assigner=assigner
        )
        client = TestClient(app)
        resp = client.post(
            f"/api/queue/complete?ticket_id={ticket.ticket_id}",
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        seat = store.get("iris")
        assert seat is not None and seat.current_ticket_count == 0
