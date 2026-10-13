"""DAG 验证器。

🔗 工程逻辑
    验证内容:
    1. 无环 (cycle detection) — DFS 三色标记
    2. 所有 edge 引用的节点都存在
    3. 有且仅有一个 entry point（节点没有入边）
    4. 至少有一个 exit point（节点没有出边）

🤖 AI 避坑
    - 验证必须 fast-fail，第一个错误就报清楚
    - 不需要做 DAG 的并发可达性证明
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lingxi.workflow.models import WorkflowDAG


# DFS 三色标记: WHITE=未访问, GRAY=正在访问, BLACK=已完成
_WHITE, _GRAY, _BLACK = 0, 1, 2


def _detect_cycle(
    dag: WorkflowDAG,
) -> list[str] | None:
    """DFS 三色标记检测有向环。

    返回环的路径节点列表，无环则返回 None。
    """
    node_ids = dag.get_node_ids()
    color: dict[str, int] = {nid: _WHITE for nid in node_ids}
    parent: dict[str, str | None] = {nid: None for nid in node_ids}
    adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for edge in dag.edges:
        adj[edge.source].append(edge.target)

    def _dfs(u: str) -> list[str] | None:
        color[u] = _GRAY
        for v in adj[u]:
            if color[v] == _GRAY:
                # 找到环，回溯路径
                cycle = [v, u]
                node = u
                while parent.get(node) is not None and parent[node] != v:
                    node = parent[node]  # type: ignore[assignment]
                    cycle.append(node)
                cycle.reverse()
                return cycle
            if color[v] == _WHITE:
                parent[v] = u
                result = _dfs(v)
                if result is not None:
                    return result
        color[u] = _BLACK
        return None

    for nid in node_ids:
        if color[nid] == _WHITE:
            result = _dfs(nid)
            if result is not None:
                return result
    return None


def validate_dag(dag: WorkflowDAG) -> list[str]:
    """返回空列表表示通过，否则返回错误信息列表。

    验证规则:
    1. DAG 至少包含一个节点
    2. 所有 edge 引用的 source 和 target 都存在于 nodes 中
    3. DAG 中无环（DFS 三色标记）
    4. 有且仅有一个入口节点（无入边）
    5. 至少有一个出口节点（无出边）
    """
    errors: list[str] = []

    # 规则 1: 非空检查
    if not dag.nodes:
        errors.append("DAG 必须至少包含一个节点")
        return errors  # 无节点则后续规则都无意义

    node_ids = dag.get_node_ids()

    # 规则 2: 边引用的节点必须存在
    for edge in dag.edges:
        if edge.source not in node_ids:
            errors.append(
                f"边引用了不存在的源节点: source={edge.source!r}"
            )
        if edge.target not in node_ids:
            errors.append(
                f"边引用了不存在的目标节点: target={edge.target!r}"
            )

    if errors:
        return errors  # 引用非法则后续拓扑检查跳过

    # 规则 3: 环检测
    cycle = _detect_cycle(dag)
    if cycle is not None:
        cycle_str = " -> ".join(cycle + [cycle[0]])
        errors.append(f"检测到环: {cycle_str}")
        return errors  # 有环时不做后续拓扑校验

    # 规则 4: 有且仅有一个入口节点
    entries = dag.entry_nodes()
    if len(entries) == 0:
        errors.append("未找到入口节点（所有节点都有入边，可能存在环）")
    elif len(entries) > 1:
        errors.append(f"存在多个入口节点: {entries}")

    # 规则 5: 至少有一个出口节点
    exits = dag.exit_nodes()
    if len(exits) == 0:
        errors.append("未找到出口节点（所有节点都有出边，可能存在环）")

    return errors


__all__ = ["validate_dag"]
