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
import time

# datetime.utcnow() 自 Python 3.12 起弃用并已排期移除；
# 且本项目把 DeprecationWarning 当 error（pyproject filterwarnings），
# 所以统一走 timezone-aware 的 datetime.now(UTC)。
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from lingxi.core import get_logger

if TYPE_CHECKING:
    from lingxi.flow.persistence import _SqliteWriter

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
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # 应答时长累计（秒），用于算 avg_response_seconds。此前无写入点 →
    # 控制台"平均响应"永远是 0（假数据）。随 seat 一起持久化。
    response_total_seconds: float = 0.0

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
    """坐席存储（可选 SQLite 持久化）。

    提供 CRUD + 状态转换 + 查询在线坐席。
    写操作全程持有 threading.Lock, 防止并发状态错乱。

    🔗 工程逻辑
        与 UserStore 解耦: User 管鉴权, Seat管工单分配。
        同一 username 可以既是 User (鉴权) 又是 Seat (接工单)。

        持久化复用 flow/persistence.py 的 `_SqliteWriter`（与 tickets/queue
        同一个 DB 文件、同一套后台写线程）—— 此前 SeatStore 是纯 dict，
        进程重启后所有坐席与负载计数归零，等于坐席管理不可用于生产。

    🤖 AI 避坑
        - 分配时刻是**进程内计时**（time.monotonic），不落盘：跨重启的
          "半途计时"没有业务意义，重启后按新分配重新计。
        - 落盘操作 fire-and-forget（写通），不阻塞分配路径。
    """

    def __init__(self, writer: _SqliteWriter | None = None) -> None:
        self._seats: dict[str, Seat] = {}
        self._lock = threading.Lock()
        self._writer = writer
        # username -> 分配时刻（monotonic，用于算首次响应耗时）
        self._assigned_at: dict[str, float] = {}

    # ── 持久化 ──

    @classmethod
    def with_sqlite(cls, db_path: str) -> SeatStore:
        """创建带 SQLite 持久化的 SeatStore 并水合已有坐席。"""
        from lingxi.flow.persistence import get_or_create_writer

        store = cls(writer=get_or_create_writer(db_path))
        store.hydrate()
        return store

    def hydrate(self) -> int:
        """从 SQLite 恢复坐席（进程启动时调用）。返回恢复条数。"""
        if self._writer is None:
            return 0
        try:
            rows = self._writer.load_all().get("seats", [])
        except Exception as e:  # noqa: BLE001 — 水合失败不应阻断启动
            log.warning("seat_hydrate_failed", error=str(e))
            return 0
        restored = 0
        with self._lock:
            for row in rows:
                try:
                    seat = _row_to_seat(row)
                except Exception:
                    log.warning("seat_hydrate_row_invalid", row=str(row)[:200])
                    continue
                self._seats[seat.username] = seat
                restored += 1
        if restored:
            log.info("seats_hydrated", count=restored)
        return restored

    def _persist(self, seat: Seat) -> None:
        if self._writer is None:
            return
        self._writer.submit({"kind": "upsert_seat", "data": _seat_to_row(seat)})

    def register(self, seat: Seat) -> bool:
        """注册新坐席。如果同名已存在则返回 False。"""
        with self._lock:
            if seat.username in self._seats:
                log.warning("seat_register_duplicate", username=seat.username)
                return False
            self._seats[seat.username] = seat
            self._persist(seat)
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
            seat.last_active_at = datetime.now(UTC)
            if (
                status == SeatStatus.IDLE
                and seat.current_ticket_count >= seat.max_concurrent
            ):
                # IDLE 时仍是满载, 实际应设为 BUSY
                seat.status = SeatStatus.BUSY
            self._persist(seat)
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
        """释放一个工单槽位 (工单关单/超时)。

        释放时结算一次"本轮处理耗时"：从分配到释放的墙钟时间计入
        response_total_seconds / total_handled，让 avg_response_seconds
        有真实来源（此前永远是 0）。
        """
        with self._lock:
            seat = self._seats.get(username)
            if seat is None:
                return False
            if seat.current_ticket_count > 0:
                seat.current_ticket_count -= 1
            # 结算本轮耗时（monotonic 差值，不受系统时间调整影响）
            started = self._assigned_at.pop(username, None)
            seat.total_handled += 1
            if started is not None:
                seat.response_total_seconds += max(0.0, time.monotonic() - started)
            if seat.total_handled > 0:
                seat.avg_response_seconds = round(
                    seat.response_total_seconds / seat.total_handled, 2
                )
            # 如果释放后有空余容量, 状态应从 BUSY 变回 IDLE
            if seat.status == SeatStatus.BUSY and seat.has_capacity:
                seat.status = SeatStatus.IDLE
            self._persist(seat)
            log.info(
                "seat_ticket_released",
                username=username,
                remaining=seat.current_ticket_count,
                avg_response=seat.avg_response_seconds,
            )
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
            seat.last_active_at = datetime.now(UTC)
            # 记录分配时刻：与 release_ticket 配对算首次响应耗时
            self._assigned_at[username] = time.monotonic()
            self._persist(seat)
            log.info(
                "seat_ticket_assigned",
                username=username,
                current=seat.current_ticket_count,
                max=seat.max_concurrent,
            )
            return True


# ── 持久化序列化（与 flow/persistence.py 的 SQLite 行格式对齐）──


def _seat_to_row(seat: Seat) -> dict[str, Any]:
    """Seat → SQLite 行 dict。"""
    return {
        "username": seat.username,
        "display_name": seat.display_name,
        "status": seat.status.value,
        "max_concurrent": seat.max_concurrent,
        "current_ticket_count": seat.current_ticket_count,
        "total_handled": seat.total_handled,
        "avg_response_seconds": seat.avg_response_seconds,
        "response_total_seconds": seat.response_total_seconds,
        "last_active_at": seat.last_active_at.isoformat() if seat.last_active_at else "",
        "created_at": seat.created_at.isoformat(),
    }


def _row_to_seat(row: dict[str, Any]) -> Seat:
    """SQLite 行 → Seat（容错：缺列/坏时间戳回退默认值）。"""

    def _parse_dt(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    return Seat(
        username=str(row["username"]),
        display_name=str(row.get("display_name") or ""),
        status=SeatStatus(str(row.get("status") or SeatStatus.OFFLINE.value)),
        max_concurrent=int(row.get("max_concurrent") or 3),
        current_ticket_count=int(row.get("current_ticket_count") or 0),
        total_handled=int(row.get("total_handled") or 0),
        avg_response_seconds=float(row.get("avg_response_seconds") or 0.0),
        response_total_seconds=float(row.get("response_total_seconds") or 0.0),
        last_active_at=_parse_dt(row.get("last_active_at")),
        created_at=_parse_dt(row.get("created_at")) or datetime.now(UTC),
    )


__all__ = ["Seat", "SeatStatus", "SeatStore"]
