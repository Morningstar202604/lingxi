"""Workflow DAG 可视化与执行 API 路由。

🔗 工程逻辑
    提供五个 REST 端点:
    - GET  /api/workflow/graph           → 默认 DAG 的 Mermaid code (plain text)
    - GET  /api/workflow/graph/yaml      → 默认 DAG 的 YAML 定义
    - POST /api/workflow/graph/validate  → 校验用户提交的 DAG (JSON body)
    - POST /api/workflow/graph/preview   → 用户提交 YAML → 返回 Mermaid 预览
    - POST /api/workflow/execute         → 真正执行一张 DAG，返回逐步 trace

    认证: admin_token header (与 dashboard_api 保持一致)

🤖 AI 避坑
    - /graph 返回 text/plain (不是 JSON)，让浏览器直接渲染
    - validate 必须调用 validate_dag() 并返回结构化错误
    - preview 不持久化 YAML，仅做即时转化
    - execute 必须先 validate：未校验的 DAG 可能成环，会让 executor 死循环
    - execute 的组件来自引擎装配，**不能**接受请求体传入的对象引用
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Response

from lingxi.core.admin_auth import require_admin
from lingxi.core.logging import get_logger
from lingxi.workflow.builder import (
    ValidationError,
    build_default_pipeline_dag,
    from_yaml,
    to_yaml,
)
from lingxi.workflow.executor import WorkflowContext, build_default_executor
from lingxi.workflow.mermaid import to_mermaid
from lingxi.workflow.validator import validate_dag

log = get_logger(__name__)


def create_workflow_router(components: dict[str, Any] | None = None) -> APIRouter:
    """工厂函数：创建并返回 workflow 路由。

    用法::

        from lingxi.workflow.router import create_workflow_router
        app.include_router(create_workflow_router())

    Args:
        components: 执行引擎要用的真实组件（rule_engine / ticket_store /
            knowledge / agent / queue / assigner / audit_log）。
            缺省时 /execute 端点仍可用，但除 REPLY 外的节点都会被
            优雅降级为 skipped —— 这正是「没有外部依赖也能跑完 DAG」的要求。
    """
    router = APIRouter(prefix="/api/workflow", tags=["workflow"])

    def _check_admin(x_admin_token: str | None = Header(None)) -> None:
        """Dashboard 管理面认证（委托 core.admin_auth —— 全项目唯一实现）。"""
        require_admin(x_admin_token)

    @router.get("/graph")
    async def get_graph(
        x_admin_token: str | None = Header(None),
    ) -> Response:
        """返回默认 DAG 的 Mermaid 流程图代码。

        可直接粘贴到 mermaid.live / Notion / GitHub Markdown 渲染。
        """
        _check_admin(x_admin_token)
        dag = build_default_pipeline_dag()
        mermaid_code = to_mermaid(dag, title=dag.name)
        return Response(
            content=mermaid_code,
            media_type="text/plain; charset=utf-8",
        )

    @router.get("/graph/yaml")
    async def get_graph_yaml(
        x_admin_token: str | None = Header(None),
    ) -> Response:
        """返回默认 DAG 的 YAML 定义。

        用户可下载 → 修改 → POST /validate 校验 → POST /preview 预览。
        """
        _check_admin(x_admin_token)
        dag = build_default_pipeline_dag()
        yaml_content = to_yaml(dag)
        return Response(
            content=yaml_content,
            media_type="application/x-yaml; charset=utf-8",
        )

    @router.post("/graph/validate")
    async def post_validate(
        body: dict[str, Any],
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        """校验用户提交的 DAG (JSON body)。

        请求体是 WorkflowDAG 的 JSON 格式。
        返回 {"valid": true} 或 {"valid": false, "errors": [...]}。
        """
        _check_admin(x_admin_token)
        try:
            from lingxi.workflow.models import WorkflowDAG

            dag = WorkflowDAG(**body)
        except Exception as e:
            raise HTTPException(
                status_code=422,
                detail=f"DAG 格式错误: {e}",
            ) from e

        errors = validate_dag(dag)
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "node_count": len(dag.nodes),
            "edge_count": len(dag.edges),
            "entry_nodes": dag.entry_nodes(),
            "exit_nodes": dag.exit_nodes(),
        }

    @router.post("/graph/preview")
    async def post_preview(
        body: dict[str, Any],
        x_admin_token: str | None = Header(None),
    ) -> Response:
        """用户提交 YAML → 校验 → 返回 Mermaid 预览。

        请求体: {"yaml": "..."}
        """
        _check_admin(x_admin_token)

        yaml_str = body.get("yaml", "")
        if not yaml_str:
            raise HTTPException(
                status_code=422,
                detail="缺少 'yaml' 字段",
            )

        try:
            dag = from_yaml(yaml_str)
        except Exception as e:
            raise HTTPException(
                status_code=422,
                detail=f"YAML 解析失败: {e}",
            ) from e

        errors = validate_dag(dag)
        if errors:
            raise HTTPException(
                status_code=422,
                detail=f"DAG 校验失败: {errors}",
            )

        mermaid_code = to_mermaid(dag, title=dag.name)
        return Response(
            content=mermaid_code,
            media_type="text/plain; charset=utf-8",
        )

    @router.post("/execute")
    async def post_execute(
        body: dict[str, Any],
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        """执行一张 DAG，返回逐节点 trace 与边决策。

        请求体::

            {
              "message": "我要退款",       // 必填
              "user_id": "u1",
              "chat_id": "c1",
              "channel": "qq_private",
              "system_prompt": "...",      // 可选，不给就用空
              "yaml": "..."                // 可选，不给用默认客服 DAG
            }

        返回里 `traces[].status` 与 `edge_decisions[].reason` 是排查
        「为什么走了这条分支」的唯一依据，必须完整返回，不要裁剪。
        """
        _check_admin(x_admin_token)

        message = str(body.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=422, detail="缺少 'message' 字段")

        yaml_str = body.get("yaml")
        if yaml_str:
            try:
                dag = from_yaml(str(yaml_str))
            except ValidationError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
        else:
            dag = build_default_pipeline_dag()

        errors = validate_dag(dag)
        if errors:
            raise HTTPException(
                status_code=422, detail=f"DAG 校验失败: {errors}",
            )

        ctx = WorkflowContext(
            raw_message=message,
            user_id=str(body.get("user_id", "")),
            chat_id=str(body.get("chat_id", "")),
            channel=str(body.get("channel", "qq_private")),
            system_prompt=str(body.get("system_prompt", "")),
            memory_block=str(body.get("memory_block", "")),
        )
        executor = build_default_executor(components)
        result = await executor.run(dag, ctx, validate=False)
        log.info(
            "workflow_executed",
            dag=dag.name,
            visited=len(result.visited),
            succeeded=result.succeeded,
            duration_ms=round(result.duration_ms, 1),
        )
        return result.to_dict()

    return router


# 默认导出一个可直接 include 的 router 实例
# (向后兼容，不需要传参的场景)
router = create_workflow_router()

__all__ = ["create_workflow_router", "router"]
