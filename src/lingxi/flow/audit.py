"""Phase 3 审计日志：不可篡改的追加型审计记录。

🔗 工程逻辑
    AuditLog 仅提供 INSERT 和 SELECT：
    - record()    — 追加一条审计事件 (fire-and-forget, 立即返回)
    - query()     — 查询最近 N 条, 支持 action / actor / since 过滤
    - export_json() — 序列化为 JSON (供 dashboard 下载)

    该表没有 UPDATE 和 DELETE 方法, 从设计上保证不可篡改
    (任何尝试修改/删除的方法调用都会被拒绝)。

    实现直接使用 stdlib sqlite3, 同步读写, 与 persistence.py 的 _SqliteWriter
    独立 (audit 不需要高吞吐写队列, 但需要强一致读取)。

🤖 AI 避坑
    - record() 用 check_same_thread=False 避免跨线程失败
    - 审计写入失败只 log.warning 不阻塞主流程 (但不隐藏, 因为审计数据很关键)
    - query 用参数化查询防 SQL 注入 (action/actor 都是不可信输入)
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from lingxi.core import get_logger

log = get_logger(__name__)


class AuditAction(Enum):
    """所有被记录的审计事件类型。"""

    TICKET_CREATED = "ticket.created"
    TICKET_TRANSITION = "ticket.transition"
    TICKET_ESCALATED = "ticket.escalated"
    TICKET_CLOSED = "ticket.closed"
    QUEUE_ENQUEUE = "queue.enqueue"
    QUEUE_DEQUEUE = "queue.dequeue"
    QUEUE_COMPLETE = "queue.complete"
    RULE_TRIGGERED = "rule.triggered"
    USER_LOGIN = "user.login"
    CONFIG_CHANGE = "config.change"
    KNOWLEDGE_UPLOAD = "knowledge.upload"
    KNOWLEDGE_DELETE = "knowledge.delete"


@dataclass
class AuditEntry:
    """一条不可变审计记录 (dataclass 语义上的不可变)。"""

    action: str
    actor: str
    target: str = ""
    detail: str = ""
    timestamp: float = field(default_factory=time.time)


# ── 拒绝篡改的占位方法装饰器 ──────────────────────────────────────


def _immutable(_self: Any, *args: Any, **kwargs: Any) -> None:
    """所有"修改/删除"方法的统一拒绝实现。"""
    raise TypeError("AuditLog 是追加型日志, 不支持修改或删除操作")


class AuditLog:
    """不可篡改的审计日志 (追加写入 SQLite, 无 update/delete 方法)。

    使用方式::

        audit = AuditLog(db_path="/tmp/test_audit.db")
        audit.record(AuditAction.TICKET_ESCALATED, actor="agent", target="t-001")
        rows = audit.query(action="ticket.escalated", limit=50)
    """

    TABLE_NAME = "audit_entries"

    def __init__(self, db_path: str = "") -> None:
        if not db_path:
            db_path = str(get_settings_safe().data_dir / "audit.db")
        self._db_path = db_path
        # check_same_thread=False: 适应可能被主线程/后台线程同时调用
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_table()

    def _create_table(self) -> None:
        """幂等建表: audit_entries (action, actor, target, detail, timestamp REAL)。"""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_entries (
                action    TEXT NOT NULL DEFAULT '',
                actor     TEXT NOT NULL DEFAULT '',
                target    TEXT NOT NULL DEFAULT '',
                detail    TEXT NOT NULL DEFAULT '',
                timestamp REAL NOT NULL DEFAULT 0
            )
            """
        )
        # 常用查询索引
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_entries(action)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_entries(actor)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_entries(timestamp DESC)"
        )
        self._conn.commit()

    # ── 公开 API (追加 + 查询) ────────────────────────────────────

    def record(
        self,
        action: AuditAction | str,
        actor: str,
        target: str = "",
        detail: str = "",
    ) -> None:
        """追加一条审计记录。同步方法, fire-and-forget。

        写入失败只记 warning 不抛异常 (不能让审计失败阻塞主流程)。
        """
        action_str = action.value if isinstance(action, AuditAction) else action
        ts = time.time()
        try:
            self._conn.execute(
                "INSERT INTO audit_entries (action, actor, target, detail, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (action_str, actor, target, detail, ts),
            )
            self._conn.commit()
        except Exception as e:
            log.warning(
                "audit_record_failed",
                action=action_str,
                actor=actor,
                error=str(e),
            )

    def query(
        self,
        *,
        action: str = "",
        actor: str = "",
        since: float = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询审计日志 (最近 limit 条)。

        支持按 action、actor 精确过滤和 since 时间窗过滤。
        结果按 timestamp DESC 排序 (最新在前)。
        """
        sql = "SELECT action, actor, target, detail, timestamp FROM audit_entries WHERE 1=1"
        params: list[Any] = []
        if action:
            sql += " AND action = ?"
            params.append(action)
        if actor:
            sql += " AND actor = ?"
            params.append(actor)
        if since > 0:
            sql += " AND timestamp >= ?"
            params.append(since)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        try:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            log.warning("audit_query_failed", error=str(e))
            return []

    def export_json(self, entries: list[dict[str, Any]]) -> str:
        """导出 JSON (for dashboard 下载)。"""
        return json.dumps(entries, ensure_ascii=False, indent=2)

    # ── 显式拒绝: 无 update / delete 方法 ─────────────────────────

    # 任何尝试"修改"审计记录的设计都会调用这些占位方法,
    # 它们在运行时报 TypeError, 从接口层面杜绝误用。

    update = _immutable
    delete = _immutable
    remove = _immutable
    clear = _immutable

    def close(self) -> None:
        """关闭底层连接 (测试清理用)。"""
        with contextlib.suppress(Exception):
            self._conn.close()


def get_settings_safe() -> Any:
    """延迟导入, 避免在模块顶层循环依赖。"""
    from lingxi.config import get_settings

    return get_settings()


__all__ = [
    "AuditAction",
    "AuditEntry",
    "AuditLog",
    "get_settings_safe",
]
