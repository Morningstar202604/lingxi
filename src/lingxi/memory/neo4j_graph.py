"""Neo4j 实体图谱后端（大规模多跳查询）。

🔗 工程逻辑
    与 SQLiteGraphStore 实现同一个 GraphStore 接口（graph_store.py:47），
    业务代码只依赖接口，换后端不改调用方。

    - 节点: (:Entity {id, label}) + properties JSON
    - 边:   (a)-[:REL {type, properties}]->(b)
    - related() 走 BOTH 方向（与 InMemory/SQLite 的"邻居查询"语义对齐）

🤖 AI 避坑
    - neo4j 驱动是可选依赖（extra: neo4j），import 失败要 fail-fast 报清楚
      而不是静默降级成空图（那是"配置了 Neo4j 却零图谱"的坑）
    - cypher 参数全部走 $ 占位符，绝不拼接用户输入
    - driver 要 close()，否则连接池泄漏
"""
from __future__ import annotations

import json
from typing import Any

from lingxi.core import get_logger
from lingxi.memory.graph_store import Entity, GraphStore, Relation

log = get_logger(__name__)


class Neo4jGraphStore(GraphStore):
    """Neo4j 持久化图谱（neo4j-python 驱动，同步会话）。"""

    def __init__(
        self,
        uri: str = "bolt://127.0.0.1:7687",
        username: str = "neo4j",
        password: str = "",
    ) -> None:
        try:
            from neo4j import GraphDatabase
        except ImportError as e:
            raise RuntimeError(
                "neo4j driver is not installed. Run: pip install neo4j"
            ) from e

        self._driver = GraphDatabase.driver(uri, auth=(username, password))
        self._driver.verify_connectivity()  # fail-fast: 连不上立即报错
        with self._driver.session() as session:
            # Neo4j 5 语法：约束用 REQUIRE（旧版 ASSERT/WHERE 已不接受）
            session.run(
                """
                CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity)
                REQUIRE e.id IS UNIQUE
                """
            )
        log.info("neo4j_graph_connected", uri=uri, user=username)

    def _props_json(self, props: dict[str, Any]) -> str:
        return json.dumps(props, ensure_ascii=False, default=str)

    def add_entity(self, entity: Entity) -> None:
        with self._driver.session() as session:
            session.run(
                """
                MERGE (e:Entity {id: $id})
                SET e.label = $label, e.properties = $props
                """,
                id=entity.entity_id,
                label=entity.label,
                props=self._props_json(entity.properties),
            )

    def add_relation(self, relation: Relation) -> None:
        with self._driver.session() as session:
            # 每个 MERGE 各自带 ON CREATE：多 MERGE 共用一个 ON CREATE 时，
            # 只要任一节点是新建的就会连带改写已存在节点的 label（实测会
            # 把 Person 覆盖成 Unknown，契约测试捕获）。
            session.run(
                """
                MERGE (a:Entity {id: $src})
                  ON CREATE SET a.label = 'Unknown'
                MERGE (b:Entity {id: $dst})
                  ON CREATE SET b.label = 'Unknown'
                MERGE (a)-[r:REL]->(b)
                SET r.type = $type, r.properties = $props
                """,
                src=relation.source_id,
                dst=relation.target_id,
                type=relation.rel_type or "",
                props=self._props_json(relation.properties),
            )

    def find_entity(self, entity_id: str) -> Entity | None:
        with self._driver.session() as session:
            result = session.run(
                "MATCH (e:Entity {id: $id}) RETURN e.label AS label, "
                "e.properties AS props",
                id=entity_id,
            )
            row = result.single()
        if row is None:
            return None
        return Entity(
            entity_id=entity_id,
            label=row["label"],
            properties=_props_from_db(row["props"]),
        )

    def search(
        self, label: str | None = None, **properties: str | int | float | bool
    ) -> list[Entity]:
        with self._driver.session() as session:
            if label:
                result = session.run(
                    "MATCH (e:Entity) WHERE e.label = $label "
                    "RETURN e.id AS id, e.label AS label, e.properties AS props",
                    label=label,
                )
            else:
                result = session.run(
                    "MATCH (e:Entity) "
                    "RETURN e.id AS id, e.label AS label, e.properties AS props"
                )
            out: list[Entity] = []
            for row in result:
                ent = Entity(
                    entity_id=row["id"],
                    label=row["label"],
                    properties=_props_from_db(row["props"]),
                )
                if properties and all(
                    ent.properties.get(k) == v for k, v in properties.items()
                ) or not properties:
                    out.append(ent)
            return out

    def related(
        self, entity_id: str, rel_type: str | None = None
    ) -> list[tuple[Relation, Entity]]:
        with self._driver.session() as session:
            if rel_type:
                result = session.run(
                    """
                    MATCH (e:Entity {id: $id})-[r:REL]-(n:Entity)
                    WHERE r.type = $type
                    RETURN r.type AS type, r.properties AS props,
                           startNode(r).id AS src, endNode(r).id AS dst,
                           n.id AS neighbor
                    """,
                    id=entity_id,
                    type=rel_type,
                )
            else:
                result = session.run(
                    """
                    MATCH (e:Entity {id: $id})-[r:REL]-(n:Entity)
                    RETURN r.type AS type, r.properties AS props,
                           startNode(r).id AS src, endNode(r).id AS dst,
                           n.id AS neighbor
                    """,
                    id=entity_id,
                )
            out: list[tuple[Relation, Entity]] = []
            for row in result:
                neighbor_id = row["neighbor"]
                if neighbor_id == entity_id:
                    continue
                neighbor = self.find_entity(neighbor_id)
                if neighbor is None:
                    continue
                rel = Relation(
                    source_id=row["src"],
                    target_id=row["dst"],
                    rel_type=row["type"] or None,
                    properties=_props_from_db(row["props"]),
                )
                out.append((rel, neighbor))
            return out

    @property
    def entity_count(self) -> int:
        with self._driver.session() as session:
            result = session.run("MATCH (e:Entity) RETURN count(e) AS n")
            row = result.single()
        return int(row["n"]) if row else 0

    @property
    def relation_count(self) -> int:
        with self._driver.session() as session:
            result = session.run("MATCH ()-[r:REL]-() RETURN count(DISTINCT r) AS n")
            row = result.single()
        return int(row["n"]) if row else 0

    def close(self) -> None:
        self._driver.close()
        log.info("neo4j_graph_closed")


def _props_from_db(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return val if isinstance(val, dict) else {}
    if isinstance(raw, dict):
        return raw
    return {}


__all__ = ["Neo4jGraphStore"]
