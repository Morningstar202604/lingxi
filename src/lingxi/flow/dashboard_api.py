"""Node 5.4 — Dashboard API: FastAPI 后端接口，供 Next.js 控制台消费。

🔗 工程逻辑
    提供给监控面板的 RESTful API：
    - /api/health — 健康检查
    - /api/dashboard/summary — 实时概览（活跃工单数、队列长度、解决率）
    - /api/tickets — 活跃工单列表
    - /api/tickets/{id} — 单个工单详情（含消息历史）
    - /api/queue — 升级队列状态
    - /api/queue/claim — 坐席认领工单
    - /api/queue/complete — 坐席完成工单
    - /api/sessions/live — 当前活跃会话（WebSocket / SSE 占位）

🤖 AI 避坑
    - API 必须有认证（admin_token），绝不能暴露到公网无需鉴权
    - 不要在 API 里做耗时操作，IO 请求用 async
    - 坐席操作必须是原子的（避免多人同时认领同一工单）
"""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Header, HTTPException

from lingxi.config import get_settings
from lingxi.flow.ticket import TicketStatus, TicketStore

if TYPE_CHECKING:
    from lingxi.flow.queue import EscalationQueue


def create_dashboard_app(
    ticket_store: TicketStore,
    queue: EscalationQueue,
) -> FastAPI:
    """创建 Dashboard FastAPI 应用。"""
    app = FastAPI(title="智犀 (ZhiXi) Dashboard API", version="0.1.0")

    def _check_admin(x_admin_token: str | None = Header(None)) -> None:
        """简易 token 认证（生产应使用 OAuth2 + JWT）。"""
        settings = get_settings()
        expected = settings.security.admin_token.get_secret_value()
        if x_admin_token != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "zhixi-dashboard"}

    @app.get("/api/dashboard/summary")
    async def summary(x_admin_token: str | None = Header(None)) -> dict[str, Any]:
        _check_admin(x_admin_token)
        active = ticket_store.list_active()
        escalated = [t for t in active if t.status.value == "escalated"]
        processing = [t for t in active if t.status.value == "processing"]
        waiting = [t for t in active if t.status.value == "waiting_user"]
        queue_snapshot = queue.snapshot()

        return {
            "active_tickets": len(active),
            "escalated": len(escalated),
            "processing": len(processing),
            "waiting_user": len(waiting),
            "queue": queue_snapshot,
        }

    @app.get("/api/tickets")
    async def list_tickets(
        status: str | None = None,
        x_admin_token: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        _check_admin(x_admin_token)
        tickets = ticket_store.list_active()
        if status:
            tickets = [t for t in tickets if t.status.value == status]
        return [t.to_dict() for t in tickets]

    @app.get("/api/tickets/{ticket_id}")
    async def get_ticket(
        ticket_id: str,
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_admin(x_admin_token)
        ticket = ticket_store.get(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        d = ticket.to_dict()
        d["messages"] = [
            {
                "role": m.role,
                "content": m.content[:500],
                "timestamp": m.timestamp,
                "confidence": m.confidence,
            }
            for m in ticket.messages
        ]
        return d

    @app.get("/api/queue")
    async def queue_status(
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_admin(x_admin_token)
        return queue.snapshot()

    @app.post("/api/queue/claim")
    async def claim_ticket(
        agent_id: str,
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_admin(x_admin_token)
        entry = await queue.dequeue(agent_id)
        if not entry:
            raise HTTPException(status_code=404, detail="No pending tickets")
        return {
            "ticket_id": entry.ticket_id,
            "chat_id": entry.chat_id,
            "category": entry.category.value,
            "reason": entry.reason,
        }

    @app.post("/api/queue/complete")
    async def complete_ticket(
        ticket_id: str,
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_admin(x_admin_token)
        ticket = ticket_store.get(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        queue.complete(ticket_id)
        # 同步更新 ticket 状态为 CLOSED
        with contextlib.suppress(ValueError):
            ticket.transition(TicketStatus.CLOSED)
        return {"ticket_id": ticket_id, "status": "completed"}

    return app


__all__ = ["create_dashboard_app"]
