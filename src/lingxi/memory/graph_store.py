"""Node 3.3 — 实体图谱存储（抽象接口 + 内存实现 + SQLite 实现）。

🔗 工程逻辑
    实体图谱用于存储"谁认识谁、谁买了什么、谁在哪"这类结构化事实。
    - 接口先行：先定义 GraphStore ABC，业务代码只依赖接口
    - `InMemoryGraphStore` 用于测试与单进程临时运行
    - `SQLiteGraphStore` 是生产默认：零运维、随仓库自带，重启不丢
    - Neo4j 仍是可选扩展（大规模多跳查询），当前不引入额外依赖

🤖 AI 避坑
    - 不要在这里做 ORM 式重度建模（Entity 保持 dataclass 就行）
    - 事实的"置信度"和"来源"很重要（是谁说的、什么时候说的）
    - 实体消歧"张总=张伟=zhangwei"是 NLP 难题，先做精确匹配
    - `related()` 是**邻居查询**语义：邻接必须双向登记，否则从实体
      反查记忆永远返回空
"""
from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Entity:
    """图谱节点。"""

    entity_id: str
    label: str  # "Person" | "Product" | "Location" | ...
    properties: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass
class Relation:
    """图谱边。"""

    source_id: str
    target_id: str
    rel_type: str | None = None  # "KNOWS" | "BOUGHT" | "LOCATED_IN" | ...
    properties: dict[str, str | int | float | bool] = field(default_factory=dict)


class GraphStore(ABC):
    """实体图谱抽象接口。"""

    @abstractmethod
    def add_entity(self, entity: Entity) -> None: ...

    @abstractmethod
    def add_relation(self, relation: Relation) -> None: ...

    @abstractmethod
    def find_entity(self, entity_id: str) -> Entity | None: ...

    @abstractmethod
    def search(self, label: str | None = None, **properties: str | int | float | bool) -> list[Entity]: ...

    @abstractmethod
    def related(self, entity_id: str, rel_type: str | None = None) -> list[tuple[Relation, Entity]]: ...


class InMemoryGraphStore(GraphStore):
    """基于 dict 的内存图谱（单进程测试/开发用）。"""

    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        # 节点 id -> 与之相邻的边（双向）。
        # 此前只登记 `_adj[source]`，于是 related(entity) 从实体侧出发永远为空
        # —— 图谱一旦用于「从实体反查记忆」，整条链路就是断的。
        self._adj: dict[str, list[Relation]] = {}

    def add_entity(self, entity: Entity) -> None:
        self._entities[entity.entity_id] = entity
        if entity.entity_id not in self._adj:
            self._adj[entity.entity_id] = []

    def add_relation(self, relation: Relation) -> None:
        self._ensure_entity_exists(relation.source_id)
        self._ensure_entity_exists(relation.target_id)
        self._adj[relation.source_id].append(relation)
        # 双向登记：related() 是"邻居查询"语义，不该要求调用方知道边的方向
        if relation.target_id != relation.source_id:
            self._adj[relation.target_id].append(relation)

    def find_entity(self, entity_id: str) -> Entity | None:
        return self._entities.get(entity_id)

    def search(self, label: str | None = None, **properties: str | int | float | bool) -> list[Entity]:
        results: list[Entity] = []
        for ent in self._entities.values():
            if label and ent.label != label:
                continue
            if all(ent.properties.get(k) == v for k, v in properties.items()):
                results.append(ent)
        return results

    def related(self, entity_id: str, rel_type: str | None = None) -> list[tuple[Relation, Entity]]:
        """返回与该节点相邻的 (关系, 邻居)。

        邻居 = 关系的另一端；从 source 查询得到 target，从 target 查询得到 source。
        """
        out: list[tuple[Relation, Entity]] = []
        for rel in self._adj.get(entity_id, []):
            if rel_type and rel.rel_type != rel_type:
                continue
            neighbor_id = (
                rel.target_id if rel.source_id == entity_id else rel.source_id
            )
            neighbor = self._entities.get(neighbor_id)
            if neighbor:
                out.append((rel, neighbor))
        return out

    def _ensure_entity_exists(self, entity_id: str) -> None:
        if entity_id not in self._entities:
            self.add_entity(Entity(entity_id=entity_id, label="Unknown"))

    @property
    def entity_count(self) -> int:
        return len(self._entities)

    @property
    def relation_count(self) -> int:
        """关系总数（邻接表双向登记，故除以 2）。

        补这个是为了与 SQLiteGraphStore 接口对齐 —— 两个实现方法集不同，
        换后端时就会撞上 AttributeError。
        """
        return sum(len(v) for v in self._adj.values()) // 2


class SQLiteGraphStore(GraphStore):
    """SQLite 持久化图谱。

    🔗 工程逻辑
        `InMemoryGraphStore` 重启即丢 —— 记忆管线每次重启都从零建图，
        「长期记忆 + 实体图谱」就退化成了单次会话记忆。
        Neo4j 需要外部服务，自部署场景常有；SQLite 随仓库自带、零运维，
        且与 memory/tickets/queue 共用同一套 SQLite 依赖，不新增引入成本。

    选 SQLite 而不是 ORM：图查询只有「按 id 取邻居」与「按 label 搜」两种，
        两条 SQL 就够，引入 ORM 只会增加依赖与启动开销。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS graph_entities (
                    entity_id  TEXT PRIMARY KEY,
                    label      TEXT NOT NULL,
                    properties TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS graph_relations (
                    source_id  TEXT NOT NULL,
                    target_id  TEXT NOT NULL,
                    rel_type   TEXT,
                    properties TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (source_id, target_id, rel_type)
                );
                CREATE INDEX IF NOT EXISTS idx_rel_target
                    ON graph_relations(target_id);
                CREATE INDEX IF NOT EXISTS idx_ent_label
                    ON graph_entities(label);
                """,
            )

    def add_entity(self, entity: Entity) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO graph_entities(entity_id,label,properties) VALUES(?,?,?) "
                "ON CONFLICT(entity_id) DO UPDATE SET label=excluded.label, "
                "properties=excluded.properties",
                (
                    entity.entity_id,
                    entity.label,
                    json.dumps(entity.properties, ensure_ascii=False),
                ),
            )

    def add_relation(self, relation: Relation) -> None:
        self._ensure_entity_exists(relation.source_id)
        self._ensure_entity_exists(relation.target_id)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO graph_relations"
                "(source_id,target_id,rel_type,properties) VALUES(?,?,?,?)",
                (
                    relation.source_id,
                    relation.target_id,
                    relation.rel_type,
                    json.dumps(relation.properties, ensure_ascii=False),
                ),
            )

    def find_entity(self, entity_id: str) -> Entity | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM graph_entities WHERE entity_id=?", (entity_id,),
            ).fetchone()
        return _row_to_entity(row) if row else None

    def search(self, label: str | None = None, **properties: Any) -> list[Entity]:
        sql = "SELECT * FROM graph_entities WHERE 1=1"
        params: list[Any] = []
        if label:
            sql += " AND label=?"
            params.append(label)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        out = []
        for row in rows:
            ent = _row_to_entity(row)
            if ent is None:
                continue
            if all(ent.properties.get(k) == v for k, v in properties.items()):
                out.append(ent)
        return out

    def related(
        self, entity_id: str, rel_type: str | None = None,
    ) -> list[tuple[Relation, Entity]]:
        # 双向查询：邻居既可能是 target 也可能是 source
        sql = (
            "SELECT * FROM graph_relations WHERE source_id=? OR target_id=?"
        )
        params: list[Any] = [entity_id, entity_id]
        if rel_type:
            sql += " AND rel_type=?"
            params.append(rel_type)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        out: list[tuple[Relation, Entity]] = []
        for row in rows:
            neighbor_id = (
                row["target_id"] if row["source_id"] == entity_id else row["source_id"]
            )
            neighbor = self.find_entity(neighbor_id)
            if neighbor is not None:
                out.append((_row_to_relation(row), neighbor))
        return out

    def _ensure_entity_exists(self, entity_id: str) -> None:
        if self.find_entity(entity_id) is None:
            self.add_entity(Entity(entity_id=entity_id, label="Unknown"))

    @property
    def entity_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM graph_entities",
            ).fetchone()
        return int(row["n"]) if row else 0

    @property
    def relation_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM graph_relations",
            ).fetchone()
        return int(row["n"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_entity(row: sqlite3.Row) -> Entity | None:
    try:
        props = json.loads(row["properties"] or "{}")
    except json.JSONDecodeError:
        props = {}
    return Entity(
        entity_id=row["entity_id"],
        label=row["label"],
        properties=props if isinstance(props, dict) else {},
    )


def _row_to_relation(row: sqlite3.Row) -> Relation:
    try:
        props = json.loads(row["properties"] or "{}")
    except json.JSONDecodeError:
        props = {}
    return Relation(
        source_id=row["source_id"],
        target_id=row["target_id"],
        rel_type=row["rel_type"],
        properties=props if isinstance(props, dict) else {},
    )


__all__ = [
    "Entity",
    "Relation",
    "GraphStore",
    "InMemoryGraphStore",
    "SQLiteGraphStore",
]
