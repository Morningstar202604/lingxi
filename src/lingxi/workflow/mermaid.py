"""Mermaid 图导出。

🔗 工程逻辑
    Mermaid flowchart 语法:
    flowchart TD
    A[知识召回] --> B{规则拦截?}
    B -->|否| C[LLM 推理]
    B -->|是| D[升级坐席]
    C --> E[后处理]

    用 TD (top-down)，每个节点按 type 显示不同形状:
    - rectangle: 标准处理
    - rounded: 输入/输出
    - rhombus/stadium: 条件判断
    - circle: entry/exit

🤖 AI 避坑
    - 节点 label 不能包含 Mermaid 特殊字符: [](){}|.
    - 超过 8 个节点的图建议分片 or 子图
"""
from __future__ import annotations

from lingxi.workflow.models import DAGNode, NodeType, WorkflowDAG

# Mermaid 特殊字符转义: [](){}.| 需要转义为 HTML entity 或引号包裹
_MERMAID_SPECIAL = "[](){}|."

# 节点类型 → Mermaid 节点形状
# 格式: (前缀, 后缀) 括号对
_NODE_SHAPES: dict[NodeType, tuple[str, str]] = {
    NodeType.KNOWLEDGE_RECALL: ("[", "]"),  # rectangle — 标准处理
    NodeType.RULE_ENGINE: ("{", "}"),  # rhombus — 条件判断
    NodeType.LLM_INFERENCE: ("[", "]"),  # rectangle
    NodeType.TOOL_EXEC: ("[", "]"),  # rectangle
    NodeType.SEAT_ASSIGN: ("([", "])"),  # stadium — 过程
    NodeType.AUDIT_LOG: ("[", "]"),  # rectangle
    NodeType.REPLY: ("(", ")"),  # rounded — 输出/终端
}

# 高亮样式: 当前活跃节点
_ACTIVE_STYLE = ":::active"


def _escape_label(label: str) -> str:
    """转义 Mermaid 特殊字符。"""
    escaped = label
    for ch in _MERMAID_SPECIAL:
        escaped = escaped.replace(ch, f"&#{ord(ch)};")
    return escaped


def _node_display_name(node: DAGNode) -> str:
    """返回节点的可读显示名称（优先用 label）。"""
    return node.label or node.id


def _format_node(node: DAGNode) -> str:
    """格式化单个节点为 Mermaid 语法。"""
    shape_prefix, shape_suffix = _NODE_SHAPES.get(
        node.type, ("[", "]")
    )
    label = _escape_label(_node_display_name(node))
    return f"{node.id}{shape_prefix}\"{label}\"{shape_suffix}"


def _format_edge(
    source: str,
    target: str,
    label: str = "",
    dashed: bool = False,
) -> str:
    """格式化单条边为 Mermaid 语法。"""
    connector = "-.->" if dashed else "-->"
    if label:
        escaped_label = _escape_label(label)
        return f"{source} {connector}|{escaped_label}| {target}"
    return f"{source} {connector} {target}"


def to_mermaid(dag: WorkflowDAG, title: str | None = None) -> str:
    """返回 Mermaid flowchart string。

    Args:
        dag: 工作流 DAG 定义。
        title: 可选图标题。

    Returns:
        可直接粘贴到 mermaid.live 渲染的 Mermaid 流程图文本。
    """
    lines: list[str] = []

    # header
    lines.append("flowchart TD")
    if title:
        lines.append(f"    %% {{{title}}}")
        lines.append("")

    # 节点定义
    for node in dag.nodes:
        lines.append(f"    {_format_node(node)}")

    lines.append("")

    # 边定义
    for edge in dag.edges:
        label = edge.label if edge.is_conditional else ""
        lines.append(f"    {_format_edge(edge.source, edge.target, label)}")

    return "\n".join(lines)


__all__ = ["to_mermaid"]
