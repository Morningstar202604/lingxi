"""Node 3.3 — 实体图谱存储（抽象接口 + 内存实现 + Neo4j 占位）。

🔗 工程逻辑
    实体图谱用于存储"谁认识谁、谁买了什么、谁在哪"这类结构化事实。
    - 接口先行：先定义 GraphStore ABC，业务代码只依赖接口
    - 本地实现用 NetworkX（单机零依赖）
    - Neo4j 实现后续接入（大规模、多跳查询）

🤖 AI 避坑
    - 不要在这里做 ORM 式重度建模（Entity 保持 dataclass 就行）
    - 事实的"置信度"和"来源"很重要（是谁说的、什么时候说的）
    - 实体消歧"张总=张伟=zhangwei"是 NLP 难题，先做精确匹配
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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
        self._adj: dict[str, list[Relation]] = {}  # source_id -> relations

    def add_entity(self, entity: Entity) -> None:
        self._entities[entity.entity_id] = entity
        if entity.entity_id not in self._adj:
            self._adj[entity.entity_id] = []

    def add_relation(self, relation: Relation) -> None:
        self._ensure_entity_exists(relation.source_id)
        self._ensure_entity_exists(relation.target_id)
        self._adj[relation.source_id].append(relation)

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
        out: list[tuple[Relation, Entity]] = []
        for rel in self._adj.get(entity_id, []):
            if rel_type and rel.rel_type != rel_type:
                continue
            target = self._entities.get(rel.target_id)
            if target:
                out.append((rel, target))
        return out

    def _ensure_entity_exists(self, entity_id: str) -> None:
        if entity_id not in self._entities:
            self.add_entity(Entity(entity_id=entity_id, label="Unknown"))

    @property
    def entity_count(self) -> int:
        return len(self._entities)


__all__ = ["Entity", "Relation", "GraphStore", "InMemoryGraphStore"]
