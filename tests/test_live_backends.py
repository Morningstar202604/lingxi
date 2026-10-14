"""PostgreSQL / Neo4j 活容器契约测试（服务不可达时整体 skip）。

验证 Sync 包装器的真实行为面对齐 in-memory 接口：
- SyncPostgresTicketStore: get_or_create / list_idle / list_escalated
- SyncPostgresEscalationQueue: enqueue → dequeue → complete / snapshot / close 幂等
- Neo4jGraphStore: add_entity / add_relation / related 双向 / 计数

🤖 AI 避坑
    - 这些测试需要 zhixi-pg / zhixi-neo4j 容器（见 README 本地开发章节），
      连不上就 skip 而不是 fail —— 契约测试 ≠ 环境测试
    - 用随机 ticket_id / user 前缀避免与其他测试数据互踩
"""
from __future__ import annotations

import uuid

import pytest


def _prefix() -> str:
    return f"t_{uuid.uuid4().hex[:10]}"


def _pg_available() -> bool:
    try:
        import asyncio

        import asyncpg

        async def _probe() -> None:
            conn = await asyncio.wait_for(
                asyncpg.connect("postgresql://zhixi:zhixi@127.0.0.1:5432/zhixi"),
                timeout=3,
            )
            await conn.close()

        asyncio.run(_probe())
        return True
    except Exception:
        return False


def _neo4j_available() -> bool:
    try:
        from neo4j import GraphDatabase

        d = GraphDatabase.driver(
            "bolt://127.0.0.1:7687", auth=("neo4j", "zhixi_neo4j")
        )
        d.verify_connectivity()
        d.close()
        return True
    except Exception:
        return False


pg_available = _pg_available()
neo4j_available = _neo4j_available()


@pytest.fixture
def pg_writer():
    """共享的 PostgresWriter（池建在 runner 循环上，与 Sync 包装器同源）。"""
    from lingxi.flow.postgres import PostgresWriter

    writer = PostgresWriter.create_on_runner(
        "postgresql://zhixi:zhixi@127.0.0.1:5432/zhixi",
        min_size=1,
        max_size=2,
    )
    writer.init_schema_on_runner()
    yield writer
    writer.close_on_runner()


@pytest.mark.skipif(
    not pg_available, reason="PostgreSQL 容器不可达（zhixi-pg 未起）"
)
class TestSyncPostgresContract:
    """Sync 包装器与 in-memory 接口对齐（活库真实读写）。"""

    def test_ticket_get_or_create_and_idle(self, pg_writer) -> None:
        from lingxi.flow.postgres import (
            PostgresTicketStore,
            SyncPostgresTicketStore,
        )

        store = SyncPostgresTicketStore(PostgresTicketStore(pg_writer))
        p = _prefix()
        t = store.get_or_create(f"{p}-chat", user_id=f"{p}-user", channel="qq")
        assert t.chat_id == f"{p}-chat"
        # 幂等：同 chat 再取是同一张单
        t2 = store.get_or_create(f"{p}-chat")
        assert t2.ticket_id == t.ticket_id

        # list_idle 语义 = "空闲超过 N 秒"，刚建的工单不该出现
        assert all(
            x.ticket_id != t.ticket_id for x in store.list_idle(3600)
        )
        # idle_seconds=0 → 阈值即此刻，刚建的工单应被列出
        assert any(
            x.ticket_id == t.ticket_id for x in store.list_idle(0)
        )
        esc = store.list_escalated()
        assert isinstance(esc, list)

    @pytest.mark.asyncio
    async def test_queue_lifecycle_and_close_idempotent(self, pg_writer) -> None:
        from lingxi.flow.postgres import (
            PostgresEscalationQueue,
            PostgresTicketStore,
            SyncPostgresEscalationQueue,
            SyncPostgresTicketStore,
        )

        tickets = PostgresTicketStore(pg_writer)
        q = SyncPostgresEscalationQueue(
            PostgresEscalationQueue(pg_writer, ticket_store=tickets)
        )
        q.attach_store(SyncPostgresTicketStore(tickets))

        p = _prefix()
        ticket = SyncPostgresTicketStore(tickets).get_or_create(
            f"{p}-chat", user_id=f"{p}-u", channel="qq"
        )
        q.enqueue(ticket, reason="contract-test")
        assert q.pending_count() >= 1

        # snapshot 必须带真实等待时长与原因（此前 hardcode 0/空串）
        snap_before = q.snapshot()
        mine = [
            e for e in snap_before["top_5_pending"]
            if e["ticket_id"] == ticket.ticket_id
        ]
        assert mine, "新入队工单应出现在 top_5_pending"
        assert mine[0]["reason"] == "contract-test"
        assert isinstance(mine[0]["wait_seconds"], int)
        assert mine[0]["wait_seconds"] >= 0
        assert snap_before["longest_wait"] >= mine[0]["wait_seconds"]

        entry = await q.dequeue("agent-x")
        assert entry is not None
        # 认领后不再 pending
        q.complete(entry.ticket_id)

        snap = q.snapshot()
        assert "pending_count" in snap and "top_5_pending" in snap

        # close 幂等：多 store 共享同一池时连续 close 不报错
        q.close()
        q.close()

    @pytest.mark.asyncio
    async def test_memory_total_is_real_count(self, pg_writer) -> None:
        """SyncPostgresMemoryPipeline.total_memories 必须是真实计数（非 0 占位）。"""
        import uuid

        from lingxi.flow.postgres import (
            PostgresMemoryPipeline,
            SyncPostgresMemoryPipeline,
        )
        from lingxi.memory.pipeline import MemoryItem

        pipe = SyncPostgresMemoryPipeline(PostgresMemoryPipeline(pg_writer))
        before = pipe.total_memories
        uid = f"{_prefix()}-u"
        pipe.store(uid, [
            MemoryItem(
                memory_id=uuid.uuid4().hex,
                user_id=uid,
                fact="契约测试记忆",
                confidence=0.9,
                source="contract",
            )
        ])
        assert pipe.total_memories == before + 1

    @pytest.mark.asyncio
    async def test_dequeue_returns_ticket_for_dashboard_claim(self, pg_writer) -> None:
        """dashboard /api/queue/claim 读 ticket_id/chat_id/category.value/reason。

        Postgres 的 dequeue 必须返回 QueueEntry（此前返回 Ticket，
        Ticket 上没有 reason，认领路由会 500）。
        """
        from lingxi.flow.postgres import (
            PostgresEscalationQueue,
            PostgresTicketStore,
            SyncPostgresEscalationQueue,
            SyncPostgresTicketStore,
        )

        tickets = PostgresTicketStore(pg_writer)
        q = SyncPostgresEscalationQueue(
            PostgresEscalationQueue(pg_writer, ticket_store=tickets)
        )
        q.attach_store(SyncPostgresTicketStore(tickets))

        p = _prefix()
        ticket = SyncPostgresTicketStore(tickets).get_or_create(
            f"{p}-c", user_id=f"{p}-u"
        )
        q.enqueue(ticket, reason="claim-contract")
        entry = await q.dequeue("seat-1")
        assert entry.ticket_id == ticket.ticket_id
        assert entry.chat_id == f"{p}-c"
        # claim 路由实际读取的字段
        assert entry.reason == "claim-contract"
        assert entry.category.value == ticket.category.value
        assert entry.claimed_by == "seat-1"
        q.complete(entry.ticket_id)


@pytest.mark.skipif(
    not neo4j_available, reason="Neo4j 容器不可达（zhixi-neo4j 未起）"
)
class TestNeo4jContract:
    """Neo4jGraphStore 契约面（与 InMemory/SQLite 的邻居查询语义对齐）。"""

    @pytest.fixture()
    def n4j(self):
        from lingxi.memory.neo4j_graph import Neo4jGraphStore

        store = Neo4jGraphStore(
            uri="bolt://127.0.0.1:7687",
            username="neo4j",
            password="zhixi_neo4j",
        )
        yield store
        store.close()

    def test_entity_relation_roundtrip(self, n4j) -> None:
        from lingxi.memory.graph_store import Entity, Relation

        p = _prefix()
        a, b = f"{p}-a", f"{p}-b"
        n4j.add_entity(Entity(entity_id=a, label="Person", properties={"name": "甲"}))
        n4j.add_relation(
            Relation(source_id=a, target_id=b, rel_type="KNOWS",
                    properties={"since": "2026"})
        )

        found = n4j.find_entity(a)
        assert found is not None
        assert found.label == "Person"
        assert found.properties.get("name") == "甲"

        # 双向邻居语义：从 b 反查也能看到 a（与 InMemoryGraphStore 一致）
        from_b = n4j.related(b, rel_type="KNOWS")
        assert any(rel.rel_type == "KNOWS" for rel, _ in from_b)
        assert n4j.entity_count >= 2
        assert n4j.relation_count >= 1

    def test_search_by_label(self, n4j) -> None:
        from lingxi.memory.graph_store import Entity

        p = _prefix()
        n4j.add_entity(
            Entity(entity_id=f"{p}-loc", label="ContractTestLoc",
                  properties={"city": "杭州"})
        )
        hits = n4j.search(label="ContractTestLoc")
        assert any(e.entity_id == f"{p}-loc" for e in hits)
