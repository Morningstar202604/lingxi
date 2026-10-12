"""SQLite 图谱持久化的行为锁。

🔗 工程逻辑
    `InMemoryGraphStore` 重启即丢 —— 记忆管线每次重启从零建图，
    「长期记忆 + 实体图谱」退化成单次会话记忆。SQLiteGraphStore 复用仓库
    已有的 SQLite 依赖（不新增引入成本），随 data_dir 走。

    重点锁两条：
    1. `related()` 双向（从实体反查记忆是图谱的主要用途）
    2. 重新打开 = 重启，数据仍在
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from lingxi.memory.graph_store import (
    Entity,
    InMemoryGraphStore,
    Relation,
    SQLiteGraphStore,
)
from lingxi.memory.pipeline import MemoryItem, MemoryPipeline


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "graph.db")


def _seed(graph: Any) -> None:
    graph.add_entity(Entity(entity_id="m1", label="Memory", properties={"n": 1}))
    graph.add_entity(Entity(entity_id="套餐", label="Concept"))
    graph.add_relation(Relation(source_id="m1", target_id="套餐", rel_type="mentions"))


class TestSQLiteGraphStore:
    def test_write_and_read(self, db_path: str) -> None:
        g = SQLiteGraphStore(db_path)
        _seed(g)
        assert g.entity_count == 2
        assert g.relation_count == 1
        found = g.find_entity("m1")
        assert found is not None
        assert found.properties["n"] == 1
        g.close()

    def test_related_is_bidirectional(self, db_path: str) -> None:
        """从实体侧反查记忆 —— 图谱最主要的用法，不能只通正向。"""
        g = SQLiteGraphStore(db_path)
        _seed(g)
        forward = g.related("m1", rel_type="mentions")
        backward = g.related("套餐", rel_type="mentions")
        assert len(forward) == 1
        assert len(backward) == 1
        assert forward[0][1].entity_id == "套餐"
        assert backward[0][1].entity_id == "m1"
        g.close()

    def test_rel_type_filter(self, db_path: str) -> None:
        g = SQLiteGraphStore(db_path)
        _seed(g)
        assert g.related("套餐", rel_type="mentions")
        assert not g.related("套餐", rel_type="BOUGHT")
        g.close()

    def test_search_by_label_and_props(self, db_path: str) -> None:
        g = SQLiteGraphStore(db_path)
        g.add_entity(Entity(entity_id="p1", label="Person", properties={"city": "北京"}))
        g.add_entity(Entity(entity_id="p2", label="Person", properties={"city": "上海"}))
        g.add_entity(Entity(entity_id="l1", label="Location"))
        assert len(g.search(label="Person")) == 2
        assert [e.entity_id for e in g.search(label="Person", city="北京")] == ["p1"]
        g.close()

    def test_survives_reopen(self, db_path: str) -> None:
        """重启不丢 —— 这是换 SQLite 的全部意义。"""
        g = SQLiteGraphStore(db_path)
        _seed(g)
        g.close()

        g2 = SQLiteGraphStore(db_path)
        assert g2.entity_count == 2
        assert g2.relation_count == 1
        assert [e.entity_id for _, e in g2.related("套餐", rel_type="mentions")] == ["m1"]
        g2.close()

    def test_upsert_is_idempotent(self, db_path: str) -> None:
        """记忆重放会重复写同一实体/关系，不能炸也不能膨胀。

        实体数是 3：显式建的 "x" + add_relation 自动补的 "a" / "b"。
        """
        g = SQLiteGraphStore(db_path)
        for _ in range(3):
            g.add_entity(Entity(entity_id="x", label="A"))
            g.add_relation(Relation(source_id="a", target_id="b", rel_type="r"))
        assert g.entity_count == 3
        assert g.relation_count == 1
        g.close()

    def test_missing_entity_is_none(self, db_path: str) -> None:
        g = SQLiteGraphStore(db_path)
        assert g.find_entity("ghost") is None
        assert g.related("ghost") == []
        g.close()

    def test_parent_dir_created(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "graph.db"
        g = SQLiteGraphStore(str(nested))
        assert nested.parent.exists()
        g.close()

    def test_matches_inmemory_semantics(self, db_path: str) -> None:
        """两个实现必须行为一致，否则换后端就换出一堆 bug。"""
        mem = InMemoryGraphStore()
        sql = SQLiteGraphStore(db_path)
        for graph in (mem, sql):
            _seed(graph)

        assert mem.entity_count == sql.entity_count
        assert mem.relation_count == sql.relation_count
        assert len(mem.related("套餐")) == len(sql.related("套餐"))
        sql.close()

    def test_memory_pipeline_with_sqlite_graph(self, db_path: str) -> None:
        """真实链路：编码 → 建图 → 图谱召回。"""
        graph = SQLiteGraphStore(db_path)
        pipeline = MemoryPipeline(graph=graph)
        item = MemoryItem(
            memory_id="m9", user_id="u1", fact="套餐到期了", entities=["套餐"],
        )
        pipeline.store(item)
        pipeline._link_graph(item)  # noqa: SLF001

        assert graph.find_entity("套餐") is not None
        assert pipeline.graph_neighbors("u1", "套餐怎么续费") == ["套餐到期了"]

        # 重开图谱后记忆仍在（记忆本身在别处，这里只验证图谱边）
        graph.close()
        reopened = SQLiteGraphStore(db_path)
        assert [e.entity_id for _, e in reopened.related("套餐")] == ["m9"]
        reopened.close()
