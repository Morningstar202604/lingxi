"""A/B 实验框架 — 多版本 Persona 配置的分流管理与胜率判定。

🔗 工程逻辑
    提供三核心能力：
    1. 实验注册与生命周期管理（draft -> running -> paused -> completed）
    2. 基于确定性 hash 的用户分流（同一用户始终落入同一组）
    3. 简化 z-test 胜率判定（置信度 >= 95% 推荐 winner）

    典型使用流程::

        from lingxi.experiment import ExperimentEngine, ExperimentConfig, VariantConfig, VariantType

        engine = ExperimentEngine()
        engine.register(ExperimentConfig(
            name="greetings_v2",
            variants=[
                VariantConfig(name="baseline", variant_type=VariantType.BASELINE, weight=50),
                VariantConfig(name="new_prompt", variant_type=VariantType.CHALLENGER, weight=50,
                              persona_overrides={"greeting": "您好！有什么可以帮助您？"}),
            ],
        ))
        engine.update_status("greetings_v2", ExperimentStatus.RUNNING)

        # 在 pipeline 中查询用户分组
        variant = engine.get_variant_for_user(user_id="wxid_abc123", experiment_name="greetings_v2")
        overrides = engine.get_variant_overrides(user_id="wxid_abc123", experiment_name="greetings_v2")

🤖 AI 避坑
    - engine 是进程内存存储，重启后数据丢失（后续需持久化到 DB）
    - 不要在生产环境手动改权重或重启后修改实验配置
    - min_sample_size 过小会导致假阳性（建议 >= 100）
"""
from lingxi.experiment.engine import ExperimentEngine
from lingxi.experiment.models import (
    ExperimentConfig,
    ExperimentStatus,
    VariantConfig,
    VariantType,
)
from lingxi.experiment.router import create_experiment_router

__all__ = [
    "ExperimentEngine",
    "ExperimentConfig",
    "ExperimentStatus",
    "VariantConfig",
    "VariantType",
    "create_experiment_router",
]
