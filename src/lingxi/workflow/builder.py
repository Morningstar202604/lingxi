"""默认 PipelineDAG 构建器 + YAML 序列化/反序列化。

🔗 工程逻辑
    用代码构建默认 DAG (static builder)，然后可序列化为 YAML，
    用户修改 YAML 后反序列化再 reload 到运行时。

🤖 AI 避坑
    - YAML 解析要做内容校验（不能执行任意代码）
    - PyYAML 默认 Loader 有毒，必须用 SafeLoader
    - auto_reload 功能先不做——只做导出/导入
"""
from __future__ import annotations

from enum import Enum
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from lingxi.workflow.models import (
    DAGEdge,
    DAGNode,
    EdgeCondition,
    NodeType,
    WorkflowDAG,
)


class ValidationError(Exception):
    """YAML 反序列化或结构校验失败。"""


def build_default_pipeline_dag() -> WorkflowDAG:
    """从当前 pipeline.py 逻辑导出 DAG（静态描述）。

    基于 CustomerServicePipeline.handle_event 的调用链创建:
    1. 入口: 接收 IM 事件
    2. 规则引擎评估 (短路升级)
    3. 关键词强制升级 (短路升级)
    4. 知识召回 (RAG)
    5. LLM 推理 (Agent)
    6. 置信度判断 → 升级 或 直接回复
    7. 审计日志
    """
    nodes: list[DAGNode] = [
        DAGNode(
            id="entry",
            type=NodeType.REPLY,
            label="IM事件入口",
            params={"source": "im_adapter", "description": "NapCat / Gewechat"},
        ),
        DAGNode(
            id="rule_engine",
            type=NodeType.RULE_ENGINE,
            label="规则引擎",
            params={
                "description": "正则/关键字匹配 → escalate 条件短路升级",
                "action_on_match": "escalate",
            },
        ),
        DAGNode(
            id="keyword_escalate",
            type=NodeType.RULE_ENGINE,
            label="关键词升级",
            params={"description": "强制关键字匹配 → 跳过 Agent 直接升级"},
        ),
        DAGNode(
            id="ticket_intake",
            type=NodeType.TOOL_EXEC,
            label="工单创建/分类",
            params={"intent_classify": True, "pii_sanitize": True},
        ),
        DAGNode(
            id="knowledge_recall",
            type=NodeType.KNOWLEDGE_RECALL,
            label="知识召回",
            params={
                "rag_top_k": 5,
                "max_chars": 2000,
                "lorebook_enabled": True,
            },
        ),
        DAGNode(
            id="llm_inference",
            type=NodeType.LLM_INFERENCE,
            label="LLM 推理",
            params={
                "model": "gpt-4-turbo",
                "timeout": 45.0,
                "confidence_threshold": 0.6,
            },
        ),
        DAGNode(
            id="confidence_check",
            type=NodeType.RULE_ENGINE,
            label="置信度判断",
            params={"threshold": 0.6},
        ),
        DAGNode(
            id="seat_assign",
            type=NodeType.SEAT_ASSIGN,
            label="坐席分配",
            params={"strategy": "round_robin"},
        ),
        DAGNode(
            id="audit_log",
            type=NodeType.AUDIT_LOG,
            label="审计日志",
            params={"events": ["ticket.escalated", "ticket.replied"]},
        ),
        DAGNode(
            id="reply",
            type=NodeType.REPLY,
            label="回复用户",
            params={"pii_sanitize_output": True},
        ),
    ]

    edges: list[DAGEdge] = [
        # 入口 → 规则引擎
        DAGEdge(source="entry", target="rule_engine"),
        # 规则引擎 → 命中升级: 分配坐席 + 审计
        DAGEdge(
            source="rule_engine",
            target="seat_assign",
            condition=EdgeCondition(
                field="rule.match",
                operator="equals",
                value="escalate",
            ),
        ),
        # 规则引擎 → 未命中: 关键词检查
        DAGEdge(
            source="rule_engine",
            target="keyword_escalate",
            condition=EdgeCondition(
                field="rule.match",
                operator="equals",
                value="pass",
            ),
        ),
        # 关键词升级 → 命中
        DAGEdge(
            source="keyword_escalate",
            target="seat_assign",
            condition=EdgeCondition(
                field="keyword.matched",
                operator="equals",
                value=True,
            ),
        ),
        # 关键词 → 未命中: 工单创建
        DAGEdge(
            source="keyword_escalate",
            target="ticket_intake",
            condition=EdgeCondition(
                field="keyword.matched",
                operator="equals",
                value=False,
            ),
        ),
        # 工单 → 知识召回
        DAGEdge(source="ticket_intake", target="knowledge_recall"),
        # 知识召回 → LLM 推理
        DAGEdge(source="knowledge_recall", target="llm_inference"),
        # LLM 推理 → 置信度判断
        DAGEdge(source="llm_inference", target="confidence_check"),
        # 置信度低 → 升级坐席
        DAGEdge(
            source="confidence_check",
            target="seat_assign",
            condition=EdgeCondition(
                field="confidence",
                operator="lt",
                value=0.6,
            ),
        ),
        # 置信度足够 → 回复
        DAGEdge(
            source="confidence_check",
            target="reply",
            condition=EdgeCondition(
                field="confidence",
                operator="gte",
                value=0.6,
            ),
        ),
        # 坐席分配 → 审计日志
        DAGEdge(source="seat_assign", target="audit_log"),
        # 回复 → 审计日志
        DAGEdge(source="reply", target="audit_log"),
    ]

    return WorkflowDAG(
        name="zhixi_default_pipeline",
        description="智犀默认客服 DAG — 基于 pipeline.py handle_event 调用链自动生成",
        version="1.0",
        nodes=nodes,
        edges=edges,
    )


def _convert_enums(obj: Any) -> Any:
    """递归转换枚举为字符串值（PyYAML SafeDumper 无法识别 Enum 子类）。"""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: _convert_enums(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_enums(item) for item in obj]
    return obj


def to_yaml(dag: WorkflowDAG) -> str:
    """序列化为 YAML 字符串。"""
    data = _convert_enums(dag.model_dump(mode="python"))
    return yaml.safe_dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )


def from_yaml(yaml_str: str) -> WorkflowDAG:
    """反序列化 YAML 字符串为 WorkflowDAG。

    安全约束:
    - 使用 safe_load 防止代码注入
    - Pydantic 校验结构合法性

    Raises:
        ValidationError: YAML 结构非法或缺少必填字段。
    """
    try:
        data = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        raise ValidationError(f"YAML 解析失败: {e}") from e

    if not isinstance(data, dict):
        raise ValidationError("YAML 顶层必须是字典/映射类型")

    try:
        return WorkflowDAG(**data)
    except PydanticValidationError as e:
        raise ValidationError(f"DAG 结构校验失败: {e}") from e


__all__ = [
    "build_default_pipeline_dag",
    "to_yaml",
    "from_yaml",
    "ValidationError",
]
