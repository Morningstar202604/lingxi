"""实验数据模型。

🔗 工程逻辑
    A/B 实验三要素：分组（variant）、分流（assignment）、指标（metric）。
    分流用 user_id 取 hash 模 100 → 确保同一用户始终落入同一组。

🤖 AI 避坑
    - 不要用 random/random.random() 做分流（不同进程结果不同）
    - 实验一旦开始就不要改分流比例（避免辛普森悖论）
    - baseline/challenger 的两组样本量要大致相等
"""
from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class VariantType(StrEnum):
    """实验组类型。"""

    BASELINE = "baseline"
    CHALLENGER = "challenger"


class ExperimentStatus(StrEnum):
    """实验生命周期状态。"""

    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


class VariantConfig(BaseModel):
    """实验组配置。"""

    name: str  # "baseline" / "challenger_a" / "challenger_b"
    variant_type: VariantType
    weight: int = 50  # 分流权重 (0-100)
    persona_overrides: dict[str, Any] = Field(default_factory=dict)  # 覆盖 persona 参数


class ExperimentConfig(BaseModel):
    """实验配置。"""

    name: str  # 实验名，如 "greetings_v2"
    description: str = ""
    status: ExperimentStatus = ExperimentStatus.DRAFT
    variants: list[VariantConfig]  # 通常 2 个（baseline + challenger）
    target_metric: str = "resolution_rate"  # resolution_rate / escalation_rate / csat
    min_sample_size: int = 30  # 每组最小样本量
    created_at: float = Field(default_factory=time.time)
    tenant_id: str = "default"
    # ── 多指标联合判定 ──
    multi_metric: bool = False  # 是否启用多指标联合判定
    metric_weights: dict[str, float] = Field(default_factory=dict)  # 多指标权重
    bonferroni_correction: bool = True  # 是否启用 Bonferroni 校正


class Assignment(BaseModel):
    """用户分组记录。"""

    user_id: str
    experiment_name: str
    variant_name: str
    assigned_at: float = Field(default_factory=time.time)


class ExperimentMetric(BaseModel):
    """单次交互的指标记录。"""

    experiment_name: str
    variant_name: str
    user_id: str
    resolved: bool = False  # 是否最终解决
    escalated: bool = False  # 是否升级
    duration_seconds: float = 0.0  # 交互持续时长
    message_count: int = 0  # 消息数
    timestamp: float = Field(default_factory=time.time)


__all__ = [
    "VariantType",
    "ExperimentStatus",
    "VariantConfig",
    "ExperimentConfig",
    "Assignment",
    "ExperimentMetric",
]
