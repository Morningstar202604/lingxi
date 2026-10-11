"""实验管理 REST API 端点。

🔗 工程逻辑
    为 Dashboard/前端提供实验 CRUD + 分流查询 + 指标录入的统一接口。
    FastAPI Router 模式，可被 mount 到主应用或独立使用。

🤖 AI 避坑
    - 不要在前端 URL 暴露实验的内部状态机细节（status 字段枚举即可）
    - POST /metrics 是高吞吐接口，必须严格校验 variant_name 属于对应实验
    - 分流查询 `assign/{user_id}` 被 pipeline 调用，必须快（<1ms）
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from lingxi.core import get_logger
from lingxi.experiment.models import (
    ExperimentConfig,
    ExperimentMetric,
    ExperimentStatus,
)

if TYPE_CHECKING:
    from lingxi.experiment.engine import ExperimentEngine

log = get_logger(__name__)


def create_experiment_router(engine: ExperimentEngine) -> APIRouter:
    """创建实验管理 Router。

    用法::

        from lingxi.experiment.router import create_experiment_router
        engine = ExperimentEngine()
        app.include_router(create_experiment_router(engine), prefix="/api")
    """
    router = APIRouter(tags=["experiments"])

    @router.get("/experiments")
    async def list_experiments() -> list[dict[str, Any]]:
        """列出所有实验。"""
        experiments = engine.list_experiments()
        return [
            {
                "name": exp.name,
                "description": exp.description,
                "status": exp.status.value,
                "variants": [
                    {
                        "name": v.name,
                        "variant_type": v.variant_type.value,
                        "weight": v.weight,
                        "persona_overrides": v.persona_overrides,
                    }
                    for v in exp.variants
                ],
                "target_metric": exp.target_metric,
                "min_sample_size": exp.min_sample_size,
                "created_at": exp.created_at,
                "tenant_id": exp.tenant_id,
            }
            for exp in experiments
        ]

    @router.post("/experiments")
    async def create_experiment(config: ExperimentConfig) -> dict[str, Any]:
        """创建实验。同名覆盖。"""
        # 校验 variant weights
        if config.variants:
            total_weight = sum(v.weight for v in config.variants)
            if total_weight <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Sum of variant weights must be > 0",
                )
        engine.register(config)
        log.info("experiment_created_via_api", name=config.name)
        return {"name": config.name, "status": "registered"}

    @router.put("/experiments/{name}/status")
    async def update_experiment_status(name: str, body: dict[str, Any]) -> dict[str, Any]:
        """更新实验状态。

        Body: {"status": "running" | "paused" | "completed"}
        """
        status_str = body.get("status")
        if not status_str:
            raise HTTPException(status_code=400, detail="Missing 'status' field")

        try:
            new_status = ExperimentStatus(status_str)
        except ValueError:
            valid = [s.value for s in ExperimentStatus]
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{status_str}'. Valid: {valid}",
            ) from None

        if not engine.update_status(name, new_status):
            raise HTTPException(status_code=404, detail=f"Experiment '{name}' not found")

        return {"name": name, "status": new_status.value}

    @router.delete("/experiments/{name}")
    async def delete_experiment(name: str) -> dict[str, str]:
        """删除实验及其关联数据。"""
        if not engine.delete_experiment(name):
            raise HTTPException(status_code=404, detail=f"Experiment '{name}' not found")
        return {"deleted": name}

    @router.get("/experiments/{name}")
    async def get_experiment(name: str) -> dict[str, Any]:
        """获取单个实验详情（含统计和 winner 分析）。"""
        exp = engine.get_experiment(name)
        if not exp:
            raise HTTPException(status_code=404, detail=f"Experiment '{name}' not found")

        variants_data = []
        for v in exp.variants:
            stats = engine.compute_variant_stats(name, v.name)
            variants_data.append({
                "name": v.name,
                "variant_type": v.variant_type.value,
                "weight": v.weight,
                "persona_overrides": v.persona_overrides,
                **stats,
            })

        return {
            "name": exp.name,
            "description": exp.description,
            "status": exp.status.value,
            "variants": variants_data,
            "target_metric": exp.target_metric,
            "min_sample_size": exp.min_sample_size,
            "winner_analysis": engine.determine_winner(name),
            "total_assignments": sum(
                1
                for a_key in _assignment_keys(engine)
                if a_key[1] == name
            ),
        }

    @router.get("/experiments/{name}/assign/{user_id}")
    async def assign_user(name: str, user_id: str) -> dict[str, Any]:
        """查询用户分组（幂等）。"""
        exp = engine.get_experiment(name)
        if not exp:
            raise HTTPException(status_code=404, detail=f"Experiment '{name}' not found")

        assignment = engine.assign(user_id, name)
        if assignment is None:
            # 实验不在 RUNNING 状态 → 使用默认 variant
            variant_name = engine.get_variant_for_user(user_id, name)
            return {
                "user_id": user_id,
                "experiment_name": name,
                "variant_name": variant_name,
                "active": False,
            }

        return {
            "user_id": assignment.user_id,
            "experiment_name": assignment.experiment_name,
            "variant_name": assignment.variant_name,
            "assigned_at": assignment.assigned_at,
            "active": True,
            "persona_overrides": engine.get_variant_overrides(user_id, name),
        }

    @router.post("/experiments/{name}/metrics")
    async def record_metric(name: str, body: dict[str, Any]) -> dict[str, Any]:
        """记录实验指标。

        Body 字段（全部可选，有默认值）:
        - variant_name: str (required)
        - user_id: str (required)
        - resolved: bool = False
        - escalated: bool = False
        - duration_seconds: float = 0.0
        - message_count: int = 0
        """
        exp = engine.get_experiment(name)
        if not exp:
            raise HTTPException(status_code=404, detail=f"Experiment '{name}' not found")

        variant_name = body.get("variant_name")
        if not variant_name:
            raise HTTPException(status_code=400, detail="Missing 'variant_name' field")

        # 校验 variant_name 属于该实验
        valid_variants = {v.name for v in exp.variants}
        if variant_name not in valid_variants:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Variant '{variant_name}' not in experiment. "
                    f"Valid: {sorted(valid_variants)}"
                ),
            )

        user_id = body.get("user_id", "")
        if not user_id:
            raise HTTPException(status_code=400, detail="Missing 'user_id' field")

        metric = ExperimentMetric(
            experiment_name=name,
            variant_name=variant_name,
            user_id=user_id,
            resolved=body.get("resolved", False),
            escalated=body.get("escalated", False),
            duration_seconds=body.get("duration_seconds", 0.0),
            message_count=body.get("message_count", 0),
        )
        engine.record_metric(metric)
        return {"recorded": True, "variant": variant_name, "user_id": user_id}

    return router


def _assignment_keys(engine: ExperimentEngine) -> list[tuple[str, str]]:
    """获取引擎中所有 assignment 的 (user_id, experiment_name) 键列表。

    用于 router 中的统计查询。通过 _assignments 内部结构访问。
    """
    return list(engine._assignments.keys())  # noqa: SLF001 — router 是 engine 的协作者


__all__ = ["create_experiment_router"]
