"""坐席管理 REST API 路由。

🔗 工程逻辑
    坐席管理接口, 提供:
    - 坐席注册 / 列表 / 详情
    - 坐席状态切换 (登录/登出/忙碌/休息)
    - 手动触发工单分配 (body: {chat_id})
    - 坐席利用率指标

    路由与 /api 前缀配合:
    POST   /api/seats              注册坐席
    GET    /api/seats              列出所有坐席
    GET    /api/seats/{username}   单个坐席详情
    PUT    /api/seats/{username}/status  状态切换
    POST   /api/seats/assign       触发分配 (body: {chat_id})
    GET    /api/seats/metrics      坐席利用率

🤖 AI 避坑
    - 注册坐席时 display_name 为空则 fallback 到 username
    - 状态切换由 update_status 内部校验合法性
    - assign 端点需要一个全局 ticket_store 来查找 chat_id 对应的 ticket,
      仅做触发; 分配是否成功取决于 SeatStore 的可用容量
    - 所有响应不含敏感密码字段 (Seat 模型本身不含密码)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from lingxi.core import get_logger
from lingxi.core.admin_auth import require_admin
from lingxi.seat.models import Seat, SeatStatus, SeatStore

if TYPE_CHECKING:
    from lingxi.seat.assignment import TicketAssigner

log = get_logger(__name__)


def _require_admin(x_admin_token: str | None = Header(None)) -> None:
    """坐席接口的管理凭据校验（委托 core.admin_auth —— 全项目唯一实现）。

    这些端点能注册坐席、分配与释放工单 —— 属于写管理面。此前该模块从未挂载，
    所以没有鉴权也无人察觉；一旦 include_router 就会变成裸奔的写接口。
    """
    require_admin(x_admin_token)


# ── Request / Response 模型 ─────────────────────────────────────


class RegisterSeatRequest(BaseModel):
    username: str
    display_name: str = ""
    max_concurrent: int = Field(default=3, ge=1, le=20)


class UpdateStatusRequest(BaseModel):
    status: str  # "offline" | "idle" | "busy" | "break"


class AssignRequest(BaseModel):
    chat_id: str


class SeatResponse(BaseModel):
    username: str
    display_name: str
    status: str
    max_concurrent: int
    current_ticket_count: int
    total_handled: int
    avg_response_seconds: float
    has_capacity: bool


class MetricsResponse(BaseModel):
    total_seats: int
    online: int
    utilization_rate: float
    avg_tickets_per_busy_seat: float


# ── 路由工厂 ─────────────────────────────────────────────────────


def create_seat_router(
    seat_store: SeatStore,
    assigner: TicketAssigner | None = None,
    ticket_store: Any = None,  # TicketStore | None — 查找 chat_id 对应的 ticket
) -> APIRouter:
    """创建坐席管理 Router。

    用法::

        from lingxi.seat.router import create_seat_router
        seat_store = SeatStore()
        assigner = TicketAssigner(seat_store)
        app.include_router(
            create_seat_router(seat_store, assigner=assigner),
            prefix="/api",
        )
    """
    # 全路由统一挂管理凭据依赖：注册坐席 / 分配工单都是写操作
    router = APIRouter(tags=["seats"], dependencies=[Depends(_require_admin)])

    @router.post("/seats", response_model=SeatResponse, status_code=201)
    async def register_seat(body: RegisterSeatRequest) -> SeatResponse:
        """注册新坐席。同名返回 409。"""
        seat = Seat(
            username=body.username,
            display_name=body.display_name or body.username,
            max_concurrent=body.max_concurrent,
        )
        if not seat_store.register(seat):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Seat '{body.username}' already exists",
            )
        return _to_response(seat)

    @router.get("/seats", response_model=list[SeatResponse])
    async def list_seats() -> list[SeatResponse]:
        """列出所有坐席。"""
        return [_to_response(s) for s in seat_store.list_all()]

    # ⚠️ 路由顺序敏感：FastAPI/Starlette 按注册顺序匹配，
    # "/seats/metrics" 必须注册在 "/seats/{username}" 之前，
    # 否则 metrics 会被当成 username="metrics" 去查坐席 → 404。
    @router.get("/seats/metrics", response_model=MetricsResponse)
    async def get_seat_metrics() -> MetricsResponse:
        """获取坐席利用率报表。

        如果 assigner 已配置则使用 assigner.get_metrics,
        否则返回基础统计。
        """
        if assigner is not None:
            return MetricsResponse(**assigner.get_metrics())

        # Fallback: 直接从 SeatStore 计算
        all_seats = seat_store.list_all()
        online = sum(1 for s in all_seats if s.status in ("idle", "busy"))
        busy = [s for s in all_seats if s.status == "busy"]
        return MetricsResponse(
            total_seats=len(all_seats),
            online=online,
            utilization_rate=round(len(busy) / online, 2) if online else 0.0,
            avg_tickets_per_busy_seat=round(
                sum(s.current_ticket_count for s in busy) / len(busy), 1,
            )
            if busy
            else 0.0,
        )

    @router.get("/seats/{username}", response_model=SeatResponse)
    async def get_seat(username: str) -> SeatResponse:
        """获取单个坐席详情。"""
        seat = seat_store.get(username)
        if seat is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Seat '{username}' not found",
            )
        return _to_response(seat)

    @router.put("/seats/{username}/status", response_model=SeatResponse)
    async def update_seat_status(username: str, body: UpdateStatusRequest) -> SeatResponse:
        """切换坐席状态。

        Body: {"status": "idle" | "busy" | "break" | "offline"}
        """
        try:
            new_status = SeatStatus(body.status)
        except ValueError:
            valid = [s.value for s in SeatStatus]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{body.status}'. Valid: {valid}",
            ) from None

        if seat_store.get(username) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Seat '{username}' not found",
            )

        if not seat_store.update_status(username, new_status):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Illegal status transition for seat '{username}'. "
                    "Check current status allows this transition."
                ),
            )

        seat = seat_store.get(username)
        if seat is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Seat '{username}' disappeared after status update",
            )
        return _to_response(seat)

    @router.post("/seats/assign")
    async def trigger_assign(body: AssignRequest) -> dict[str, Any]:
        """手动触发一次工单分配。

        Body: {"chat_id": "..."}

        需要传入 assigner 和 ticket_store 才能工作。
        没有分配器或找不到对应工单时返回失败原因。
        """
        if assigner is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="TicketAssigner not configured",
            )
        if ticket_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ticket_store not configured for assign endpoint",
            )

        # 查找 chat_id 对应的 ticket
        ticket = ticket_store.get(body.chat_id)
        if ticket is None:
            # 尝试用 chat_id 查找
            active = ticket_store.list_active()
            ticket = next(
                (t for t in active if getattr(t, "chat_id", None) == body.chat_id),
                None,
            )
        if ticket is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No active ticket found for chat_id '{body.chat_id}'",
            )

        success, result = assigner.try_assign(ticket)
        if success:
            return {
                "success": True,
                "assigned_to": result,
                "chat_id": body.chat_id,
            }
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Assignment failed: {result}",
        )

    return router


def _to_response(seat: Seat) -> SeatResponse:
    """将 Seat 模型转换为响应模型。"""
    return SeatResponse(
        username=seat.username,
        display_name=seat.display_name,
        status=seat.status.value,
        max_concurrent=seat.max_concurrent,
        current_ticket_count=seat.current_ticket_count,
        total_handled=seat.total_handled,
        avg_response_seconds=seat.avg_response_seconds,
        has_capacity=seat.has_capacity,
    )


__all__ = ["create_seat_router"]
