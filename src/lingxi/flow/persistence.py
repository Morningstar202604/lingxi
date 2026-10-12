"""P0-1 — SQLite 持久化层 (aiosqlite)。

🔗 工程逻辑
    智犀三大状态组件 (TicketStore / EscalationQueue / MemoryPipeline) 当前全内存态,
    重启即丢。本模块提供 SQLite 持久化版本, 写通 (write-through) 模式:
    - 所有读写操作先走内存 (保持接口零变化)
    - 变更异步写入 SQLite (后台线程 + aiosqlite)
    - 启动时从 SQLite 反序列化到内存

    使用 aiosqlite 而非 stdlib sqlite3: 与 asyncio 生态一致,
    内部用线程池执行, 但 API 是 async 的, 无阻塞风险。

    实现模式: Persistent* 继承对应的 in-memory 类, 在写操作方法上
    调用 super() 后额外调用 _persist() 落盘。内存是 truth, SQLite 是备份。

🤖 AI 避坑
    - 不要在 IM 事件回调里 await SQLite 操作 (会阻塞主循环)
    - 写通模式: 内存是 truth, SQLite 是备份 → 读永远走内存, 写异步刷盘
    - 不要手动管理线程生命周期, 用 queue.Queue + threading.Thread
    - JSON 序列化时 datetime 不可序列化 → 用 isoformat()
    - 失败降级: SQLite 写入失败只 log.error, 绝不抛异常阻塞主流程
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import threading
from typing import TYPE_CHECKING, Any

from lingxi.core import get_logger

if TYPE_CHECKING:
    from lingxi.memory.graph_store import GraphStore

log = get_logger(__name__)


# ── 后台 SQLite 写入器 ──────────────────────────────────────────────

class _SqliteWriter:
    """单线程 SQLite 写入器 (一个实例管一张 sqlite 文件)。

    内部启动一个 daemon 线程, 线程内运行独立的 asyncio 事件循环 + aiosqlite。
    外部通过 self.submit(op_dict) fire-and-forget 写入。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=5000)
        self._running = True
        self._db: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._barrier_event: threading.Event | None = None
        self._barrier_holder: dict[str, list[dict[str, Any]]] | None = None
        self._ready = threading.Event()
        # daemon 线程, 主进程退出时不阻塞
        self._thread = threading.Thread(
            target=self._run, name=f"sqlite-writer-{db_path}", daemon=True,
        )
        self._thread.start()
        # 进程退出时关闭 writer 线程, 防止 ResourceWarning (event loop 未关)
        import atexit
        atexit.register(self.close)
        # 等后台 loop 就绪
        self._ready.wait(timeout=3.0)

    def _run(self) -> None:
        """后台线程入口: 创建事件循环, 初始化 DB, 消费写入队列。"""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception:
            log.exception("sqlite_writer_crashed", db=self._db_path)
        finally:
            # 必须关闭 loop: _main() 退出后 loop 本身不会自动销毁，
            # 否则每个 writer 线程都会泄漏一个 ProactorEventLoop
            # (表现为 ResourceWarning: unclosed event loop)。
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            asyncio.set_event_loop(None)

    async def _main(self) -> None:
        """主循环: 建表 → 就绪 → 消费写入队列。"""
        import aiosqlite

        db = await aiosqlite.connect(self._db_path)
        self._db = db  # 保存连接引用 (供 barrier 读取)
        try:
            await self._init_schema(db)
            self._ready.set()
            while self._running:
                try:
                    op = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if op is None:  # 关闭信号
                    break
                if isinstance(op, str) and op == "barrier":
                    # 刷新屏障: 确保之前所有写入已提交, 然后在同一连接上读
                    result = await self._read_all_sync(db)
                    self._barrier_holder = result
                    # flush_and_read 必须在 put_nowait("barrier") 之前设好 event
                    assert self._barrier_event is not None
                    self._barrier_event.set()
                    continue
                try:
                    await self._exec(db, op)
                except Exception:
                    log.exception("sqlite_op_failed", kind=op.get("kind"))
        finally:
            await db.close()

    @staticmethod
    async def _read_all_sync(db: Any) -> dict[str, list[dict[str, Any]]]:
        """读取当前 DB 所有数据 (在同一连接上, 看到最新已提交状态)。"""
        import aiosqlite
        orig_factory = db.row_factory
        db.row_factory = aiosqlite.Row
        tickets: list[dict[str, Any]] = []
        async with db.execute("SELECT * FROM tickets") as cur:
            async for row in cur:
                tickets.append(dict(row))
        entries: list[dict[str, Any]] = []
        async with db.execute("SELECT * FROM queue_entries") as cur:
            async for row in cur:
                entries.append(dict(row))
        mems: list[dict[str, Any]] = []
        async with db.execute("SELECT * FROM memories") as cur:
            async for row in cur:
                mems.append(dict(row))
        db.row_factory = orig_factory
        return {"tickets": tickets, "queue": entries, "memories": mems}

    @staticmethod
    async def _init_schema(db: Any) -> None:
        """幂等建表。"""
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id      TEXT PRIMARY KEY,
                chat_id        TEXT NOT NULL DEFAULT '',
                user_id        TEXT NOT NULL DEFAULT '',
                channel        TEXT NOT NULL DEFAULT '',
                category       TEXT NOT NULL DEFAULT 'other',
                status         TEXT NOT NULL DEFAULT 'intake',
                messages_json  TEXT NOT NULL DEFAULT '[]',
                created_at     REAL NOT NULL DEFAULT 0,
                updated_at     REAL NOT NULL DEFAULT 0,
                escalation_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_tickets_chat ON tickets(chat_id);
            CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

            CREATE TABLE IF NOT EXISTS queue_entries (
                ticket_id   TEXT PRIMARY KEY,
                chat_id     TEXT NOT NULL DEFAULT '',
                category    TEXT NOT NULL DEFAULT 'other',
                reason      TEXT NOT NULL DEFAULT '',
                enqueued_at REAL NOT NULL DEFAULT 0,
                claimed_by  TEXT,
                claimed_at  REAL
            );

            CREATE TABLE IF NOT EXISTS memories (
                memory_id     TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL DEFAULT '',
                fact          TEXT NOT NULL DEFAULT '',
                entities_json TEXT NOT NULL DEFAULT '[]',
                confidence    REAL NOT NULL DEFAULT 1.0,
                source        TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL DEFAULT '',
                last_accessed TEXT NOT NULL DEFAULT '',
                access_count  INTEGER NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'active'
            );
            CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
            CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
            """
        )
        await db.commit()

    @staticmethod
    async def _exec(db: Any, op: dict[str, Any]) -> None:
        """执行一条写入操作。"""
        kind = op["kind"]

        if kind == "upsert_ticket":
            d = op["data"]
            await db.execute(
                "INSERT OR REPLACE INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?)",
                (d["ticket_id"], d["chat_id"], d["user_id"], d["channel"],
                 d["category"], d["status"], d["messages_json"],
                 d["created_at"], d["updated_at"], d["escalation_reason"]),
            )
            await db.commit()

        elif kind == "delete_ticket":
            await db.execute("DELETE FROM tickets WHERE ticket_id=?", (op["ticket_id"],))
            await db.commit()

        elif kind == "upsert_queue_entry":
            d = op["data"]
            await db.execute(
                "INSERT OR REPLACE INTO queue_entries VALUES (?,?,?,?,?,?,?)",
                (d["ticket_id"], d["chat_id"], d["category"], d["reason"],
                 d["enqueued_at"], d.get("claimed_by"), d.get("claimed_at")),
            )
            await db.commit()

        elif kind == "delete_queue_entry":
            await db.execute("DELETE FROM queue_entries WHERE ticket_id=?", (op["ticket_id"],))
            await db.commit()

        elif kind == "upsert_memory":
            d = op["data"]
            await db.execute(
                "INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
                (d["memory_id"], d["user_id"], d["fact"], d["entities_json"],
                 d["confidence"], d["source"], d["created_at"],
                 d["last_accessed"], d["access_count"], d["status"]),
            )
            await db.commit()

        elif kind == "delete_memory":
            await db.execute("DELETE FROM memories WHERE memory_id=?", (op["memory_id"],))
            await db.commit()

    def submit(self, op: dict[str, Any]) -> None:
        """提交写入操作 (非阻塞, 队列满则丢弃 + 告警)。"""
        if not self._running:
            return
        try:
            self._queue.put_nowait(op)
        except queue.Full:
            log.warning("sqlite_queue_full", kind=op.get("kind"))

    def load_all(self) -> dict[str, list[dict[str, Any]]]:
        """启动时调用: 同步加载所有持久化数据。

        使用 barrier 机制 — 所有先前的写入消费完后, 在同一连接上读取,
        确保数据一致性 (无需独立二次连接, 无事务隔离问题)。
        """
        barrier_event = threading.Event()
        self._barrier_event = barrier_event
        self._barrier_holder = None

        # 提交 barrier 标记 → 后台线程消费完所有写入后在此返回
        try:
            self._queue.put_nowait("barrier")
        except queue.Full:
            log.warning("sqlite_barrier_queue_full", db=self._db_path)
            return {"tickets": [], "queue": [], "memories": []}

        if not barrier_event.wait(timeout=5.0):
            log.warning("sqlite_barrier_timeout", db=self._db_path)
            return {"tickets": [], "queue": [], "memories": []}

        result = self._barrier_holder
        return result if result is not None else {"tickets": [], "queue": [], "memories": []}

    def close(self) -> None:
        self._running = False
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=3.0)


# ── 序列化辅助 ──────────────────────────────────────────────────────

def _ticket_to_row(ticket: Any) -> dict[str, Any]:
    """Ticket → dict (可存入 DB 行的字段)。"""
    return {
        "ticket_id": ticket.ticket_id,
        "chat_id": ticket.chat_id,
        "user_id": ticket.user_id,
        "channel": ticket.channel,
        "category": ticket.category.value if hasattr(ticket.category, "value") else str(ticket.category),
        "status": ticket.status.value if hasattr(ticket.status, "value") else str(ticket.status),
        "messages_json": json.dumps([
            {"role": m.role, "content": m.content, "timestamp": m.timestamp,
             "confidence": m.confidence, "escalated": m.escalated}
            for m in ticket.messages
        ], ensure_ascii=False),
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "escalation_reason": ticket.escalation_reason,
    }


def _row_to_ticket(row: dict[str, Any]) -> Any:
    """Dict (DB row) → Ticket 对象。"""
    from lingxi.flow.ticket import Ticket, TicketCategory, TicketMessage, TicketStatus

    ticket = Ticket(
        ticket_id=row["ticket_id"],
        chat_id=row.get("chat_id", ""),
        user_id=row.get("user_id", ""),
        channel=row.get("channel", ""),
        category=TicketCategory(row.get("category", "other")),
        status=TicketStatus(row.get("status", "intake")),
        created_at=row.get("created_at", 0.0),
        updated_at=row.get("updated_at", 0.0),
        escalation_reason=row.get("escalation_reason", ""),
    )
    for m in json.loads(row.get("messages_json", "[]") or "[]"):
        ticket.messages.append(TicketMessage(
            role=m.get("role", "user"),
            content=m.get("content", ""),
            timestamp=m.get("timestamp", 0.0),
            confidence=m.get("confidence", 0.0),
            escalated=m.get("escalated", False),
        ))
    return ticket


def _queue_entry_to_row(entry: Any) -> dict[str, Any]:
    """QueueEntry → dict。"""
    return {
        "ticket_id": entry.ticket_id,
        "chat_id": entry.chat_id,
        "category": entry.category.value if hasattr(entry.category, "value") else str(entry.category),
        "reason": entry.reason,
        "enqueued_at": entry.enqueued_at,
        "claimed_by": entry.claimed_by,
        "claimed_at": entry.claimed_at,
    }


def _row_to_queue_entry(row: dict[str, Any]) -> Any:
    """Dict → QueueEntry。"""
    from lingxi.flow.queue import QueueEntry
    from lingxi.flow.ticket import TicketCategory
    return QueueEntry(
        ticket_id=row["ticket_id"],
        chat_id=row.get("chat_id", ""),
        category=TicketCategory(row.get("category", "other")),
        reason=row.get("reason", ""),
        enqueued_at=row.get("enqueued_at", 0.0),
        claimed_by=row.get("claimed_by"),
        claimed_at=row.get("claimed_at"),
    )


def _memory_to_row(item: Any) -> dict[str, Any]:
    """MemoryItem → dict。"""
    return {
        "memory_id": item.memory_id,
        "user_id": item.user_id,
        "fact": item.fact,
        "entities_json": json.dumps(item.entities, ensure_ascii=False),
        "confidence": item.confidence,
        "source": item.source,
        "created_at": item.created_at.isoformat(),
        "last_accessed": item.last_accessed.isoformat(),
        "access_count": item.access_count,
        "status": item.status,
    }


def _row_to_memory(row: dict[str, Any]) -> Any:
    """Dict → MemoryItem。"""
    from datetime import datetime

    from lingxi.memory.pipeline import MemoryItem

    def _parse_dt(val: str | None) -> datetime:
        if not val:
            return datetime.now()
        try:
            return datetime.fromisoformat(val)
        except (ValueError, TypeError):
            return datetime.now()

    return MemoryItem(
        memory_id=row["memory_id"],
        user_id=row.get("user_id", ""),
        fact=row.get("fact", ""),
        entities=json.loads(row.get("entities_json", "[]") or "[]"),
        confidence=row.get("confidence", 1.0),
        source=row.get("source", ""),
        created_at=_parse_dt(row.get("created_at")),
        last_accessed=_parse_dt(row.get("last_accessed")),
        access_count=row.get("access_count", 0),
        status=row.get("status", "active"),
    )


# ── PersistentTicketStore ──────────────────────────────────────────


class _WriteThroughTicket:
    """Ticket 的写通包装 — 可变操作 (add_message/transition) 自动触发持久化。

    🔗 工程逻辑
        Ticket 是 dataclass, 直接子类化而非 monkey-patch,
        避免污染 Ticket 基类的纯洁性 (in-memory 场景不需要回调)。
    """

    def __init__(self, ticket: Any, on_write: Any) -> None:
        # 用 __dict__ 代理实现透明包装 (避免重写 dataclass __init__)
        self.__dict__["_ticket"] = ticket
        self.__dict__["_on_write"] = on_write
        # 同步所有字段
        self.__dict__.update(ticket.__dict__)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_ticket"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.__dict__["_ticket"], name, value)
        self.__dict__[name] = value

    def add_message(self, *args: Any, **kwargs: Any) -> Any:
        result = self.__dict__["_ticket"].add_message(*args, **kwargs)
        self._on_write(self)
        return result

    def transition(self, *args: Any, **kwargs: Any) -> Any:
        result = self.__dict__["_ticket"].transition(*args, **kwargs)
        self._on_write(self)
        return result

    def __repr__(self) -> str:
        return repr(self.__dict__["_ticket"])

    # 透明代理 dataclass 比较
    def __eq__(self, other: Any) -> bool:
        if isinstance(other, _WriteThroughTicket):
            return bool(self.__dict__["_ticket"] == other.__dict__["_ticket"])
        return bool(self.__dict__["_ticket"] == other)

    def __hash__(self) -> int:
        return hash(self.__dict__["_ticket"].ticket_id)

    def __bool__(self) -> bool:
        return bool(self.__dict__["_ticket"])


class PersistentTicketStore:
    """TicketStore 的 SQLite 写通版本。

    直接实现 TicketStore 的完整接口 (不依赖继承, 避免覆盖方法签名问题),
    但所有写操作额外提交到后台 SQLite writer。

    用法:
        store = await PersistentTicketStore.create("data/tickets.db")
    """

    def __init__(self, writer: _SqliteWriter) -> None:
        self._writer = writer
        self._tickets: dict[str, Any] = {}
        self._chat_index: dict[str, str] = {}

    @classmethod
    async def create(cls, db_path: str) -> PersistentTicketStore:
        """工厂方法: 创建实例, 建表, 加载历史数据。"""
        writer = _SqliteWriter(db_path)
        inst = cls(writer)
        inst._hydrate()
        return inst

    def _hydrate(self) -> None:
        """从 SQLite 加载现有数据到内存 (启动时调用, 同步等待)。"""
        data = self._writer.load_all()
        for row in data.get("tickets", []):
            try:
                ticket = _row_to_ticket(row)
                self._tickets[ticket.ticket_id] = ticket
                if ticket.chat_id:
                    self._chat_index[ticket.chat_id] = ticket.ticket_id
            except Exception:
                log.warning("hydrate_ticket_failed", ticket_id=row.get("ticket_id"))

    # ── TicketStore 兼容接口 ──

    def get_or_create(self, chat_id: str, user_id: str = "", channel: str = "") -> Any:
        from lingxi.flow.ticket import Ticket, TicketStatus

        if chat_id in self._chat_index:
            existing = self._tickets.get(self._chat_index[chat_id])
            if existing and existing.status not in (TicketStatus.CLOSED, TicketStatus.RESOLVED):
                # 从 DB 加载的 ticket 已经纯化 (普通 Ticket), 加写通包装
                wtt = _WriteThroughTicket(existing, lambda t: self._writer.submit(
                    {"kind": "upsert_ticket", "data": _ticket_to_row(t.__dict__["_ticket"])},
                ))
                return wtt

        ticket = Ticket(chat_id=chat_id, user_id=user_id, channel=channel)
        wtt = _WriteThroughTicket(ticket, lambda t: self._writer.submit(
            {"kind": "upsert_ticket", "data": _ticket_to_row(t.__dict__["_ticket"])},
        ))
        self._tickets[ticket.ticket_id] = ticket
        self._chat_index[chat_id] = ticket.ticket_id
        self._writer.submit({"kind": "upsert_ticket", "data": _ticket_to_row(ticket)})
        return wtt

    def get(self, ticket_id: str) -> Any:
        return self._tickets.get(ticket_id)

    def list_active(self) -> list[Any]:
        from lingxi.flow.ticket import TicketStatus
        return [t for t in self._tickets.values()
                if t.status not in (TicketStatus.CLOSED, TicketStatus.RESOLVED)]

    def list_escalated(self) -> list[Any]:
        from lingxi.flow.ticket import TicketStatus
        return [t for t in self._tickets.values() if t.status == TicketStatus.ESCALATED]

    def list_idle(self, idle_seconds: float = 1800) -> list[Any]:
        return [t for t in self._tickets.values() if t.can_auto_close(idle_seconds)]


# ── PersistentEscalationQueue ───────────────────────────────────────

class PersistentEscalationQueue:
    """EscalationQueue 的 SQLite 写通版本。

    内部持有 EscalationQueue 实例处理 in-memory 逻辑和 pub/sub,
    写操作 (enqueue/dequeue/complete) 额外落盘。
    """

    def __init__(self, writer: _SqliteWriter) -> None:
        from lingxi.flow.queue import EscalationQueue
        self._writer = writer
        self._inner = EscalationQueue()

    @classmethod
    async def create(cls, db_path: str) -> PersistentEscalationQueue:
        writer = _SqliteWriter(db_path)
        inst = cls(writer)
        inst._hydrate()
        return inst

    def _hydrate(self) -> None:
        data = self._writer.load_all()
        for row in data.get("queue", []):
            try:
                entry = _row_to_queue_entry(row)
                self._inner._entries[entry.ticket_id] = entry
            except Exception:
                log.warning("hydrate_queue_failed", ticket_id=row.get("ticket_id"))

    # ── EscalationQueue 兼容接口 ──

    def subscribe(self) -> Any:
        return self._inner.subscribe()

    def unsubscribe(self, q: Any) -> None:
        self._inner.unsubscribe(q)

    def enqueue(self, ticket: Any, reason: str) -> Any:
        entry = self._inner.enqueue(ticket, reason)
        self._writer.submit({"kind": "upsert_queue_entry", "data": _queue_entry_to_row(entry)})
        return entry

    async def dequeue(self, agent_id: str) -> Any:
        entry = await self._inner.dequeue(agent_id)
        if entry is not None:
            self._writer.submit({"kind": "upsert_queue_entry", "data": _queue_entry_to_row(entry)})
        return entry

    def complete(self, ticket_id: str) -> None:
        self._inner.complete(ticket_id)
        self._writer.submit({"kind": "delete_queue_entry", "ticket_id": ticket_id})

    def list_pending(self) -> list[Any]:
        return self._inner.list_pending()

    def list_claimed(self, agent_id: str | None = None) -> list[Any]:
        return self._inner.list_claimed(agent_id)

    @property
    def pending_count(self) -> int:
        return self._inner.pending_count

    def snapshot(self) -> dict[str, Any]:
        return self._inner.snapshot()


# ── PersistentMemoryPipeline ────────────────────────────────────────

class PersistentMemoryPipeline:
    """MemoryPipeline 的 SQLite 写通版本。

    内部持有 MemoryPipeline 实例, 写操作 (store/update/contradict/forget)
    额外落盘。encode_and_store 是 async 方法, 可直接 await SQLite 写入。

    🔗 graph 必须透传：此前 `MemoryPipeline()` 无参构造，
    持久化模式下实体图谱整体失效（编码不建边、
    `recall_hybrid` 的图谱分支恒空、MCP `graph://stats`
    资源因 `getattr(memory, "graph")` 为 None 而不注册）。
    """

    def __init__(
        self,
        writer: _SqliteWriter,
        graph: GraphStore | None = None,
    ) -> None:
        from lingxi.memory.pipeline import MemoryPipeline
        self._writer = writer
        self._inner = MemoryPipeline(graph=graph)

    @property
    def graph(self) -> GraphStore | None:
        return self._inner.graph

    @classmethod
    async def create(
        cls,
        db_path: str,
        graph: GraphStore | None = None,
    ) -> PersistentMemoryPipeline:
        writer = _SqliteWriter(db_path)
        inst = cls(writer, graph=graph)
        inst._hydrate()
        return inst

    def _hydrate(self) -> None:
        data = self._writer.load_all()
        for row in data.get("memories", []):
            try:
                item = _row_to_memory(row)
                self._inner._memories[item.memory_id] = item
            except Exception:
                log.warning("hydrate_memory_failed", memory_id=row.get("memory_id"))

    # ── MemoryPipeline 兼容接口 ──

    def store(self, item: Any) -> None:
        self._inner.store(item)
        self._writer.submit({"kind": "upsert_memory", "data": _memory_to_row(item)})

    def recall(self, user_id: str, query: str, top_k: int = 5, min_confidence: float = 0.3) -> Any:
        return self._inner.recall(user_id, query, top_k, min_confidence)

    def update_fact(self, memory_id: str, new_fact: str, new_confidence: float) -> None:
        self._inner.update_fact(memory_id, new_fact, new_confidence)
        item = self._inner._memories.get(memory_id)
        if item:
            self._writer.submit({"kind": "upsert_memory", "data": _memory_to_row(item)})

    def contradict(self, memory_id: str, evidence: str) -> None:
        self._inner.contradict(memory_id, evidence)
        item = self._inner._memories.get(memory_id)
        if item:
            self._writer.submit({"kind": "upsert_memory", "data": _memory_to_row(item)})

    def forget(self, user_id: str | None = None, days: int = 30) -> int:
        count = self._inner.forget(user_id, days)
        # forget 是批量操作, 重新提交所有被影响的记忆 (简单实现)
        for item in self._inner._memories.values():
            if item.status != "active" or count > 0:
                self._writer.submit({"kind": "upsert_memory", "data": _memory_to_row(item)})
        return count

    def list_user_memories(self, user_id: str) -> list[Any]:
        return self._inner.list_user_memories(user_id)

    async def encode_and_store(self, user_id: str, text: str, *, max_facts: int = 3, source: str = "") -> list[Any]:
        items = await self._inner.encode_and_store(user_id, text, max_facts=max_facts, source=source)
        for item in items:
            self._writer.submit({"kind": "upsert_memory", "data": _memory_to_row(item)})
        return items

    @property
    def total_memories(self) -> int:
        return self._inner.total_memories

    def recall_hybrid(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        min_confidence: float = 0.3,
    ) -> Any:
        return self._inner.recall_hybrid(user_id, query, top_k, min_confidence)


# ── 全局单例 writer (三表共用一个 sqlite 文件) ───────────────────

_default_writer: _SqliteWriter | None = None


def get_or_create_writer(db_path: str) -> _SqliteWriter:
    """获取或创建全局 SQLite writer 实例。"""
    global _default_writer
    if _default_writer is None:
        _default_writer = _SqliteWriter(db_path)
    return _default_writer


async def create_persistent_stores(
    db_path: str,
    graph: GraphStore | None = None,
) -> tuple[PersistentTicketStore, PersistentEscalationQueue, PersistentMemoryPipeline]:
    """创建三个持久化存储, 共享同一个 SQLite writer 和 db 文件。

    使用场景:
        tickets, queue, memory = await create_persistent_stores("data/lingxi.db")

    graph: 实体图谱存储。传入则记忆编码同步建边、
    recall_hybrid 的图谱分支生效；不传则记忆链路照常
    工作但图谱能力退化为纯字面匹配。
    """
    writer = get_or_create_writer(db_path)
    tickets = PersistentTicketStore(writer)
    esc_queue = PersistentEscalationQueue(writer)
    memory = PersistentMemoryPipeline(writer, graph=graph)
    # 共享 writer 已经建好表, 直接 hydrate
    tickets._hydrate()
    esc_queue._hydrate()
    memory._hydrate()
    return tickets, esc_queue, memory


__all__ = [
    "PersistentTicketStore",
    "PersistentEscalationQueue",
    "PersistentMemoryPipeline",
    "create_persistent_stores",
]
