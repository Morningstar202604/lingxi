"""智犀 (ZhiXi) Workflow DSL — 可视化流程编排**与执行**引擎。

🔗 工程逻辑
    将硬编码的 CustomerServicePipeline.handle_event 调用链抽象为 DAG 描述语言:
    - models: WorkflowDAG / DAGNode / DAGEdge / NodeType Pydantic 模型
    - validator: DAG 结构校验（无环、引用完整性、入口/出口）
    - mermaid: Mermaid flowchart 导出（文本 → 可粘贴渲染）
    - builder: 默认 DAG 构建 + YAML 序列化/反序列化
    - executor: 按拓扑 + 条件边真正执行，产出逐节点 trace
    - router: FastAPI 端点（graph/validate/preview/execute）

    用法::

        from lingxi.workflow import (
            WorkflowDAG, DAGNode, DAGEdge, NodeType,
            build_default_pipeline_dag, to_mermaid, to_yaml, validate_dag,
            build_default_executor, WorkflowContext,
        )

        dag = build_default_pipeline_dag()
        errors = validate_dag(dag)
        mermaid = to_mermaid(dag)

        executor = build_default_executor({"agent": agent, "ticket_store": tickets})
        result = await executor.run(dag, WorkflowContext(raw_message="我要退款"))

🤖 AI 避坑
    - 默认 DAG 描述的**仍是** pipeline.handle_event 的链。executor 是它的
      可执行镜像，两者独立演进 —— 改了默认 DAG 也要同步 pipeline，
      否则图和实际行为会分叉（这是刻意保留的已知限制）。
    - 组件缺失一律优雅降级为 skipped，不要抛异常。
"""
from lingxi.workflow.builder import (
    ValidationError,
    build_default_pipeline_dag,
    from_yaml,
    to_yaml,
)
from lingxi.workflow.executor import (
    ExecutionResult,
    NodeTrace,
    WorkflowContext,
    WorkflowExecutor,
    build_default_executor,
    evaluate_condition,
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
    # ── Execution ──
    "WorkflowExecutor",
    "WorkflowContext",
    "ExecutionResult",
    "NodeTrace",
    "build_default_executor",
    "evaluate_condition",
    # ── FastAPI router ──
    "create_workflow_router",
    "workflow_router",
]
