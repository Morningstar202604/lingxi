"""工单分配策略。

🔗 工程逻辑
    三种策略:
    1. round_robin: 轮流分配（公平但忽略负载）
    2. least_busy: 给当前最闲的坐席（推荐默认）
    3. skill_based: 按标签匹配（预留接口，当前退化到 least_busy）

    分配流程 (try_assign):
    1. 查询可用坐席 (status=IDLE 且有空余容量)
    2. 按策略选出最佳坐席
    3. 原子地推进坐席 current_ticket_count (通过 SeatStore.assign_ticket)
    4. 写入 ticket.assigned_agent_id

🤖 AI 避坑
    - 分配必须是原子的 — 同一工单不能同时分给两个坐席
      (SeatStore.assign_ticket 已持有 threading.Lock 保证)
    - 如果所有坐席都坐满，保留在队列里不丢弃
    - 分配失败不能 crash，要返回失败原因
    - 写入 ticket.assigned_agent_id 后不可再改 (防跳单)
"""
from __future__ import annotations

import threading
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from lingxi.core import get_logger

if TYPE_CHECKING:
    from lingxi.seat.models import Seat, SeatStore

log = get_logger(__name__)


class AssignmentStrategy(StrEnum):
    """分配策略枚举。"""

    ROUND_ROBIN = "round_robin"
    LEAST_BUSY = "least_busy"
    SKILL_BASED = "skill_based"


class TicketAssigner:
    """工单分配器。"""

    def __init__(
        self,
        seat_store: SeatStore,
        strategy: AssignmentStrategy = AssignmentStrategy.LEAST_BUSY,
    ) -> None:
        self._store = seat_store
        self._strategy = strategy
        self._rr_index: int = 0  # round_robin 轮转指针
        self._lock = threading.Lock()  # 保护 _rr_index 和策略级原子性

    def try_assign(self, ticket: Any) -> tuple[bool, str]:
        """尝试给工单分配一个坐席。

        返回值: (success, seat_username_or_reason)

        流程:
        1. 按策略选出最佳坐席
        2. 推进 current_ticket_count
        3. 写入 ticket.assigned_agent_id
        """
        # 1. 选出最佳坐席
        seat = self._select_seat(ticket)
        if seat is None:
            # 记录原因
            available = self._store.list_available()
            if not available:
                reason = "No available seats (all offline or at capacity)"
            else:
                reason = "Selection strategy found no suitable seat"
            log.warning(
                "ticket_assign_failed",
                chat_id=getattr(ticket, "chat_id", ""),
                reason=reason,
            )
            return (False, reason)

        # 2. 分配 (atomic via SeatStore)
        username = seat.username
        if not self._store.assign_ticket(username):
            reason = f"Seat '{username}' assign_ticket failed (race condition or state change)"
            log.warning(
                "ticket_assign_atomic_failed",
                chat_id=getattr(ticket, "chat_id", ""),
                seat=username,
            )
            return (False, reason)

        # 3. 写入 ticket
        if hasattr(ticket, "assigned_agent_id"):
            if ticket.assigned_agent_id:
                # 已经有分配，释放新分配的，返回旧坐席
                self._store.release_ticket(username)
                reason = (
                    f"Ticket already assigned to '{ticket.assigned_agent_id}', "
                    "reassignment not allowed"
                )
                return (False, reason)
            ticket.assigned_agent_id = username
        else:
            # 兼容非 dataclass 或者动态赋值
            try:
                ticket.assigned_agent_id = username
            except AttributeError:
                log.error(
                    "ticket_has_no_assigned_agent_id",
                    ticket_type=type(ticket).__name__,
                )

        log.info(
            "ticket_assigned",
            chat_id=getattr(ticket, "chat_id", ""),
            seat=username,
            strategy=self._strategy.value,
        )
        return (True, username)

    def release(self, ticket: Any) -> None:
        """关单时释放坐席 capacity。"""
        agent_id: str = getattr(ticket, "assigned_agent_id", "")
        if not agent_id:
            log.debug("ticket_release_no_agent", chat_id=getattr(ticket, "chat_id", ""))
            return

        if self._store.release_ticket(agent_id):
            log.info(
                "seat_capacity_released",
                seat=agent_id,
                chat_id=getattr(ticket, "chat_id", ""),
            )
        else:
            log.warning(
                "seat_release_failed_seat_not_found",
                seat=agent_id,
            )
        # 清除 ticket 上的分配标记
        if hasattr(ticket, "assigned_agent_id"):
            ticket.assigned_agent_id = ""

    def get_metrics(self) -> dict[str, Any]:
        """坐席利用率报表。"""
        all_seats = self._store.list_all()
        total = len(all_seats)
        online = sum(
            1 for s in all_seats if s.status in ("idle", "busy")
        )
        busy_seats = [s for s in all_seats if s.status == "busy"]

        # 利用率 = 忙碌坐席数 / 在线坐席数
        utilization_rate = (
            len(busy_seats) / online if online > 0 else 0.0
        )

        # 忙碌坐席的平均工单数
        avg_tickets_per_busy_seat = (
            sum(s.current_ticket_count for s in busy_seats) / len(busy_seats)
            if busy_seats
            else 0.0
        )

        return {
            "total_seats": total,
            "online": online,
            "utilization_rate": round(utilization_rate, 2),
            "avg_tickets_per_busy_seat": round(avg_tickets_per_busy_seat, 1),
        }

    # ── 策略实现 ─────────────────────────────────────────────────

    def _select_seat(self, ticket: Any) -> Seat | None:
        """根据策略选出最佳坐席。"""
        available = self._store.list_available()
        if not available:
            return None

        if self._strategy == AssignmentStrategy.ROUND_ROBIN:
            return self._select_round_robin(available)
        if self._strategy == AssignmentStrategy.SKILL_BASED:
            # 当前退化到 least_busy, 预留扩展接口
            return self._select_least_busy(available, ticket)
        # 默认 least_busy
        return self._select_least_busy(available, ticket)

    def _select_round_robin(self, available: list[Seat]) -> Seat | None:
        """轮流分配: 简单轮转指针。"""
        with self._lock:
            if not available:
                return None
            self._rr_index %= len(available)
            chosen = available[self._rr_index]
            self._rr_index = (self._rr_index + 1) % len(available)
            return chosen

    def _select_least_busy(
        self,
        available: list[Seat],
        ticket: Any,
    ) -> Seat | None:
        """选出当前最闲的坐席 (current_ticket_count / max_concurrent 比值最小)。"""
        if not available:
            return None
        # 按利用率升序排列, 比例相同时按 current_ticket_count 绝对值
        return min(
            available,
            key=lambda s: (s.current_ticket_count / s.max_concurrent, s.current_ticket_count),
        )
