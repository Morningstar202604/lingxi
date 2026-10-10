"""Node 5.1 — 工单系统：客服会话的生命周期管理。

🔗 工程逻辑
    每个 IM 会话对应一个 Ticket（工单），无论是否被 Agent 解决：
    - 建单时自动分类（咨询/投诉/售后/其他）
    - Agent 回复 confidence < threshold → 升级到人工队列
    - 会话 30 分钟无新消息 → 自动关闭

🤖 AI 避坑
    - 不要每个 IM 消息都建一个新 ticket，一个会话一个 ticket（按 chat_id 去重）
    - 状态机转换必须合法（不能从 closed → processing）
    - ticket_id 应包含时间信息以便排序（UUID7 > UUID4）
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TicketStatus(StrEnum):
    """工单状态机。"""

    INTAKE = "intake"           # 刚创建，等待 Agent 处理
    PROCESSING = "processing"   # Agent 正在处理
    WAITING_USER = "waiting_user"  # Agent 回复了，等用户下一条
    ESCALATED = "escalated"     # 已升级到人工队列
    RESOLVED = "resolved"       # Agent 自行解决
    CLOSED = "closed"           # 超时或用户主动关闭


class TicketCategory(StrEnum):
    """工单自动分类。"""

    INQUIRY = "inquiry"         # 商品/物流/规则咨询
    COMPLAINT = "complaint"     # 投诉（情绪激动、要求赔偿）
    AFTER_SALES = "after_sales" # 退货/退款/换货
    OTHER = "other"             # 兜底


# 合法状态转换
_VALID_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.INTAKE: {TicketStatus.PROCESSING, TicketStatus.CLOSED},
    TicketStatus.PROCESSING: {
        TicketStatus.WAITING_USER, TicketStatus.ESCALATED,
        TicketStatus.RESOLVED, TicketStatus.CLOSED,
    },
    TicketStatus.WAITING_USER: {
        TicketStatus.PROCESSING, TicketStatus.ESCALATED,
        TicketStatus.RESOLVED, TicketStatus.CLOSED,
    },
    TicketStatus.ESCALATED: {TicketStatus.PROCESSING, TicketStatus.CLOSED},
    TicketStatus.RESOLVED: {TicketStatus.CLOSED},
    TicketStatus.CLOSED: set(),
}


@dataclass
class TicketMessage:
    """工单内的一条消息记录。"""

    role: str  # "user" / "agent" / "system" / "agent_escalated"
    content: str
    timestamp: float = field(default_factory=time.time)
    confidence: float = 0.0
    escalated: bool = False


@dataclass
class Ticket:
    """客服工单。"""

    ticket_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    chat_id: str = ""
    user_id: str = ""
    channel: str = ""
    category: TicketCategory = TicketCategory.OTHER
    status: TicketStatus = TicketStatus.INTAKE
    messages: list[TicketMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    escalation_reason: str = ""

    def transition(self, new_status: TicketStatus) -> None:
        """安全的状态转换。不合法则抛异常。"""
        valid_next = _VALID_TRANSITIONS.get(self.status, set())
        if new_status not in valid_next:
            raise ValueError(
                f"非法状态转换: {self.status.value} → {new_status.value}"
            )
        self.status = new_status
        self.updated_at = time.time()

    def add_message(self, role: str, content: str, confidence: float = 0.0) -> TicketMessage:
        msg = TicketMessage(role=role, content=content, confidence=confidence)
        self.messages.append(msg)
        self.updated_at = time.time()
        return msg

    def can_auto_close(self, idle_seconds: float = 1800) -> bool:
        """检查是否超时未回复（可关闭）。"""
        if self.status in (TicketStatus.CLOSED, TicketStatus.RESOLVED):
            return False
        return (time.time() - self.updated_at) > idle_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "category": self.category.value,
            "status": self.status.value,
            "message_count": len(self.messages),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "escalation_reason": self.escalation_reason,
        }


def classify_intent(text: str) -> TicketCategory:
    """简单的关键词分类（生产环境应接入分类 LLM）。"""
    text_lower = text.lower()
    if any(kw in text_lower for kw in ("退货", "退款", "换货", "退钱", "不想要")):
        return TicketCategory.AFTER_SALES
    if any(kw in text_lower for kw in ("投诉", "骗", "假", "差", "烂", "索赔", "举报")):
        return TicketCategory.COMPLAINT
    if any(kw in text_lower for kw in ("怎么", "如何", "多少", "多久", "时间", "价格", "物流", "订单")):
        return TicketCategory.INQUIRY
    return TicketCategory.OTHER


class TicketStore:
    """内存中的工单存储（生产应换 Redis/Postgres）。"""

    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}  # ticket_id → Ticket
        self._chat_index: dict[str, str] = {}  # chat_id → ticket_id

    def get_or_create(self, chat_id: str, user_id: str = "", channel: str = "") -> Ticket:
        """获取或创建工单（确保一个 chat_id 只有一个活跃工单）。"""
        if chat_id in self._chat_index:
            existing = self._tickets.get(self._chat_index[chat_id])
            if existing and existing.status not in (TicketStatus.CLOSED, TicketStatus.RESOLVED):
                return existing
        ticket = Ticket(chat_id=chat_id, user_id=user_id, channel=channel)
        self._tickets[ticket.ticket_id] = ticket
        self._chat_index[chat_id] = ticket.ticket_id
        return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        return self._tickets.get(ticket_id)

    def list_active(self) -> list[Ticket]:
        return [
            t for t in self._tickets.values()
            if t.status not in (TicketStatus.CLOSED, TicketStatus.RESOLVED)
        ]

    def list_escalated(self) -> list[Ticket]:
        return [t for t in self._tickets.values() if t.status == TicketStatus.ESCALATED]

    def list_idle(self, idle_seconds: float = 1800) -> list[Ticket]:
        return [t for t in self._tickets.values() if t.can_auto_close(idle_seconds)]


__all__ = [
    "Ticket",
    "TicketCategory",
    "TicketMessage",
    "TicketStatus",
    "TicketStore",
    "classify_intent",
]
