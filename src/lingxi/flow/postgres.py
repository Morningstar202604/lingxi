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

import asyncio
import contextlib
import json
import threading
import time
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

    @classmethod
    def create_on_runner(
        cls,
        dsn: str,
        min_size: int = 2,
        max_size: int = 10,
    ) -> PostgresWriter:
        """在后台 runner 事件循环上创建连接池。

        asyncpg 的连接绑定在**创建它的那个事件循环**上。同步包装器
        （SyncPostgres*）把每个协程都扔到 runner 的独立后台循环里执行，
        因此池必须也在那个循环上创建；否则每个调用都会撞
        ``got Future ... attached to a different loop``（Engine 里
        `asyncio.run` 建池、另一个循环查库，必然触发）。
        """
        writer: PostgresWriter = _get_runner().run(
            cls.create(dsn, min_size=min_size, max_size=max_size)
        )
        return writer

    def init_schema_on_runner(self) -> None:
        """在 runner 循环上建表（与 create_on_runner 配对）。"""
        _get_runner().run(self.init_schema())

    async def close(self) -> None:
        """关闭连接池（可重复调用，多 store 共享同一池时幂等）。"""
        if self._pool is None:
            return
        pool, self._pool = self._pool, None
        await pool.close()
        log.info("postgres_pool_closed")

    def close_on_runner(self) -> None:
        """在 runner 循环上关池（池绑定在该循环，不能从别的循环 close）。"""
        _get_runner().run(self.close())

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

    async def list_escalated(self, limit: int = 100) -> list[Any]:
        """列出 escalated 状态的工单（等待人工处理）。"""
        rows = await self.writer.fetch(
            """SELECT * FROM tickets
               WHERE status = 'escalated'
               ORDER BY updated_at DESC LIMIT $1""",
            limit,
        )
        return [_rebuild_ticket(_ticket_from_row(r)) for r in rows]

    async def get_or_create(self, chat_id: str, user_id: str = "", channel: str = "") -> Any:
        """获取该 chat 的活跃工单，没有则创建（对齐 TicketStore.get_or_create）。"""
        from lingxi.flow.ticket import TicketStatus

        row = await self.writer.fetchrow(
            "SELECT ticket_id FROM tickets "
            "WHERE chat_id = $1 AND status NOT IN ($2, $3) LIMIT 1",
            chat_id,
            TicketStatus.CLOSED.value,
            TicketStatus.RESOLVED.value,
        )
        if row:
            ticket = await self.get(row["ticket_id"])
            if ticket is not None:
                return ticket
        return await self.create(chat_id, user_id, channel)

    async def list_idle(self, idle_seconds: float = 1800, limit: int = 100) -> list[Any]:
        """列出超过 idle_seconds 未更新的活跃工单（自动关闭扫描用）。

        对齐 TicketStore.list_idle(idle_seconds) 的语义。
        """
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(seconds=idle_seconds)
        rows = await self.writer.fetch(
            """SELECT * FROM tickets
               WHERE status NOT IN ('closed', 'resolved')
                 AND updated_at < $1
               ORDER BY updated_at ASC LIMIT $2""",
            cutoff,
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
        # In-process pub/sub for SSE (same as EscalationQueue)
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_subs = 64

    def attach_store(self, store: PostgresTicketStore) -> None:
        """关联 ticket store（用于 dequeue 时查完整工单）。"""
        self._ticket_store = store

    # -- Pub/Sub (SSE realtime) --
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """订阅队列事件, 返回一个 asyncio.Queue 作为事件流。"""
        import asyncio
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        if len(self._subs) >= self._max_subs:
            log.warning("escalation_subscribers_full", max=self._max_subs)
            return q
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        """广播事件到所有订阅者。"""
        import time
        msg = {"event": event_type, "data": data, "ts": time.time()}
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self._subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subs.discard(q)

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
        self._publish("escalated", {
            "ticket_id": ticket.ticket_id,
            "chat_id": ticket.chat_id,
            "category": ticket.category.value,
            "reason": reason,
        })

    async def dequeue(self, agent_id: str) -> Any:
        """原子出队 — 认领最早的未认领条目并删除，返回 QueueEntry。

        与其他两套后端同语义（in-memory / SQLite 版的 dequeue 都返回
        QueueEntry）。此前返回 Ticket：dashboard 的 claim 路由读
        entry.reason / entry.category，而 Ticket 只有 escalation_reason，
        用 Postgres 后端认领必 500。
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
                       RETURNING ticket_id, chat_id, category, reason, enqueued_at"""
            )
        if not row:
            return None

        from lingxi.flow.queue import QueueEntry
        from lingxi.flow.ticket import TicketCategory

        try:
            category = TicketCategory(row["category"])
        except ValueError:
            category = TicketCategory.OTHER

        entry = QueueEntry(
            ticket_id=row["ticket_id"],
            chat_id=row["chat_id"],
            category=category,
            reason=row["reason"],
            enqueued_at=_parse_pg_ts(row["enqueued_at"]),
            claimed_by=agent_id,
            claimed_at=time.time(),
        )
        log.debug("pg_queue_dequeued", ticket_id=entry.ticket_id, agent_id=agent_id)
        self._publish("claimed", {
            "ticket_id": entry.ticket_id,
            "agent_id": agent_id,
        })
        return entry

    async def complete(self, ticket_id: str) -> None:
        """从队列移除（已解决或关闭）。"""
        await self.writer.execute(
            "DELETE FROM queue_entries WHERE ticket_id = $1", ticket_id
        )
        log.debug("pg_queue_completed", ticket_id=ticket_id)
        self._publish("completed", {
            "ticket_id": ticket_id,
        })

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


# ── 同步包装器：在后台事件循环线程中运行 async 方法 ─────────────────────

class _AsyncRunner:
    """后台事件循环线程，用于同步调用 async 方法。"""

    _thread: threading.Thread | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _stopped: threading.Event

    def __init__(self) -> None:
        self._stopped = threading.Event()
        self._start()

    def _start(self) -> None:
        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._stopped.clear()
            loop.run_forever()

        self._thread = threading.Thread(target=_run_loop, name="pg-async-runner", daemon=True)
        self._thread.start()
        # 等待 loop 就绪
        while self._loop is None:
            time.sleep(0.001)

    def run(self, coro: Any) -> Any:
        """同步运行 coroutine，返回结果或抛出异常。"""
        if self._loop is None or self._stopped.is_set():
            raise RuntimeError("Async runner not started or stopped")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def stop(self) -> None:
        if self._loop and not self._stopped.is_set():
            self._stopped.set()
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=2.0)


_async_runner: _AsyncRunner | None = None


def _get_runner() -> _AsyncRunner:
    global _async_runner
    if _async_runner is None:
        _async_runner = _AsyncRunner()
    return _async_runner


class SyncPostgresTicketStore:
    """PostgresTicketStore 的同步包装器。"""

    def __init__(self, async_store: PostgresTicketStore) -> None:
        self._async = async_store
        self._runner = _get_runner()

    def create(self, chat_id: str, user_id: str = "", channel: str = "") -> Any:
        return self._runner.run(self._async.create(chat_id, user_id, channel))

    def get(self, ticket_id: str) -> Any:
        return self._runner.run(self._async.get(ticket_id))

    def save(self, ticket: Any) -> None:
        self._runner.run(self._async.save(ticket))

    def list_active(self, limit: int = 100) -> list[Any]:
        return self._runner.run(self._async.list_active(limit))  # type: ignore[no-any-return]

    def list_idle(self, idle_seconds: float = 1800, limit: int = 100) -> list[Any]:
        """对齐 TicketStore.list_idle(idle_seconds)：按空闲时长列。"""
        return self._runner.run(self._async.list_idle(idle_seconds, limit))  # type: ignore[no-any-return]

    def get_or_create(self, chat_id: str, user_id: str = "", channel: str = "") -> Any:
        return self._runner.run(self._async.get_or_create(chat_id, user_id, channel))

    def list_escalated(self) -> list[Any]:
        return self._runner.run(self._async.list_escalated())  # type: ignore[no-any-return]

    def close(self) -> None:
        """关闭共享连接池（池绑定在 runner 循环，必须在该循环上关）。"""
        self._async.writer.close_on_runner()


class SyncPostgresEscalationQueue:
    """PostgresEscalationQueue 的同步包装器。"""

    def __init__(self, async_queue: PostgresEscalationQueue) -> None:
        self._async = async_queue
        self._runner = _get_runner()

    def attach_store(self, store: SyncPostgresTicketStore) -> None:
        # 传入的 store 是同步包装器，需要解包出 async store
        self._async.attach_store(store._async)

    def enqueue(self, ticket: Any, *, reason: str = "") -> None:
        self._runner.run(self._async.enqueue(ticket, reason=reason))

    async def dequeue(self, agent_id: str) -> Any:
        """与 InMemory/Persistent 队列同签名（async）：dashboard 走 `await`。

        实际查询在 runner 后台循环执行（池绑定在那里），用 to_thread 包一层
        以免阻塞 dashboard 所在的事件循环。
        """
        return await asyncio.to_thread(self._dequeue_sync, agent_id)

    def _dequeue_sync(self, agent_id: str) -> Any:
        return self._runner.run(self._async.dequeue(agent_id))

    def complete(self, ticket_id: str) -> None:
        self._runner.run(self._async.complete(ticket_id))

    # -- Pub/Sub (SSE realtime) --

    def subscribe(self) -> Any:
        """订阅队列事件, 返回一个 asyncio.Queue 作为事件流。"""
        return self._runner.run(self._async.subscribe())

    def unsubscribe(self, q: Any) -> None:
        # unsubscribe returns None, discard the result
        self._runner.run(self._async.unsubscribe(q))  # type: ignore[func-returns-value]

    def list_pending(self, limit: int = 100) -> list[str]:
        return self._runner.run(self._async.list_pending(limit))  # type: ignore[no-any-return]

    def list_claimed(self, agent_id: str | None = None, limit: int = 100) -> list[str]:
        return self._runner.run(self._async.list_claimed(agent_id, limit))  # type: ignore[no-any-return]

    def pending_count(self) -> int:
        return self._runner.run(self._async.pending_count())  # type: ignore[no-any-return]

    def size(self) -> int:
        return self.pending_count()

    def snapshot(self) -> dict[str, Any]:
        """Dashboard /api/queue 与 /api/dashboard/summary 依赖此方法。"""
        pending = [
            {
                "ticket_id": t,
                "category": "",
                "reason": "",
                "wait_seconds": 0,
            }
            for t in self.list_pending()
        ]
        claimed = self.list_claimed()
        return {
            "pending_count": len(pending),
            "claimed_count": len(claimed),
            "longest_wait": 0,
            "top_5_pending": pending[:5],
        }

    def close(self) -> None:
        """关闭底层连接池（进程退出时调用，多 store 共享同一池时幂等）。"""
        self._async.writer.close_on_runner()


class SyncPostgresMemoryPipeline:
    """PostgresMemoryPipeline 的同步包装器，含 recall_hybrid。"""

    writer: PostgresWriter

    def __init__(self, async_pipe: PostgresMemoryPipeline, graph: Any = None) -> None:
        self._async = async_pipe
        self.writer = async_pipe.writer
        self._graph = graph
        self._runner = _get_runner()

    # -- sync wrappers for async methods --

    def store(self, user_id: str, items: list[Any]) -> None:
        self._runner.run(self._async.store(user_id, items))

    def recall(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        min_confidence: float = 0.3,
    ) -> list[Any]:
        return self._runner.run(self._async.recall(user_id, query, top_k, min_confidence))  # type: ignore[no-any-return]

    # -- recall_hybrid: 字面召回 + 图谱召回（去重后按原顺序合并） --

    def recall_hybrid(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        min_confidence: float = 0.3,
    ) -> Any:
        """字面召回 + 图谱召回（去重后按原顺序合并）。"""
        from lingxi.memory.pipeline import MemoryRecallResult

        # 1. 字面召回
        literal_items = self.recall(user_id, query, top_k, min_confidence)

        # 2. 图谱召回（若有 graph）
        graph_items: list[Any] = []
        if self._graph is not None:
            with contextlib.suppress(Exception):
                graph_items = self._runner.run(
                    self._graph.recall_related(user_id, query, top_k)
                )

        # 3. 去重合并：按 memory_id 去重，保持首次出现顺序
        seen: set[str] = set()
        merged: list[Any] = []
        for item in literal_items + graph_items:
            mid = getattr(item, "memory_id", None)
            if mid and mid not in seen:
                seen.add(mid)
                merged.append(item)
        return MemoryRecallResult(
            items=merged[:top_k],
            total_candidates=len(literal_items) + len(graph_items),
        )

    # -- 透传其它同步接口（如有）--

    @property
    def graph(self) -> Any:
        return self._graph

    @property
    def total_memories(self) -> int:
        # 无法同步获取，返回 0 占位
        return 0


# ── 导出 ────────────────────────────────────────────────────────────────

__all__ = [
    "HAS_ASYNC_PG",
    "PostgresWriter",
    "PostgresTicketStore",
    "PostgresEscalationQueue",
    "PostgresMemoryPipeline",
    "SyncPostgresTicketStore",
    "SyncPostgresEscalationQueue",
    "SyncPostgresMemoryPipeline",
    "_ticket_from_row",
    "_queue_entry_from_row",
    "_memory_from_row",
]
