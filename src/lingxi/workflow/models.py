"""DAG 节点和边的 Pydantic 模型。

🔗 工程逻辑
    DAG 是"有向无环图"：节点 = 处理步骤，边 = 依赖关系。
    Pipeline 的 handle_event 就是按拓扑排序依次执行节点。

    节点类型:
    - knowledge_recall: 前置，召回 RAG
    - rule_engine: 前置/中断，匹配规则可短路
    - llm_inference: 核心，调 LLM
    - tool_exec: 工具调用
    - seat_assign: 升级分配坐席
    - audit_log: 后置，记录
    - reply: 后置，回复用户

🤖 AI 避坑
    - DAG 必须无环 — 验证算法用 DFS 三色标记
    - 节点可以有多个出口条件（conditional edge）
    - conditional edge 的条件用预定义字段匹配: ticket.status / confidence / keyword / timeout
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class NodeType(StrEnum):
    """DAG 节点类型枚举。"""

    KNOWLEDGE_RECALL = "knowledge_recall"
    RULE_ENGINE = "rule_engine"
    LLM_INFERENCE = "llm_inference"
    TOOL_EXEC = "tool_exec"
    SEAT_ASSIGN = "seat_assign"
    AUDIT_LOG = "audit_log"
    REPLY = "reply"


class EdgeCondition(BaseModel):
    """边的条件（为空表示无条件下游）。

    表示从 source 到 target 这条边在何种条件成立时才被激活。
    """

    field: str = ""  # "ticket.status" / "confidence" / "keyword" / "timeout"
    operator: str = ""  # "equals" / "lt" / "contains" / "exists"
    value: Any = None

    def is_empty(self) -> bool:
        """判断条件是否为空（无条件通过）。"""
        return not self.field


class DAGNode(BaseModel):
    """DAG 节点定义。"""

    id: str
    type: NodeType
    label: str = ""
    params: dict[str, Any] = Field(default_factory=dict)  # 节点定制参数


class DAGEdge(BaseModel):
    """DAG 有向边定义。"""

    source: str
    target: str
    condition: EdgeCondition | None = None

    @property
    def label(self) -> str:
        """返回边的可读标签（用于 Mermaid 显示条件）。"""
        if self.condition and not self.condition.is_empty():
            op = self.condition.operator or "is"
            return f"{self.condition.field} {op} {self.condition.value}"
        return ""

    @property
    def is_conditional(self) -> bool:
        """判断是否为条件边。"""
        return self.condition is not None and not self.condition.is_empty()


class WorkflowDAG(BaseModel):
    """完整的 DAG 工作流定义。"""

    name: str
    description: str = ""
    version: str = "1.0"
    nodes: list[DAGNode] = Field(default_factory=list)
    edges: list[DAGEdge] = Field(default_factory=list)

    def get_node_ids(self) -> set[str]:
        """返回所有节点 ID 集合。"""
        return {n.id for n in self.nodes}

    def get_in_degree(self, node_id: str) -> int:
        """返回节点入度。"""
        return sum(1 for e in self.edges if e.target == node_id)

    def get_out_degree(self, node_id: str) -> int:
        """返回节点出度。"""
        return sum(1 for e in self.edges if e.source == node_id)

    def get_successors(self, node_id: str) -> list[str]:
        """返回节点的直接后继节点 ID 列表。"""
        return [e.target for e in self.edges if e.source == node_id]

    def get_predecessors(self, node_id: str) -> list[str]:
        """返回节点的直接前驱节点 ID 列表。"""
        return [e.source for e in self.edges if e.target == node_id]

    def entry_nodes(self) -> list[str]:
        """返回入口节点 ID（无入边的节点）。"""
        return [n.id for n in self.nodes if self.get_in_degree(n.id) == 0]

    def exit_nodes(self) -> list[str]:
        """返回出口节点 ID（无出边的节点）。"""
        return [n.id for n in self.nodes if self.get_out_degree(n.id) == 0]


__all__ = [
    "NodeType",
    "EdgeCondition",
    "DAGNode",
    "DAGEdge",
    "WorkflowDAG",
]
