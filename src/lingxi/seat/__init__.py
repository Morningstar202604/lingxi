"""坐席 (人工客服) 管理系统。

提供坐席注册/状态管理、工单分配策略、REST API 路由。
"""
from lingxi.seat.assignment import AssignmentStrategy, TicketAssigner
from lingxi.seat.models import Seat, SeatStatus, SeatStore
from lingxi.seat.router import create_seat_router

__all__ = [
    "AssignmentStrategy",
    "Seat",
    "SeatStatus",
    "SeatStore",
    "TicketAssigner",
    "create_seat_router",
]
