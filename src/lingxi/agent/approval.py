"""人工审批（L2/L3）—— 挂起、决策、恢复。

🔗 工程逻辑
    BLUEPRINT 的权限分级模型要求：
      L2 中风险 → 需用户确认（群内@确认）
      L3 高风险 → 需管理员审批（坐席批准）
      L4 红线   → 始终拒绝，仅代码层操作

    原实现只有"放行 / 拒绝"两态：L3 工具对 L1 用户直接拒绝，
    L2 操作也直接拒绝 —— 等于把"需确认"读成"禁止"。

    现在用 LangGraph 的 `interrupt()` 实现真正的挂起-恢复：
      1. execute_tool_node 发现需要审批 → interrupt(payload) 挂起
      2. 坐席在控制台看到待审批 → POST /api/approvals/{id}/approve
      3. 该端点调用 agent.resume(thread_id, Command(resume=...))
      4. 图从挂起点继续，用审批人授予的权限真正执行工具

    进程重启后挂起点会丢（checkpointer 是进程内/SQLite），
    所以 `ApprovalStore` 把待审批清单落成可查询的记录，
    超时未处理的走 `expire()` 收敛成"已过期"，不会永久悬挂。

🤖 AI 避坑
    - `interrupt()` 只能在图节点里调用，抛出的 `GraphInterrupt` **不能**被
      宽泛的 `except Exception` 吞掉，否则图不会挂起而是正常往下跑
    - 恢复时传的 `Command(resume=...)` 值会原样成为 `interrupt()` 的返回值
    - 审批通过的语义是"临时授予本次调用的权限"，不是给用户提权
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from lingxi.core import get_logger

log = get_logger(__name__)

# 待审批默认存活时间（秒）。超时后自动收敛为「已过期」，
# 避免图永远挂在 interrupt 上、线程与 checkpointer 持续占用。
DEFAULT_TTL_SECONDS = 900.0


@dataclass
class ApprovalRequest:
    """一条待人工审批的高风险操作。"""

    approval_id: str
    tool_name: str
    arguments: dict[str, Any]
    required_level: int
    requester_level: int
    chat_id: str
    user_id: str
    # channel 必须一起存：LangGraph 的 thread_id 是 f"{channel}:{chat_id}"，
    # 恢复时只有 chat_id 拼不出同一个挂起点。
    channel: str = "qq_private"
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending / approved / rejected / expired
    approver: str = ""
    decided_at: float = 0.0
    note: str = ""
    # (工具, 参数, 会话) 指纹：节点重放时用它复用同一条审批
    fingerprint: str = ""

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    @property
    def thread_id(self) -> str:
        return f"{self.channel}:{self.chat_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "required_level": self.required_level,
            "requester_level": self.requester_level,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "created_at": self.created_at,
            "status": self.status,
            "approver": self.approver,
            "decided_at": self.decided_at,
            "note": self.note,
        }


class ApprovalStore:
    """线程安全的审批记录存储（进程内）。

    刻意不落 SQLite：待审批数据生命周期极短（分钟级），且必须与
    checkpointer 的存活期对齐；进程重启后旧挂起点本就无法恢复。
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._items: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def _fingerprint(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        chat_id: str,
    ) -> str:
        """审批请求的幂等指纹。"""
        return json.dumps(
            {"tool": tool_name, "args": arguments, "chat": chat_id},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

    def create(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        required_level: int,
        requester_level: int,
        chat_id: str,
        user_id: str,
        channel: str = "qq_private",
    ) -> ApprovalRequest:
        """登记审批请求。

        🔗 幂等：LangGraph 恢复挂起点时**整个节点会重放**，本方法会被再调一次。
        若每次都新建，原始那条会永远停在 pending（决策打在了一个新 id 上），
        控制台也会看到同一操作的两条待审批。因此按 (工具, 参数, 会话) 指纹复用。
        """
        fingerprint = self._fingerprint(tool_name, arguments, chat_id)
        with self._lock:
            for existing in self._items.values():
                if existing.is_pending and existing.fingerprint == fingerprint:
                    log.debug(
                        "approval_reused",
                        approval_id=existing.approval_id,
                        tool=tool_name,
                    )
                    return existing

        # 参数可能含凭据/长文本，落库前做长度收敛，避免控制台被一行命令刷屏
        safe_args = {
            k: (v if len(str(v)) <= 500 else f"{str(v)[:500]}…")
            for k, v in arguments.items()
        }
        req = ApprovalRequest(
            approval_id=uuid.uuid4().hex[:12],
            tool_name=tool_name,
            arguments=safe_args,
            required_level=required_level,
            requester_level=requester_level,
            chat_id=chat_id,
            user_id=user_id,
            channel=channel,
            fingerprint=fingerprint,
        )
        with self._lock:
            self._items[req.approval_id] = req
        log.info(
            "approval_requested",
            approval_id=req.approval_id,
            tool=tool_name,
            required_level=required_level,
            requester_level=requester_level,
            chat_id=chat_id,
        )
        return req

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            return self._items.get(approval_id)

    def list_pending(self) -> list[ApprovalRequest]:
        """待审批清单（最旧在前），顺带把过期项收敛掉。"""
        self.expire()
        with self._lock:
            pending = [r for r in self._items.values() if r.is_pending]
        return sorted(pending, key=lambda r: r.created_at)

    def list_all(self) -> list[ApprovalRequest]:
        with self._lock:
            return sorted(self._items.values(), key=lambda r: -r.created_at)

    def decide(
        self,
        approval_id: str,
        approved: bool,
        approver: str = "",
        note: str = "",
    ) -> ApprovalRequest | None:
        """记录决策。已决策过的返回 None（防重复点击重复恢复）。"""
        with self._lock:
            item = self._items.get(approval_id)
            if item is None or not item.is_pending:
                return None
            item.status = "approved" if approved else "rejected"
            item.approver = approver or "unknown"
            item.note = note
            item.decided_at = time.time()
            return item

    def expire(self) -> int:
        """把超时的待审批项标记为 expired，返回被收敛的数量。"""
        now = time.time()
        with self._lock:
            stale = [
                r for r in self._items.values()
                if r.is_pending and now - r.created_at > self._ttl
            ]
            for r in stale:
                r.status = "expired"
                r.decided_at = now
        if stale:
            log.warning("approvals_expired", count=len(stale))
        return len(stale)

    def purge(self, keep: int = 200) -> None:
        """只保留最近 keep 条已决策记录，避免长跑进程内存无限增长。"""
        with self._lock:
            decided = sorted(
                (r for r in self._items.values() if not r.is_pending),
                key=lambda r: r.decided_at or r.created_at,
                reverse=True,
            )
            for r in decided[keep:]:
                self._items.pop(r.approval_id, None)


_store: ApprovalStore | None = None


def get_approval_store() -> ApprovalStore:
    """全局审批存储单例。"""
    global _store
    if _store is None:
        _store = ApprovalStore()
    return _store


def reset_approval_store() -> None:
    """测试用：重置单例。"""
    global _store
    _store = None


__all__ = [
    "ApprovalRequest",
    "ApprovalStore",
    "DEFAULT_TTL_SECONDS",
    "get_approval_store",
    "reset_approval_store",
]
