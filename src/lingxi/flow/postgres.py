"""PostgreSQL 持久化后端 (asyncpg + psycopg2)。

🔗 工程逻辑
    SQLite 适合单机开发/测试，PostgreSQL 适合生产多进程共享。
    两者对外暴露完全一致的接口，通过配置切换。

    asyncpg 是 Python 最快的异步 PostgreSQL 驱动，直接用 SQL，
    不像 SQLAlchemy ORM 那样有额外的抽象层开销。

    连接池模式：
    - 启动时创建连接池 (asyncpg.create_pool)
    - 每次读写从池中 checkout 连接
    - 应用退出时关闭连接池

    与 SQLite 后端的区别：
    - SQLite 用单线程 + 写通队列（fire-and-forget），主循环永不阻塞
    - Postgres 用连接池 + async/await，天然非阻塞，无需后台线程
    - SQLite 的 barrier 机制在 Postgres 中不需要（MVCC 保证读一致性）

🤖 AI 避坑
    - asyncpg 的连接不要跨 await（事务除外），用完归还
    - JSONB 字段用 ``::jsonb`` 转换，返回 dict
    - 连接失败要 fail-fast，不能默默降级成内存
    - 连接池 min_size/max_size 要根据并发量调
    - datetime 存 timestamp with time zone，读取用 UTC
    - ``$1, $2`` 是 asyncpg 参数占位符（不是 ``?`` 也不是 ``%s``）
    - ON CONFLICT 语法是 Postgres 特有，SQLite 用 INSERT OR REPLACE
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from lingxi.core import get_logger

log = get_logger(__name__)

# asyncpg 可能未安装（可选依赖），用 try/except 处理
try:
    import asyncpg

    HAS_ASYNC_PG = True
except ImportError:
    HAS_ASYNC_PG = False


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(UTC).isoformat()


def _parse_pg_ts(value: datetime | None) -> float:
    """将 asyncpg 返回的 timezone-aware datetime 转为 Unix timestamp。"""
    if value is None:
        return 0.0
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).timestamp()
    return value.timestamp()


# ---------------------------------------------------------------------------
# Connection pool + Writer
# ---------------------------------------------------------------------------


class PostgresWriter:
    """PostgreSQL 异步写入器 (连接池模式)。

    用法::

        writer = await PostgresWriter.create(dsn="postgresql://user:pass@host/db")
        await writer.init_schema()  # 自动建表
        await writer.execute("INSERT ...", *args)
        await writer.fetchrow("SELECT ...", *args)
        await writer.close()
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def create(
        cls,
        dsn: str,
        min_size: int = 2,
        max_size: int = 10,
    ) -> PostgresWriter:
        """工厂方法：创建连接池并返回 writer 实例。

        Args:
            dsn: PostgreSQL 连接字符串
            min_size: 连接池最小连接数
            max_size: 连接池最大连接数
        """
        if not HAS_ASYNC_PG:
            raise RuntimeError("asyncpg is not installed. Run: pip install asyncpg")
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        inst = cls(pool)
        log.info("postgres_pool_created", dsn=_mask_dsn(dsn), min=min_size, max=max_size)
        return inst

    async def close(self) -> None:
        """关闭连接池。"""
        if self._pool:
            await self._pool.close()
            log.info("postgres_pool_closed")

    # -- Schema -----------------------------------------------------------

    async def init_schema(self) -> None:
        """自动建表（幂等, IF NOT EXISTS）。"""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id        TEXT PRIMARY KEY,
                    chat_id          TEXT NOT NULL DEFAULT '',
                    user_id          TEXT NOT NULL DEFAULT '',
                    channel          TEXT NOT NULL DEFAULT '',
                    category         TEXT NOT NULL DEFAULT 'other',
                    status           TEXT NOT NULL DEFAULT 'intake',
                    messages_json    JSONB NOT NULL DEFAULT '[]',
                    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    escalation_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_pg_tickets_chat ON tickets(chat_id);
                CREATE INDEX IF NOT EXISTS idx_pg_tickets_status ON tickets(status);

                CREATE TABLE IF NOT EXISTS queue_entries (
                    ticket_id    TEXT PRIMARY KEY,
                    chat_id      TEXT NOT NULL DEFAULT '',
                    category     TEXT NOT NULL DEFAULT 'other',
                    reason       TEXT NOT NULL DEFAULT '',
                    enqueued_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    claimed_by   TEXT,
                    claimed_at   TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS idx_pg_queue_enqueued ON queue_entries(enqueued_at);

                CREATE TABLE IF NOT EXISTS memories (
                    memory_id     TEXT PRIMARY KEY,
                    user_id       TEXT NOT NULL DEFAULT '',
                    fact          TEXT NOT NULL DEFAULT '',
                    entities_json JSONB NOT NULL DEFAULT '[]',
                    confidence    REAL NOT NULL DEFAULT 1.0,
                    source        TEXT NOT NULL DEFAULT '',
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_accessed TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    access_count  INTEGER NOT NULL DEFAULT 0,
                    status        TEXT NOT NULL DEFAULT 'active'
                );
                CREATE INDEX IF NOT EXISTS idx_pg_memories_user ON memories(user_id);
                CREATE INDEX IF NOT EXISTS idx_pg_memories_status ON memories(status);
                """
            )
        log.info("postgres_schema_initialized")

    # -- Query helpers ----------------------------------------------------

    async def execute(self, sql: str, *args: Any) -> str:
        """执行单条 SQL (INSERT/UPDATE/DELETE)。"""
        async with self._pool.acquire() as conn:
            status_tag: str = await conn.execute(sql, *args)
            return status_tag

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        """执行 SELECT，返回所有行。"""
        async with self._pool.acquire() as conn:
            rows: list[Any] = await conn.fetch(sql, *args)
            return rows

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        """执行 SELECT，返回第一行或 None。"""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        """执行 SELECT，返回单个值或 None。"""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    # -- Transaction ------------------------------------------------------

    def acquire(self) -> asyncpg.pool.PoolAcquireContext:
        """获取一个带事务的连接（用于 dequeue 等需要原子性的场景）。"""
        return self._pool.acquire()


def _mask_dsn(dsn: str) -> str:
    """遮蔽 DSN 中的密码，用于日志。"""
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(dsn)
        if parsed.password:
            masked = parsed._replace(netloc=f"{parsed.username}:****@{parsed.hostname}:{parsed.port}")
            return urlunparse(masked)
    except Exception:
        pass
    return dsn


# ---------------------------------------------------------------------------
# Row <-> Domain helpers
# ---------------------------------------------------------------------------


def _ticket_from_row(row: Any) -> dict[str, Any]:
    """从 asyncpg Record 解析工单数据为 dict。"""
    messages_raw = row["messages_json"]
    if isinstance(messages_raw, str):
        messages = json.loads(messages_raw)
    elif isinstance(messages_raw, list):
        messages = messages_raw
    else:
        messages = []

    return {
        "ticket_id": row["ticket_id"],
        "chat_id": row["chat_id"],
        "user_id": row["user_id"],
        "channel": row["channel"],
        "status": row["status"],
        "category": row["category"],
        "created_at": _parse_pg_ts(row["created_at"]),
        "updated_at": _parse_pg_ts(row["updated_at"]),
        "messages": messages,
        "escalation_reason": row["escalation_reason"] or "",
    }


def _queue_entry_from_row(row: Any) -> dict[str, Any]:
    """从 asyncpg Record 解析队列条目。"""
    return {
        "ticket_id": row["ticket_id"],
        "chat_id": row["chat_id"],
        "category": row["category"],
        "reason": row["reason"],
        "enqueued_at": _parse_pg_ts(row["enqueued_at"]),
        "claimed_by": row["claimed_by"],
        "claimed_at": _parse_pg_ts(row["claimed_at"]) if row["claimed_at"] else None,
    }


def _memory_from_row(row: Any) -> dict[str, Any]:
    """从 asyncpg Record 解析记忆条目。"""
    entities_raw = row["entities_json"]
    if isinstance(entities_raw, str):
        entities = json.loads(entities_raw)
    elif isinstance(entities_raw, list):
        entities = entities_raw
    else:
        entities = []

    def _parse_dt(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(UTC)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    return {
        "memory_id": row["memory_id"],
        "user_id": row["user_id"],
        "fact": row["fact"],
        "entities": entities,
        "confidence": row["confidence"],
        "source": row["source"],
        "created_at": _parse_dt(row["created_at"]),
        "last_accessed": _parse_dt(row["last_accessed"]),
        "access_count": row["access_count"],
        "status": row["status"],
    }


def _messages_to_json(messages: list[Any]) -> str:
    """序列化 TicketMessage 列表为 JSON 字符串。"""
    return json.dumps(
        [
            {
                "role": getattr(m, "role", "user"),
                "content": getattr(m, "content", ""),
                "timestamp": getattr(m, "timestamp", 0.0),
                "confidence": getattr(m, "confidence", 0.0),
                "escalated": getattr(m, "escalated", False),
            }
            for m in messages
        ],
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# PostgresTicketStore
# ---------------------------------------------------------------------------


class PostgresTicketStore:
    """PostgreSQL 工单持久化 (对应 PersistentTicketStore)。

    async 接口 — 所有方法都是 coroutine。
    """

    writer: PostgresWriter

    def __init__(self, writer: PostgresWriter) -> None:
        self.writer = writer

    async def create(self, chat_id: str, user_id: str = "", channel: str = "") -> Any:
        """创建新工单并落盘。"""
        from lingxi.flow.ticket import Ticket

        ticket = Ticket(chat_id=chat_id, user_id=user_id, channel=channel)
        await self.writer.execute(
            """INSERT INTO tickets
                   (ticket_id, chat_id, user_id, channel, category, status,
                    messages_json, created_at, updated_at, escalation_reason)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW(), NOW(), $8)
               ON CONFLICT (ticket_id) DO NOTHING""",
            ticket.ticket_id,
            ticket.chat_id,
            ticket.user_id,
            ticket.channel,
            ticket.category.value,
            ticket.status.value,
            _messages_to_json(ticket.messages),
            ticket.escalation_reason,
        )
        log.debug("pg_ticket_created", ticket_id=ticket.ticket_id)
        return ticket

    async def get(self, ticket_id: str) -> Any:
        """通过 ticket_id 获取工单。"""
        row = await self.writer.fetchrow(
            "SELECT * FROM tickets WHERE ticket_id = $1", ticket_id
        )
        if not row:
            return None
        return _rebuild_ticket(_ticket_from_row(row))

    async def save(self, ticket: Any) -> None:
        """保存（UPDATE）工单当前状态。"""
        await self.writer.execute(
            """UPDATE tickets
                   SET status = $2, category = $3,
                       messages_json = $4::jsonb, updated_at = NOW(),
                       escalation_reason = $5
               WHERE ticket_id = $1""",
            ticket.ticket_id,
            ticket.status.value,
            ticket.category.value,
            _messages_to_json(ticket.messages),
            getattr(ticket, "escalation_reason", ""),
        )
        log.debug("pg_ticket_saved", ticket_id=ticket.ticket_id)

    async def list_active(self, limit: int = 100) -> list[Any]:
        """列出所有未关闭/未解决的工单。"""
        rows = await self.writer.fetch(
            """SELECT * FROM tickets
               WHERE status NOT IN ('closed', 'resolved')
               ORDER BY updated_at DESC LIMIT $1""",
            limit,
        )
        return [_rebuild_ticket(_ticket_from_row(r)) for r in rows]

    async def list_idle(self, limit: int = 100) -> list[Any]:
        """列出 escalated 状态的工单（等待人工处理）。"""
        rows = await self.writer.fetch(
            """SELECT * FROM tickets
               WHERE status = 'escalated'
               ORDER BY updated_at DESC LIMIT $1""",
            limit,
        )
        return [_rebuild_ticket(_ticket_from_row(r)) for r in rows]


def _rebuild_ticket(d: dict[str, Any]) -> Any:
    """从 dict 重建 Ticket 对象。"""
    from lingxi.flow.ticket import Ticket, TicketCategory, TicketMessage, TicketStatus

    msgs = [TicketMessage(**m) for m in d.get("messages", [])]
    return Ticket(
        ticket_id=d["ticket_id"],
        chat_id=d["chat_id"],
        user_id=d["user_id"],
        channel=d["channel"],
        status=TicketStatus(d["status"]),
        category=TicketCategory(d.get("category", "other")),
        created_at=d.get("created_at", 0.0),
        updated_at=d.get("updated_at", 0.0),
        messages=msgs,
        escalation_reason=d.get("escalation_reason", ""),
    )


# ---------------------------------------------------------------------------
# PostgresEscalationQueue
# ---------------------------------------------------------------------------


class PostgresEscalationQueue:
    """PostgreSQL 升级队列持久化 (对应 PersistentEscalationQueue)。"""

    writer: PostgresWriter
    _ticket_store: PostgresTicketStore | None

    def __init__(self, writer: PostgresWriter, *, ticket_store: PostgresTicketStore | None = None) -> None:
        self.writer = writer
        self._ticket_store = ticket_store

    def attach_store(self, store: PostgresTicketStore) -> None:
        """关联 ticket store（用于 dequeue 时查完整工单）。"""
        self._ticket_store = store

    # -- Queue operations -------------------------------------------------

    async def enqueue(self, ticket: Any, *, reason: str = "") -> None:
        """将工单加入队列。"""
        await self.writer.execute(
            """INSERT INTO queue_entries (ticket_id, chat_id, category, reason, enqueued_at)
               VALUES ($1, $2, $3, $4, NOW())
               ON CONFLICT (ticket_id) DO UPDATE SET
                   reason = EXCLUDED.reason, enqueued_at = NOW()""",
            ticket.ticket_id,
            ticket.chat_id,
            ticket.category.value,
            reason,
        )
        log.debug("pg_queue_enqueued", ticket_id=ticket.ticket_id, reason=reason)

    async def dequeue(self, agent_id: str) -> Any:
        """原子出队 — 认领最早的未认领条目并删除。

        使用事务保证 DELETE + SELECT 原子性。
        """
        async with self.writer.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """DELETE FROM queue_entries
                       WHERE ticket_id = (
                           SELECT ticket_id FROM queue_entries
                           WHERE claimed_by IS NULL
                           ORDER BY enqueued_at LIMIT 1
                           FOR UPDATE SKIP LOCKED
                       )
                       RETURNING ticket_id"""
            )
        if not row:
            return None

        ticket_id: str = row["ticket_id"]
        log.debug("pg_queue_dequeued", ticket_id=ticket_id, agent_id=agent_id)

        if self._ticket_store is not None:
            return await self._ticket_store.get(ticket_id)
        return None

    async def complete(self, ticket_id: str) -> None:
        """从队列移除（已解决或关闭）。"""
        await self.writer.execute(
            "DELETE FROM queue_entries WHERE ticket_id = $1", ticket_id
        )
        log.debug("pg_queue_completed", ticket_id=ticket_id)

    async def list_pending(self, limit: int = 100) -> list[str]:
        """列出所有未认领的 ticket_id。"""
        rows = await self.writer.fetch(
            """SELECT ticket_id FROM queue_entries
               WHERE claimed_by IS NULL
               ORDER BY enqueued_at LIMIT $1""",
            limit,
        )
        return [r["ticket_id"] for r in rows]

    async def list_claimed(self, agent_id: str | None = None, limit: int = 100) -> list[str]:
        """列出已认领的 ticket_id。"""
        if agent_id:
            rows = await self.writer.fetch(
                """SELECT ticket_id FROM queue_entries
                   WHERE claimed_by = $1
                   ORDER BY claimed_at DESC LIMIT $2""",
                agent_id, limit,
            )
        else:
            rows = await self.writer.fetch(
                """SELECT ticket_id FROM queue_entries
                   WHERE claimed_by IS NOT NULL
                   ORDER BY claimed_at DESC LIMIT $1""",
                limit,
            )
        return [r["ticket_id"] for r in rows]

    async def pending_count(self) -> int:
        """返回未认领条目数。"""
        return await self.writer.fetchval(
            "SELECT COUNT(*) FROM queue_entries WHERE claimed_by IS NULL"
        ) or 0

    # -- Sync compatibility stubs (EscalationQueue Protocol) --------------

    def size(self) -> int:
        """同步 stub — 返回 0。

        真正的计数请用 ``await queue.pending_count()``。
        保留此方法仅为兼容 ``EscalationQueue`` Protocol。
        """
        return 0


# ---------------------------------------------------------------------------
# PostgresMemoryPipeline
# ---------------------------------------------------------------------------


class PostgresMemoryPipeline:
    """PostgreSQL 记忆持久化 (对应 PersistentMemoryPipeline)。

    写操作直接 await 写入（asyncpg 天然非阻塞），
    读操作从 Postgres 查询（热数据由 LRU cache 加速）。
    """

    writer: PostgresWriter

    def __init__(self, writer: PostgresWriter) -> None:
        self.writer = writer

    async def _upsert_memory(self, item: Any) -> None:
        """插入或更新一条记忆。"""
        await self.writer.execute(
            """INSERT INTO memories
                   (memory_id, user_id, fact, entities_json, confidence,
                    source, created_at, last_accessed, access_count, status)
               VALUES ($1, $2, $3, $4::jsonb, $5, $6, NOW(), NOW(), $7, $8)
               ON CONFLICT (memory_id) DO UPDATE SET
                   fact = EXCLUDED.fact,
                   entities_json = EXCLUDED.entities_json,
                   confidence = EXCLUDED.confidence,
                   last_accessed = NOW()""",
            item.memory_id,
            item.user_id,
            item.fact,
            json.dumps(item.entities, ensure_ascii=False),
            item.confidence,
            item.source,
            item.access_count,
            item.status,
        )

    async def store(self, user_id: str, items: list[Any]) -> None:
        """存储多条记忆。

        Args:
            user_id: 用户 ID
            items: MemoryItem 列表
        """
        for item in items:
            await self._upsert_memory(item)
        log.debug("pg_memory_stored", user_id=user_id, count=len(items))

    async def recall(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        min_confidence: float = 0.3,
    ) -> list[Any]:
        """基于字符 n-gram 重叠的简易召回（与 in-memory 实现对齐）。

        生产环境应替换为 pgvector 向量检索。
        """
        import math
        import re

        def _tokenize(text: str) -> set[str]:
            cjk_chars = set(re.findall(r"[一-鿿]", text))
            latin_tokens = set(re.findall(r"[a-zA-Z0-9]+", text.lower()))
            return cjk_chars | latin_tokens

        query_terms = _tokenize(query)

        rows = await self.writer.fetch(
            """SELECT * FROM memories
               WHERE user_id = $1 AND status = 'active' AND confidence >= $2
               ORDER BY last_accessed DESC LIMIT 500""",
            user_id, min_confidence,
        )

        scored: list[tuple[float, int, Any]] = []
        for idx, row in enumerate(rows):
            d = _memory_from_row(row)
            item = _rebuild_memory(d)
            fact_terms = _tokenize(item.fact)
            if not fact_terms:
                continue
            overlap = len(query_terms & fact_terms) / max(len(query_terms), 1)
            score = overlap * item.confidence * (1 + math.log1p(item.access_count) * 0.1)
            if score > 0:
                scored.append((score, idx, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, _, item in scored[:top_k]]


def _rebuild_memory(d: dict[str, Any]) -> Any:
    """从 dict 重建 MemoryItem 对象。"""
    from lingxi.memory.pipeline import MemoryItem

    return MemoryItem(
        memory_id=d["memory_id"],
        user_id=d["user_id"],
        fact=d["fact"],
        entities=d.get("entities", []),
        confidence=d.get("confidence", 1.0),
        source=d.get("source", ""),
        created_at=d["created_at"],
        last_accessed=d["last_accessed"],
        access_count=d.get("access_count", 0),
        status=d.get("status", "active"),
    )


__all__ = [
    "HAS_ASYNC_PG",
    "PostgresWriter",
    "PostgresTicketStore",
    "PostgresEscalationQueue",
    "PostgresMemoryPipeline",
    "_ticket_from_row",
    "_queue_entry_from_row",
    "_memory_from_row",
]
