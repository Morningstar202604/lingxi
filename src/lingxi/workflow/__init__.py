"""智犀 (ZhiXi) Workflow DSL — 可视化流程编排引擎。

🔗 工程逻辑
    将硬编码的 CustomerServicePipeline.handle_event 调用链抽象为 DAG 描述语言:
    - models: WorkflowDAG / DAGNode / DAGEdge / NodeType Pydantic 模型
    - validator: DAG 结构校验（无环、引用完整性、入口/出口）
    - mermaid: Mermaid flowchart 导出（文本 → 可粘贴渲染）
    - builder: 默认 DAG 构建 + YAML 序列化/反序列化
    - router: FastAPI 管理端点（graph/validate/preview）

    用法::

        from lingxi.workflow import (
            WorkflowDAG, DAGNode, DAGEdge, NodeType,
            build_default_pipeline_dag, to_mermaid, to_yaml, validate_dag,
        )

        dag = build_default_pipeline_dag()
        errors = validate_dag(dag)
        mermaid = to_mermaid(dag)

🤖 AI 避坑
    - 不要在这里写默认 DAG 的运行时逻辑（运行时由 pipeline.py 负责）
    - 本模块只做"描述 + 校验 + 可视化"，不执行业务
"""
from lingxi.workflow.builder import (
    ValidationError,
    build_default_pipeline_dag,
    from_yaml,
    to_yaml,
)
from lingxi.workflow.mermaid import (
    to_mermaid,
    to_mermaid_with_highlight,
    to_topological_view,
)
from lingxi.workflow.models import (
    DAGEdge,
    DAGNode,
    EdgeCondition,
    NodeType,
    WorkflowDAG,
)
from lingxi.workflow.router import create_workflow_router
from lingxi.workflow.router import router as workflow_router
from lingxi.workflow.validator import validate_dag

__all__ = [
    # ── Models ──
    "NodeType",
    "EdgeCondition",
    "DAGNode",
    "DAGEdge",
    "WorkflowDAG",
    # ── Validation ──
    "validate_dag",
    "ValidationError",
    # ── Mermaid export ──
    "to_mermaid",
    "to_mermaid_with_highlight",
    "to_topological_view",
    # ── Build / serialize ──
    "build_default_pipeline_dag",
    "to_yaml",
    "from_yaml",
    # ── FastAPI router ──
    "create_workflow_router",
    "workflow_router",
]
