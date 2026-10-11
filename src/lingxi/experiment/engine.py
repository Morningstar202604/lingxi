"""实验引擎：分流 → 判定 → 记录。

🔗 工程逻辑
    分流算法：hash(user_id + experiment_name) % 100 < weight
    确定性 hash 保证同一用户在同一实验的分配始终一致。

    胜率判定：
    - 当两组样本量都 ≥ min_sample_size 时计算
    - 简化 Wilson score interval 近似（z-test）判定显著性
    - 置信度 ≥ 95% 时推荐 winner

🤖 AI 避坑
    - hash 要加盐（experiment_name），否则不同实验分流会相关联
    - 不要把新用户判定进 COMPLETED 状态的实验
    - metrics 只增不改（类似审计日志原则）
    - 不要 import scipy（用纯数学近似，无额外依赖）
"""
from __future__ import annotations

import hashlib
import math
import time
from typing import Any

from lingxi.core import get_logger
from lingxi.experiment.models import (
    Assignment,
    ExperimentConfig,
    ExperimentMetric,
    ExperimentStatus,
)

log = get_logger(__name__)


class ExperimentEngine:
    """A/B 实验引擎：分流 + 指标记录 + 胜率判定。"""

    def __init__(self) -> None:
        self._experiments: dict[str, ExperimentConfig] = {}
        self._assignments: dict[tuple[str, str], Assignment] = {}  # (user_id, exp_name) -> assignment
        self._metrics: list[ExperimentMetric] = []

    # ── CRUD ──

    def register(self, config: ExperimentConfig) -> None:
        """注册一个实验。同名覆盖。"""
        self._experiments[config.name] = config
        log.info("experiment_registered", name=config.name, variants=len(config.variants))

    def get_experiment(self, name: str) -> ExperimentConfig | None:
        """获取实验配置。"""
        return self._experiments.get(name)

    def list_experiments(self) -> list[ExperimentConfig]:
        """列出所有已注册的实验。"""
        return list(self._experiments.values())

    def update_status(self, name: str, status: ExperimentStatus) -> bool:
        """更新实验状态。"""
        exp = self._experiments.get(name)
        if not exp:
            return False
        exp.status = status
        log.info("experiment_status_changed", name=name, status=status.value)
        return True

    def delete_experiment(self, name: str) -> bool:
        """删除实验及其关联的分流记录和指标。"""
        if name not in self._experiments:
            return False
        del self._experiments[name]
        self._assignments = {k: v for k, v in self._assignments.items() if k[1] != name}
        self._metrics = [m for m in self._metrics if m.experiment_name != name]
        log.info("experiment_deleted", name=name)
        return True

    # ── 分流 ──

    @staticmethod
    def _hash_bucket(user_id: str, experiment_name: str) -> int:
        """确定性哈希 -> 0-99。

        使用 MD5 + 盐（experiment_name）确保：
        1. 同一用户同一实验始终得到相同结果
        2. 不同实验之间分流互不影响
        """
        key = f"{experiment_name}:{user_id}".encode()
        return int(hashlib.md5(key).hexdigest(), 16) % 100

    def assign(self, user_id: str, experiment_name: str) -> Assignment | None:
        """为用户分配实验组（幂等：同一用户同一实验返回相同结果）。"""
        exp = self._experiments.get(experiment_name)
        if not exp or exp.status != ExperimentStatus.RUNNING:
            return None

        cache_key = (user_id, experiment_name)
        if cache_key in self._assignments:
            return self._assignments[cache_key]

        bucket = self._hash_bucket(user_id, experiment_name)
        cumulative = 0
        selected = exp.variants[0].name
        for variant in exp.variants:
            cumulative += variant.weight
            if bucket < cumulative:
                selected = variant.name
                break

        assignment = Assignment(
            user_id=user_id,
            experiment_name=experiment_name,
            variant_name=selected,
            assigned_at=time.time(),
        )
        self._assignments[cache_key] = assignment
        log.info(
            "user_assigned",
            user_id=user_id,
            experiment=experiment_name,
            variant=selected,
            bucket=bucket,
        )
        return assignment

    def get_variant_for_user(self, user_id: str, experiment_name: str) -> str:
        """获取用户的变体名（无 RUNNING 实验时返回 baseline）。"""
        exp = self._experiments.get(experiment_name)
        if not exp:
            return "baseline"
        if exp.status != ExperimentStatus.RUNNING:
            # 非 RUNNING 状态时使用第一个 variant
            return exp.variants[0].name if exp.variants else "baseline"
        assignment = self.assign(user_id, experiment_name)
        return assignment.variant_name if assignment else "baseline"

    def get_variant_overrides(self, user_id: str, experiment_name: str) -> dict[str, Any]:
        """获取用户的 persona 覆盖参数。"""
        variant_name = self.get_variant_for_user(user_id, experiment_name)
        exp = self._experiments.get(experiment_name)
        if not exp:
            return {}
        for v in exp.variants:
            if v.name == variant_name:
                return v.persona_overrides
        return {}

    # ── 指标 ──

    def record_metric(self, metric: ExperimentMetric) -> None:
        """记录一次交互指标。"""
        self._metrics.append(metric)
        log.debug(  # debug 级别避免高吞吐下日志刷屏
            "metric_recorded",
            experiment=metric.experiment_name,
            variant=metric.variant_name,
            user_id=metric.user_id,
            resolved=metric.resolved,
            escalated=metric.escalated,
        )

    def get_variant_metrics(self, experiment_name: str, variant_name: str) -> list[ExperimentMetric]:
        """获取某实验某组的所有指标。"""
        return [
            m for m in self._metrics
            if m.experiment_name == experiment_name and m.variant_name == variant_name
        ]

    def compute_variant_stats(self, experiment_name: str, variant_name: str) -> dict[str, Any]:
        """计算某组的统计量。"""
        metrics = self.get_variant_metrics(experiment_name, variant_name)
        if not metrics:
            return {
                "sample_size": 0,
                "resolution_rate": 0.0,
                "escalation_rate": 0.0,
                "avg_duration_seconds": 0.0,
                "total_messages": 0,
            }

        total = len(metrics)
        resolved = sum(1 for m in metrics if m.resolved)
        escalated = sum(1 for m in metrics if m.escalated)
        avg_duration = sum(m.duration_seconds for m in metrics) / total

        return {
            "sample_size": total,
            "resolution_rate": round(resolved / total, 4),
            "escalation_rate": round(escalated / total, 4),
            "avg_duration_seconds": round(avg_duration, 2),
            "total_messages": sum(m.message_count for m in metrics),
        }

    # ── 胜率判定 ──

    def determine_winner(self, experiment_name: str) -> dict[str, Any]:
        """判定实验 winner。返回 {winner, confidence, recommended_action}。"""
        exp = self._experiments.get(experiment_name)
        if not exp:
            return {"error": "experiment not found"}

        if len(exp.variants) < 2:
            return {"error": "need at least 2 variants"}

        baseline_name = exp.variants[0].name
        challenger_name = exp.variants[1].name

        baseline_stats = self.compute_variant_stats(experiment_name, baseline_name)
        challenger_stats = self.compute_variant_stats(experiment_name, challenger_name)

        # 检查样本量
        if (baseline_stats["sample_size"] < exp.min_sample_size
                or challenger_stats["sample_size"] < exp.min_sample_size):
            return {
                "status": "insufficient_data",
                "baseline": baseline_stats,
                "challenger": challenger_stats,
                "min_sample_size": exp.min_sample_size,
            }

        # 简化 z-test (大样本近似)
        # confidence 基于 |z| (显著性), winner 方向由 c_rate vs b_rate 决定
        b_rate = baseline_stats.get(exp.target_metric, 0.0)
        c_rate = challenger_stats.get(exp.target_metric, 0.0)
        b_n = baseline_stats["sample_size"]
        c_n = challenger_stats["sample_size"]

        if b_n == 0 or c_n == 0:
            return {
                "status": "need_more_data",
                "baseline": baseline_stats,
                "challenger": challenger_stats,
            }

        p_pool = (b_rate * b_n + c_rate * c_n) / (b_n + c_n)
        se = math.sqrt(p_pool * (1 - p_pool) * (1 / b_n + 1 / c_n))
        z = 0.0 if se == 0 else (c_rate - b_rate) / se

        # 单尾 z → confidence（用 abs(z) 判定显著性, 符号决定方向）
        confidence = self._z_to_confidence(abs(z))

        if confidence >= 0.95:
            winner = challenger_name if c_rate > b_rate else baseline_name
            action = "promote_challenger" if winner == challenger_name else "keep_baseline"
        else:
            winner = None
            action = "continue"

        return {
            "status": "ready",
            "winner": winner,
            "confidence": round(confidence, 4),
            "recommended_action": action,
            "baseline": baseline_stats,
            "challenger": challenger_stats,
            "target_metric": exp.target_metric,
        }

    # ── 胜率判定扩展：多指标联合 + Bonferroni + promotion 建议 ──

    def _compute_single_metric_z(
        self,
        experiment_name: str,
        baseline_name: str,
        challenger_name: str,
        metric_name: str,
        b_stats: dict[str, Any],
        c_stats: dict[str, Any],
    ) -> float:
        """计算单个指标的 z-score (challenger vs baseline)。

        - 比例型指标 (resolution_rate / escalation_rate) 使用 pooled proportion z-test
        - 均值型指标 (avg_duration_seconds) 使用 Welch's z-test (需原始数据算方差)
        - 指标名不在 stats 中时返回 0.0
        """
        b_n = b_stats["sample_size"]
        c_n = c_stats["sample_size"]
        if b_n == 0 or c_n == 0:
            return 0.0

        b_rate = b_stats.get(metric_name, 0.0)
        c_rate = c_stats.get(metric_name, 0.0)

        if metric_name == "avg_duration_seconds":
            # Welch's z-test for means — need raw values for variance
            b_metrics = self.get_variant_metrics(experiment_name, baseline_name)
            c_metrics = self.get_variant_metrics(experiment_name, challenger_name)
            b_durations = [m.duration_seconds for m in b_metrics]
            c_durations = [m.duration_seconds for m in c_metrics]
            b_var = ExperimentEngine._sample_variance(b_durations) if len(b_durations) > 1 else 0.0
            c_var = ExperimentEngine._sample_variance(c_durations) if len(c_durations) > 1 else 0.0
            se = math.sqrt(b_var / b_n + c_var / c_n)
        else:
            # Proportion z-test (resolution_rate / escalation_rate / generic fallback)
            p_pool = (b_rate * b_n + c_rate * c_n) / (b_n + c_n)
            se = math.sqrt(p_pool * (1 - p_pool) * (1 / b_n + 1 / c_n))

        return 0.0 if se == 0 else (c_rate - b_rate) / se

    def determine_multi_metric_winner(
        self,
        experiment_name: str,
        metric_weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """多指标联合判定。

        metric_weights 形如 {"resolution_rate": 0.5, "escalation_rate": -0.3, "avg_duration_seconds": -0.2}
        (负值表示越小越好)

        算法:
        1. 对每个指标独立计算 z-score
        2. 加权求和得 composite_score
        3. 用 Bonferroni 校正 (比较次数 = 指标数×(变体数-1))
        4. 返回 {composite_scores, winner, confidence, per_metric_z}

        默认权重: resolution_rate=0.6, escalation_rate=-0.4

        🔗 工程逻辑
            工业界 (Google EVE, Netflix) 大多用 weighted-z composite，
            比单独看每个指标更稳，不易被单指标噪声误导

        🤖 AI 避坑
            - duration 的量级和 rate 完全不同，必须先 min-max 归一化
            - 权重总和建议 = 1.0 但不强制（内部归一化）
            - 如果用户只配了一个指标，退化为单指标判定
        """
        exp = self._experiments.get(experiment_name)
        if not exp:
            return {"error": "experiment not found"}

        if len(exp.variants) < 2:
            return {"error": "need at least 2 variants"}

        baseline_name = exp.variants[0].name
        challenger_name = exp.variants[1].name

        b_stats = self.compute_variant_stats(experiment_name, baseline_name)
        c_stats = self.compute_variant_stats(experiment_name, challenger_name)

        # 检查样本量
        if (b_stats["sample_size"] < exp.min_sample_size
                or c_stats["sample_size"] < exp.min_sample_size):
            return {
                "status": "insufficient_data",
                "baseline": b_stats,
                "challenger": c_stats,
                "min_sample_size": exp.min_sample_size,
            }

        # 确定有效权重: 传入 > 实验配置 > 默认
        weights = (
            metric_weights
            if metric_weights is not None
            else (exp.metric_weights if exp.metric_weights else None)
        )
        if not weights:
            weights = {"resolution_rate": 0.6, "escalation_rate": -0.4}

        # 归一化权重使得绝对值之和 = 1.0
        total_abs_weight = sum(abs(w) for w in weights.values())
        if total_abs_weight == 0:
            return {"error": "all weights are zero"}
        normalized_weights = {k: v / total_abs_weight for k, v in weights.items()}

        # 对每个指标计算独立 z-score
        per_metric_z: dict[str, float] = {}
        for metric_name in normalized_weights:
            z = self._compute_single_metric_z(
                experiment_name, baseline_name, challenger_name,
                metric_name, b_stats, c_stats,
            )
            per_metric_z[metric_name] = round(z, 4)

        # 加权求和得 composite_score
        composite_score = sum(
            per_metric_z[m] * normalized_weights[m]
            for m in normalized_weights
        )

        # 将 composite_score 视为总体 z-score 计算原始置信度
        raw_confidence = self._z_to_confidence(abs(composite_score))

        # Bonferroni 校正
        num_metrics = len(normalized_weights)
        num_comparisons = max(num_metrics * (len(exp.variants) - 1), 1)
        bonferroni_applied = exp.bonferroni_correction and num_comparisons > 1

        if bonferroni_applied:
            raw_pvalue = 1.0 - raw_confidence
            adjusted_pvalue = min(raw_pvalue * num_comparisons, 1.0)
            confidence = 1.0 - adjusted_pvalue
        else:
            confidence = raw_confidence

        # 胜者判定: composite_score > 0 意味着 challenger 在加权意义下更优
        if confidence >= 0.95 and composite_score > 0:
            winner = challenger_name
        elif confidence >= 0.95 and composite_score < 0:
            winner = baseline_name
        else:
            winner = None

        return {
            "status": "ready",
            "winner": winner,
            "confidence": round(confidence, 4),
            "composite_score": round(composite_score, 4),
            "per_metric_z": per_metric_z,
            "weights_used": {k: round(v, 4) for k, v in normalized_weights.items()},
            "num_comparisons": num_comparisons,
            "bonferroni_applied": bonferroni_applied,
            "baseline": b_stats,
            "challenger": c_stats,
        }

    def get_promotion_recommendation(self, experiment_name: str) -> dict[str, Any]:
        """给坐席/运营看的"promote or not"决策建议。

        返回:
        {
            "recommendation": "promote_challenger" | "keep_baseline" | "continue_running",
            "reason": str,  // 人类可读的理由
            "metrics_summary": dict,  // baseline vs challenger 的各指标对比
            "statistical_power": float,  // 达到的统计功效 (0-1)
            "sample_size_recommendation": int | None,  // 还需多少样本达到 0.95
        }

        判定规则:
        - 经过 Bonferroni 校正的 confidence >= 0.95 + challenger 全面占优 → promote
        - confidence >= 0.95 + challenger 某关键指标显著下降 → keep_baseline
        - confidence < 0.95 或样本不足 → continue_running

        🔗 工程逻辑
            统计显著 != 业务显著。必须检查关键指标是否反向劣化，
            即使整体 composite 占优也不应 promote 一个降低解决率的方案。
            sample_size_recommendation 用 z-ratio 平方反推所需样本倍数。

        🤖 AI 避坑
            - "全面占优" = 所有 per_metric_z * weight > 0，而非只看 composite
            - 关键指标硬编码: resolution_rate (不可下降) / escalation_rate (不可上升)
            - statistical_power 使用 confidence 近似，而非严格 beta 计算
        """
        exp = self._experiments.get(experiment_name)
        if not exp:
            return {"error": "experiment not found"}

        if len(exp.variants) < 2:
            return {"error": "need at least 2 variants"}

        baseline_name = exp.variants[0].name
        challenger_name = exp.variants[1].name

        b_stats = self.compute_variant_stats(experiment_name, baseline_name)
        c_stats = self.compute_variant_stats(experiment_name, challenger_name)

        metrics_summary = {"baseline": b_stats, "challenger": c_stats}

        # 样本量检查
        if (b_stats["sample_size"] < exp.min_sample_size
                or c_stats["sample_size"] < exp.min_sample_size):
            return {
                "recommendation": "continue_running",
                "reason": (
                    f"样本量不足 (baseline={b_stats['sample_size']}, "
                    f"challenger={c_stats['sample_size']}, 需>={exp.min_sample_size})"
                ),
                "metrics_summary": metrics_summary,
                "statistical_power": 0.0,
                "sample_size_recommendation": exp.min_sample_size,
            }

        # 多指标或单指标判定
        if exp.multi_metric:
            result = self.determine_multi_metric_winner(experiment_name)
        else:
            result = self.determine_winner(experiment_name)

        if "error" in result:
            return {"error": result["error"]}

        if result.get("status") != "ready":
            return {
                "recommendation": "continue_running",
                "reason": f"数据状态: {result.get('status')}",
                "metrics_summary": metrics_summary,
                "statistical_power": 0.0,
                "sample_size_recommendation": None,
            }

        confidence: float = result.get("confidence", 0.0)
        statistical_power = min(confidence, 0.9999)

        # 样本量建议 (如果未达标)
        sample_size_recommendation: int | None = None
        if confidence < 0.95:
            current_z = ExperimentEngine._confidence_to_z_approx(confidence)
            target_z = 1.645  # ~95% one-sided
            # z 极小说明效应量近乎为零，无法有意义地估算所需样本
            if current_z > 1e-3:
                n_total = b_stats["sample_size"] + c_stats["sample_size"]
                scale = (target_z / current_z) ** 2
                n_needed = int(math.ceil(n_total * scale)) - n_total
                sample_size_recommendation = max(n_needed, 0)
            else:
                # 效应量过小，z-ratio 外推不可信，回退到 min_sample_size
                sample_size_recommendation = None

        # 检查关键指标是否显著回落
        key_metrics_degraded: list[str] = []

        # resolution_rate 不可显著下降
        z_res = self._compute_single_metric_z(
            experiment_name, baseline_name, challenger_name,
            "resolution_rate", b_stats, c_stats,
        )
        if z_res < -1.645:  # 单尾 p < 0.05
            key_metrics_degraded.append("resolution_rate")

        # escalation_rate 不可显著上升 (lower is better)
        z_esc = self._compute_single_metric_z(
            experiment_name, baseline_name, challenger_name,
            "escalation_rate", b_stats, c_stats,
        )
        if z_esc > 1.645:  # 显著增加升级率 = 劣化
            key_metrics_degraded.append("escalation_rate")

        # 判定逻辑
        winner = result.get("winner")
        if confidence >= 0.95 and not key_metrics_degraded:
            if winner == challenger_name:
                recommendation = "promote_challenger"
                reason = (
                    f" challenger 在置信度 {confidence:.1%} 下加权占优，"
                    f"未检测到关键指标显著回落，建议推全。"
                )
            elif winner == baseline_name:
                recommendation = "keep_baseline"
                reason = (
                    f" baseline 在置信度 {confidence:.1%} 下优于 challenger，"
                    f"建议保持 baseline。"
                )
            else:
                recommendation = "continue_running"
                reason = f"置信度 {confidence:.1%} 达到阈值但无明确方向性结论"
        elif confidence >= 0.95 and key_metrics_degraded:
            recommendation = "keep_baseline"
            degraded_str = ", ".join(key_metrics_degraded)
            reason = (
                f"虽然置信度 {confidence:.1%} 达到阈值，"
                f"但关键指标 [{degraded_str}] 显著劣化，"
                f"建议保持 baseline 并排查原因。"
            )
        else:
            recommendation = "continue_running"
            reason = (
                f"置信度 {confidence:.1%} 未达到 0.95 阈值，"
                f"建议继续收集数据。"
            )
            if sample_size_recommendation is not None:
                reason += f" 预计还需约 {sample_size_recommendation} 个样本。"

        return {
            "recommendation": recommendation,
            "reason": reason,
            "metrics_summary": metrics_summary,
            "statistical_power": round(statistical_power, 4),
            "sample_size_recommendation": sample_size_recommendation,
        }

    @staticmethod
    def _sample_variance(values: list[float]) -> float:
        """样本方差 (Bessel's correction, ddof=1)。

        values 长度 < 2 时返回 0.0。
        """
        n = len(values)
        if n < 2:
            return 0.0
        mean = sum(values) / n
        return sum((v - mean) ** 2 for v in values) / (n - 1)

    @staticmethod
    def _confidence_to_z_approx(confidence: float) -> float:
        """由置信度近似反算 |z| 值 (二分查找, 精度 1e-6)。

        用于 sample_size_recommendation 中反推所需样本量。
        """
        target = abs(confidence)
        lo, hi = 0.0, 6.0  # z=6 对应 confidence ≈ 0.999999999
        for _ in range(50):
            mid = (lo + hi) / 2.0
            if ExperimentEngine._z_to_confidence(mid) < target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    @staticmethod
    def _z_to_confidence(z: float) -> float:
        """简化 z-score -> 置信度（单尾，正态分布 CDF 近似）。

        使用 Abramowitz & Stegun 的误差函数近似（最大误差 1.5e-7）。
        """
        a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
        p = 0.3275911
        sign = 1 if z >= 0 else -1
        z_abs = abs(z) / math.sqrt(2)
        t = 1.0 / (1.0 + p * z_abs)
        y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-z_abs * z_abs)
        return 0.5 * (1.0 + sign * y)

    @staticmethod
    def _bonferroni_correction(z_scores: list[float], num_comparisons: int) -> list[float]:
        """对一组 z-score 做 Bonferroni 校正，返回校正后的置信度列表。

        校正后的置信度 = 原始置信度 / num_comparisons
        当同时跑 N 组比较时，阈值从 0.95 变成 0.95/N 级别

        🔗 工程逻辑
            不造轮子: 直接用已有 _z_to_confidence 算单组置信度，
            Bonferroni 只是缩放因子 k = num_comparisons
            (实际 p_value = p * k，但 p_value <= 1，故截断)

        🤖 AI 避坑
            - Bonferroni 非常保守 (保守度 O(n))，但 A/B 实验很少 > 5 组
            - 不要做 Holm-Bonferroni——Bonferroni 已够简单可解释
            - 必须明确告知用户"此置信度已经过 N 倍校正"
        """
        if num_comparisons <= 1:
            # 只有一组比较时无需校正
            return [ExperimentEngine._z_to_confidence(z) for z in z_scores]

        corrected: list[float] = []
        for z in z_scores:
            raw_confidence = ExperimentEngine._z_to_confidence(z)
            raw_pvalue = 1.0 - raw_confidence
            # Bonferroni: p_adjusted = min(p_raw * k, 1.0)
            adjusted_pvalue = min(raw_pvalue * num_comparisons, 1.0)
            corrected.append(1.0 - adjusted_pvalue)

        return corrected

    # ── 序列化（供 dashboard API 使用）──

    def dashboard_summary(self) -> list[dict[str, Any]]:
        """返回所有实验的 dashboard 友好摘要。"""
        result: list[dict[str, Any]] = []
        for exp in self._experiments.values():
            variants_data = []
            for v in exp.variants:
                stats = self.compute_variant_stats(exp.name, v.name)
                variants_data.append({"name": v.name, **stats})
            winner_info = self.determine_winner(exp.name)
            result.append({
                "name": exp.name,
                "description": exp.description,
                "status": exp.status.value,
                "variants": variants_data,
                "target_metric": exp.target_metric,
                "winner_analysis": winner_info,
                "total_assignments": sum(
                    1 for a in self._assignments.values() if a.experiment_name == exp.name
                ),
            })
        return result


__all__ = ["ExperimentEngine"]
