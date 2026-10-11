"""坐席模型 + 状态机。

🔗 工程逻辑
    坐席三态: OFFLINE -> IDLE -> BUSY -> IDLE。
    类似 Erlang/FreeSWITCH 的 agent pool 模式。

    状态机:
    - OFFLINE -> IDLE  (坐席登录/上线)
    - IDLE -> BUSY     (工单达到 max_concurrent)
    - BUSY -> IDLE     (工单降级, 释放容量后仍有余量)
    - IDLE / BUSY -> BREAK (坐席主动离席/小休)
    - BREAK -> IDLE    (坐席恢复)
    - 任何 -> OFFLINE  (坐席主动登出)

🤖 AI 避坑
    - 状态转换必须幂等 (不会把 IDLE 直接转 OFFLINE) — 实际上 OFFLINE 可以从任何状态到达,
      但要确保先释放已分配工单再下线
    - max_concurrent 配置决定同时能接几个工单
    - 不要把 Seat 和 User 混为一谈 (鉴权是 User, 接工单是 Seat)
    - 工单分配到坐席后不允许再改 seat_id (防止工单在坐席间乱飞)
"""
from __future__ import annotations

import threading
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from lingxi.core import get_logger

log = get_logger(__name__)


class SeatStatus(StrEnum):
    """坐席状态枚举。"""

    OFFLINE = "offline"
    IDLE = "idle"
    BUSY = "busy"
    BREAK = "break"


# 合法状态转换: 当前状态 -> 允许到达的状态集合
_VALID_TRANSITIONS: dict[SeatStatus, set[SeatStatus]] = {
    SeatStatus.OFFLINE: {SeatStatus.IDLE},
    SeatStatus.IDLE: {SeatStatus.BUSY, SeatStatus.BREAK, SeatStatus.OFFLINE},
    SeatStatus.BUSY: {SeatStatus.IDLE, SeatStatus.BREAK, SeatStatus.OFFLINE},
    SeatStatus.BREAK: {SeatStatus.IDLE, SeatStatus.OFFLINE},
}


class Seat(BaseModel):
    """坐席账户。"""

    username: str                  # 对应 auth.models.User 的 username
    display_name: str = ""         # 显示名
    status: SeatStatus = SeatStatus.OFFLINE
    max_concurrent: int = 3        # 最大同时处理工单数
    current_ticket_count: int = 0  # 当前活跃工单数
    total_handled: int = 0         # 累计处理数
    avg_response_seconds: float = 0.0
    last_active_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def has_capacity(self) -> bool:
        """是否还有空余工单槽位。"""
        return self.current_ticket_count < self.max_concurrent

    def can_transition(self, new_status: SeatStatus) -> bool:
        """检查状态转换是否合法。
        OFFLINE 可以从任何状态到达(强制下线),其他遵循状态机。
        """
        if new_status == SeatStatus.OFFLINE:
            return True
        return new_status in _VALID_TRANSITIONS.get(self.status, set())

    def model_post_init(self, __context: object) -> None:
        """初始化后校验。"""
        if self.max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {self.max_concurrent}")


class SeatStore:
    """内存坐席存储(支持扩展为持久化)。

    提供 CRUD + 状态转换 + 查询在线坐席。
    写操作全程持有 threading.Lock, 防止并发状态错乱。

    🔗 工程逻辑
        与 UserStore 解耦: User 管鉴权, Seat管工单分配。
        同一 username 可以既是 User (鉴权) 又是 Seat (接工单)。
    """

    def __init__(self) -> None:
        self._seats: dict[str, Seat] = {}
        self._lock = threading.Lock()

    def register(self, seat: Seat) -> bool:
        """注册新坐席。如果同名已存在则返回 False。"""
        with self._lock:
            if seat.username in self._seats:
                log.warning("seat_register_duplicate", username=seat.username)
                return False
            self._seats[seat.username] = seat
            log.info("seat_registered", username=seat.username, display_name=seat.display_name)
            return True

    def get(self, username: str) -> Seat | None:
        """按 username 查找坐席 (返回副本, 避免外部改坏内部状态)。"""
        with self._lock:
            seat = self._seats.get(username)
            if seat is None:
                return None
            # 返回副本，防止外部直接修改内部状态
            return seat.model_copy()

    def update_status(self, username: str, status: SeatStatus) -> bool:
        """更新坐席状态 (带合法校验)。"""
        with self._lock:
            seat = self._seats.get(username)
            if seat is None:
                log.warning("seat_update_status_not_found", username=username)
                return False
            if not seat.can_transition(status):
                log.warning(
                    "seat_illegal_transition",
                    username=username,
                    from_status=seat.status.value,
                    to_status=status.value,
                )
                return False
            seat.status = status
            seat.last_active_at = datetime.utcnow()
            if (
                status == SeatStatus.IDLE
                and seat.current_ticket_count >= seat.max_concurrent
            ):
                # IDLE 时仍是满载, 实际应设为 BUSY
                seat.status = SeatStatus.BUSY
            log.info("seat_status_updated", username=username, new_status=status.value)
            return True

    def list_all(self) -> list[Seat]:
        """返回所有坐席的副本列表。"""
        with self._lock:
            return [s.model_copy() for s in self._seats.values()]

    def list_available(self) -> list[Seat]:
        """返回状态为 IDLE 且有空余容量的坐席。"""
        with self._lock:
            return [
                s.model_copy()
                for s in self._seats.values()
                if s.status == SeatStatus.IDLE and s.has_capacity
            ]

    def release_ticket(self, username: str) -> bool:
        """释放一个工单槽位 (工单关单/超时)。"""
        with self._lock:
            seat = self._seats.get(username)
            if seat is None:
                return False
            if seat.current_ticket_count > 0:
                seat.current_ticket_count -= 1
            # 如果释放后有空余容量, 状态应从 BUSY 变回 IDLE
            if seat.status == SeatStatus.BUSY and seat.has_capacity:
                seat.status = SeatStatus.IDLE
            log.info("seat_ticket_released", username=username, remaining=seat.current_ticket_count)
            return True

    def assign_ticket(self, username: str) -> bool:
        """原子地分配一个工单: 增加计数, 满载时自动转 BUSY。"""
        with self._lock:
            seat = self._seats.get(username)
            if seat is None:
                return False
            if not seat.has_capacity:
                log.warning("seat_assign_no_capacity", username=username)
                return False
            if seat.status != SeatStatus.IDLE:
                log.warning(
                    "seat_assign_not_idle",
                    username=username,
                    current_status=seat.status.value,
                )
                return False
            seat.current_ticket_count += 1
            # 达到上限 -> 状态自动转为 BUSY
            if seat.current_ticket_count >= seat.max_concurrent:
                seat.status = SeatStatus.BUSY
            seat.last_active_at = datetime.utcnow()
            log.info(
                "seat_ticket_assigned",
                username=username,
                current=seat.current_ticket_count,
                max=seat.max_concurrent,
            )
            return True
