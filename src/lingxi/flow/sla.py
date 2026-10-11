"""Phase 2 — SLA 监控：跟踪工单是否超时违规。

🔗 工程逻辑
    在主动作之外加一道"服务质量"监控：
    - 首响超时 (FIRST_RESPONSE): INTAKE/PROCESSING 超过 N 秒
    - 解决超时 (RESOLUTION): 活跃工单超过 M 秒未解决
    - 升级响应超时 (ESCALATION): ESCALATED 超过 K 秒无人认领

🤖 AI 避坑
    - SLA 检查是快照式扫描，不持有 ticket 引用，状态变了不影响结果
    - business_hours_only 暂时只存储，不计算（工作时间内嵌计算留 Phase 3）
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel


class SLADefinition(BaseModel):
    """一条 SLA 规则定义。"""

    name: str = "default"
    first_response_timeout: int = 300    # 秒, 首次响应超时
    resolution_timeout: int = 3600       # 秒, 解决超时
    escalation_timeout: int = 600        # 秒, 升级响应超时
    business_hours_only: bool = False    # 是否仅工作时间计算


class SLAMonitor:
    """监控活跃 Tickets 的 SLA 状态, 标记违规 (breach)。"""

    def __init__(self, sla: SLADefinition) -> None:
        self.sla = sla

    def check_ticket(self, ticket: Any) -> dict[str, Any]:
        """检查单张工单的 SLA 状态。

        Returns:
            {"breached": bool, "reasons": list[str], "remaining_seconds": int|None}
        """
        now = time.time()
        elapsed = now - ticket.created_at
        reasons: list[str] = []

        from lingxi.flow.ticket import TicketStatus

        # 1. 首响超时
        if (ticket.status in (TicketStatus.INTAKE, TicketStatus.PROCESSING)
                and elapsed > self.sla.first_response_timeout):
            reasons.append("FIRST_RESPONSE")

        # 2. 解决超时
        if (ticket.status in (
            TicketStatus.INTAKE, TicketStatus.PROCESSING,
            TicketStatus.WAITING_USER, TicketStatus.ESCALATED,
        ) and elapsed > self.sla.resolution_timeout):
            reasons.append("RESOLUTION")

        breached = len(reasons) > 0

        # 计算最近即将超时的剩余时间
        remaining: int | None = None
        if not breached:
            candidates = []
            if ticket.status in (TicketStatus.INTAKE, TicketStatus.PROCESSING):
                candidates.append(
                    int(self.sla.first_response_timeout - elapsed)
                )
            if ticket.status in (
                TicketStatus.INTAKE, TicketStatus.PROCESSING,
                TicketStatus.WAITING_USER, TicketStatus.ESCALATED,
            ):
                candidates.append(
                    int(self.sla.resolution_timeout - elapsed)
                )
            if candidates:
                remaining = min(candidates)

        return {
            "breached": breached,
            "reasons": reasons,
            "remaining_seconds": remaining,
        }

    def check_all(self, tickets: list[Any]) -> list[dict[str, Any]]:
        """批量检查所有工单。"""
        return [
            {"ticket_id": t.ticket_id, **self.check_ticket(t)}
            for t in tickets
        ]


def format_sla_status(results: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总检查结果为统计视图。"""
    total = len(results)
    breached = sum(1 for r in results if r.get("breached", False))
    compliant = total - breached
    breach_rate = breached / total if total > 0 else 0.0

    return {
        "total": total,
        "breached": breached,
        "compliant": compliant,
        "breach_rate": round(breach_rate, 4),
    }


__all__ = ["SLADefinition", "SLAMonitor", "format_sla_status"]
