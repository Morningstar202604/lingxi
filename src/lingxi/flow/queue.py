"""Node 5.2 — 人工坐席队列：升级工单的优先级调度。

🔗 工程逻辑
    当 Agent 无法处理（低置信度、权限不足、高风险命令、用户投诉）时，
    工单进入 escalate 状态，被放入坐席队列。

    队列调度策略：
    1. 投诉类工单 > 售后 > 咨询（投诉最容易升级舆情）
    2. 同优先级按 created_at FIFO
    3. 坐席"认领"后工单状态转为 processing，其他人看不到

🤖 AI 避坑
    - 不要给队列加无限制的优先级插队（会导致 starvation）
    - 坐席认领必须是原子的（compare-and-swap 语义）
    - 生产环境应使用 Redis Sorted Set 或专业的任务队列
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from lingxi.core import get_logger
from lingxi.flow.ticket import Ticket, TicketCategory

log = get_logger(__name__)

# 投诉 > 售后 > 咨询 > 其他
_CATEGORY_PRIORITY: dict[TicketCategory, int] = {
    TicketCategory.COMPLAINT: 0,    # 数字越小优先级越高
    TicketCategory.AFTER_SALES: 1,
    TicketCategory.INQUIRY: 2,
    TicketCategory.OTHER: 3,
}


@dataclass
class QueueEntry:
    """队列中的一个条目。"""

    ticket_id: str
    chat_id: str
    category: TicketCategory
    reason: str
    enqueued_at: float = field(default_factory=time.time)
    claimed_by: str | None = None
    claimed_at: float | None = None

    @property
    def priority(self) -> int:
        return _CATEGORY_PRIORITY.get(self.category, 3)

    @property
    def is_claimed(self) -> bool:
        return self.claimed_by is not None

    @property
    def wait_time(self) -> float:
        return time.time() - self.enqueued_at


class EscalationQueue:
    """内存中的升级队列 (生产环境应使用 Redis Sorted Set)。

    内置 asyncio.Queue 事件总线, 供 SSE 实时推送:
    - enqueue() 时发布 "escalated" 事件
    - dequeue() 时发布 "claimed" 事件
    前端 EventSource("/api/events") 订阅获取实时通知。
    """

    def __init__(self, max_subscribers: int = 16) -> None:
        self._entries: dict[str, QueueEntry] = {}  # ticket_id → entry
        self._lock = asyncio.Lock()  # 原子 claim
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_subs = max_subscribers

    # ── Pub/Sub (SSE realtime) ──

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """订阅队列事件, 返回一个 asyncio.Queue 作为事件流。"""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        if len(self._subs) >= self._max_subs:
            log.warning("escalation_subscribers_full", max=self._max_subs)
            return q
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        """广播事件到所有订阅者。"""
        msg = {"event": event_type, "data": data, "ts": time.time()}
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self._subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subs.discard(q)

    def enqueue(self, ticket: Ticket, reason: str) -> QueueEntry:
        """将工单加入队列。发布 "escalated" 事件。"""
        entry = QueueEntry(
            ticket_id=ticket.ticket_id,
            chat_id=ticket.chat_id,
            category=ticket.category,
            reason=reason,
        )
        self._entries[ticket.ticket_id] = entry
        self._publish("escalated", {
            "ticket_id": entry.ticket_id,
            "category": ticket.category.value,
            "reason": reason,
        })
        return entry

    async def dequeue(self, agent_id: str) -> QueueEntry | None:
        """坐席原子认领 (asyncio.Lock 防并发抢单), 发布 "claimed" 事件。"""
        async with self._lock:
            avail = [e for e in self._entries.values() if not e.is_claimed]
            if not avail:
                return None
            avail.sort(key=lambda e: (e.priority, e.enqueued_at))
            entry = avail[0]
            entry.claimed_by = agent_id
            entry.claimed_at = time.time()
            self._publish("claimed", {
                "ticket_id": entry.ticket_id,
                "agent_id": agent_id,
            })
            return entry

    def complete(self, ticket_id: str) -> None:
        """从队列中移除 (已解决或关闭), 发布 "completed" 事件。"""
        entry = self._entries.pop(ticket_id, None)
        if entry is not None:
            self._publish("completed", {"ticket_id": ticket_id})

    def list_pending(self) -> list[QueueEntry]:
        """返回所有未认领的条目，按优先级排序。"""
        pending = [e for e in self._entries.values() if not e.is_claimed]
        pending.sort(key=lambda e: (e.priority, e.enqueued_at))
        return pending

    def list_claimed(self, agent_id: str | None = None) -> list[QueueEntry]:
        """返回已认领的条目（可筛选指定坐席）。"""
        claimed = [e for e in self._entries.values() if e.is_claimed]
        if agent_id:
            claimed = [e for e in claimed if e.claimed_by == agent_id]
        return claimed

    @property
    def pending_count(self) -> int:
        return sum(1 for e in self._entries.values() if not e.is_claimed)

    def snapshot(self) -> dict[str, Any]:
        """Queue 状态快照（供 Dashboard API 使用）。"""
        pending = self.list_pending()
        claimed = self.list_claimed()
        return {
            "pending_count": len(pending),
            "claimed_count": len(claimed),
            "longest_wait": max((e.wait_time for e in pending), default=0),
            "top_5_pending": [
                {
                    "ticket_id": e.ticket_id,
                    "category": e.category.value,
                    "reason": e.reason,
                    "wait_seconds": round(e.wait_time),
                }
                for e in pending[:5]
            ],
        }


__all__ = ["QueueEntry", "EscalationQueue"]
