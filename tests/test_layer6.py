"""P0-1 持久化层回归测试 — SQLite write-through (不用造轮子, aiosqlite 写通模式)。

覆盖:
  - _SqliteWriter 后台线程创建/消费/关闭
  - PersistentTicketStore get_or_create / hydrate
  - PersistentEscalationQueue enqueue / dequeue / complete
  - PersistentMemoryPipeline store / recall / hydrate
  - 跨重启持久化 (落盘 → 重新加载 → 数据一致)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from lingxi.flow.persistence import (
    PersistentEscalationQueue,
    PersistentMemoryPipeline,
    PersistentTicketStore,
    _SqliteWriter,
    create_persistent_stores,
)
from lingxi.flow.ticket import Ticket, TicketCategory, TicketStatus

# ── fixtures ──

@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test_lingxi.db")


@pytest.fixture
def writer(db_path: str) -> Any:
    """返回 _SqliteWriter 并在测试后关闭线程 (避免 unraisable teardown warning)。"""
    w = _SqliteWriter(db_path)
    yield w
    w.close()


# ── _SqliteWriter 单元测试 ──

class TestSqliteWriter:
    def test_init_creates_tables(self, db_path: str) -> None:
        w = _SqliteWriter(db_path)
        # 等待就绪
        import time
        time.sleep(0.5)
        assert Path(db_path).exists()
        w.close()

    def test_write_and_load_roundtrip(self, db_path: str) -> None:
        w = _SqliteWriter(db_path)
        import time
        time.sleep(0.3)

        w.submit({
            "kind": "upsert_ticket",
            "data": {
                "ticket_id": "abc123",
                "chat_id": "chat1",
                "user_id": "user1",
                "channel": "qq",
                "category": "inquiry",
                "status": "intake",
                "messages_json": "[]",
                "created_at": 1000.0,
                "updated_at": 1000.0,
                "escalation_reason": "",
            },
        })

        # 等后台消费
        time.sleep(1.0)
        data = w.load_all()
        assert len(data["tickets"]) == 1
        assert data["tickets"][0]["ticket_id"] == "abc123"

        w.close()


# ── PersistentTicketStore ──

class TestPersistentTicketStore:
    @pytest.mark.asyncio
    async def test_create_and_get_or_create(self, db_path: str) -> None:
        store = await PersistentTicketStore.create(db_path)
        t = store.get_or_create("chat1", "user1", "qq")
        assert t.chat_id == "chat1"
        assert t.user_id == "user1"
        assert t.status == TicketStatus.INTAKE

        # 同 chat_id 返回同一个
        t2 = store.get_or_create("chat1", "user1", "qq")
        assert t2.ticket_id == t.ticket_id

    @pytest.mark.asyncio
    async def test_hydrate_after_restart(self, db_path: str) -> None:
        # 第一次启动 → 创建数据
        store1 = await PersistentTicketStore.create(db_path)
        t = store1.get_or_create("chat_persist", "user_x", "qq")
        t.add_message("user", "hello")
        # 等待写通
        await asyncio.sleep(1.0)

        # 第二次启动 (模拟重启) → 应从 SQLite 加载
        store2 = await PersistentTicketStore.create(db_path)
        # chat_index 已恢复, get_or_create 返回同一个
        t2 = store2.get_or_create("chat_persist", "user_x", "qq")
        assert t2.ticket_id == t.ticket_id
        assert len(t2.messages) == 1
        assert t2.messages[0].content == "hello"

    @pytest.mark.asyncio
    async def test_list_active_and_idle(self, db_path: str) -> None:
        store = await PersistentTicketStore.create(db_path)
        store.get_or_create("c1", "u1")
        store.get_or_create("c2", "u2")
        assert len(store.list_active()) == 2

        # c1 状态改为 closed
        t1 = store.get_or_create("c1", "u1", "qq")
        t1.transition(TicketStatus.PROCESSING)
        t1.transition(TicketStatus.RESOLVED)
        t1.transition(TicketStatus.CLOSED)

        active = store.list_active()
        assert len(active) == 1
        assert active[0].chat_id == "c2"


# ── PersistentEscalationQueue ──

class TestPersistentEscalationQueue:
    @pytest.mark.asyncio
    async def test_enqueue_and_claim(self, db_path: str) -> None:
        q = await PersistentEscalationQueue.create(db_path)
        ticket = Ticket(
            ticket_id="tq1", chat_id="cq1",
            category=TicketCategory.COMPLAINT,
        )
        entry = q.enqueue(ticket, "low_confidence")
        assert entry.ticket_id == "tq1"
        assert q.list_pending()[0].ticket_id == "tq1"

        # claim
        claimed = await q.dequeue("agent_1")
        assert claimed is not None
        assert claimed.ticket_id == "tq1"
        assert q.list_pending() == []

        # complete
        q.complete("tq1")
        # 已经被 complete 从内存删除

    @pytest.mark.asyncio
    async def test_hydrate_queue(self, db_path: str) -> None:
        q1 = await PersistentEscalationQueue.create(db_path)
        ticket = Ticket(ticket_id="tq_h", chat_id="ch_h", category=TicketCategory.INQUIRY)
        q1.enqueue(ticket, "test")
        await asyncio.sleep(1.0)

        q2 = await PersistentEscalationQueue.create(db_path)
        assert len(q2.list_pending()) == 1
        assert q2.list_pending()[0].ticket_id == "tq_h"


# ── PersistentMemoryPipeline ──

class TestPersistentMemoryPipeline:
    @pytest.mark.asyncio
    async def test_store_and_recall(self, db_path: str) -> None:
        mem = await PersistentMemoryPipeline.create(db_path)
        from lingxi.memory.pipeline import MemoryItem
        item = MemoryItem(
            memory_id="m1", user_id="u1",
            fact="用户购买了Pro套餐", entities=["pro"],
        )
        mem.store(item)

        result = mem.recall("u1", "购买 套餐")
        assert len(result.items) >= 1

    @pytest.mark.asyncio
    async def test_hydrate_memories(self, db_path: str) -> None:
        from lingxi.memory.pipeline import MemoryItem

        m1 = await PersistentMemoryPipeline.create(db_path)
        m1.store(MemoryItem(memory_id="m_h", user_id="u_h", fact="用户偏爱蓝色"))
        await asyncio.sleep(1.0)

        m2 = await PersistentMemoryPipeline.create(db_path)
        assert m2.total_memories >= 1
        result = m2.recall("u_h", "蓝色")
        assert len(result.items) >= 1


# ── create_persistent_stores 工厂 ──

class TestFactory:
    @pytest.mark.asyncio
    async def test_create_all_stores(self, db_path: str) -> None:
        tickets, queue, memory = await create_persistent_stores(db_path)

        # tickets
        t = tickets.get_or_create("factory_chat", "fu", "qq")
        assert t.chat_id == "factory_chat"

        # queue
        ticket_q = Ticket(ticket_id="fq1", chat_id="fc1", category=TicketCategory.COMPLAINT)
        queue.enqueue(ticket_q, "test")
        assert queue.pending_count == 1

        # memory
        from lingxi.memory.pipeline import MemoryItem
        memory.store(MemoryItem(memory_id="fm1", user_id="fu", fact="factory test"))
        assert memory.total_memories >= 1
