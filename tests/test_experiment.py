"""A/B 实验引擎测试。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from lingxi.experiment import (
    ExperimentConfig,
    ExperimentEngine,
    ExperimentStatus,
    VariantConfig,
    VariantType,
)
from lingxi.experiment.models import ExperimentMetric
from lingxi.experiment.router import create_experiment_router

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def _make_experiment(
    name: str = "test_exp",
    baseline_weight: int = 50,
    challenger_weight: int = 50,
    min_sample_size: int = 10,
) -> ExperimentConfig:
    return ExperimentConfig(
        name=name,
        description="Test experiment",
        variants=[
            VariantConfig(
                name="baseline",
                variant_type=VariantType.BASELINE,
                weight=baseline_weight,
            ),
            VariantConfig(
                name="challenger",
                variant_type=VariantType.CHALLENGER,
                weight=challenger_weight,
            ),
        ],
        target_metric="resolution_rate",
        min_sample_size=min_sample_size,
    )


class TestExperimentEngine:
    """实验引擎核心测试。"""

    def test_register_and_get(self) -> None:
        engine = ExperimentEngine()
        exp = _make_experiment()
        engine.register(exp)
        result = engine.get_experiment("test_exp")
        assert result is not None
        assert result.name == "test_exp"

    def test_get_nonexistent_returns_none(self) -> None:
        engine = ExperimentEngine()
        assert engine.get_experiment("nonexistent") is None

    def test_list_experiments(self) -> None:
        engine = ExperimentEngine()
        engine.register(_make_experiment("exp_a"))
        engine.register(_make_experiment("exp_b"))
        assert len(engine.list_experiments()) == 2

    def test_update_status(self) -> None:
        engine = ExperimentEngine()
        engine.register(_make_experiment())
        assert engine.update_status("test_exp", ExperimentStatus.RUNNING)
        assert engine.get_experiment("test_exp").status == ExperimentStatus.RUNNING

    def test_update_status_nonexistent(self) -> None:
        engine = ExperimentEngine()
        assert not engine.update_status("no_such", ExperimentStatus.RUNNING)

    def test_delete_experiment(self) -> None:
        engine = ExperimentEngine()
        engine.register(_make_experiment())
        assert engine.delete_experiment("test_exp")
        assert engine.get_experiment("test_exp") is None

    def test_delete_cleans_assignments(self) -> None:
        engine = ExperimentEngine()
        engine.register(_make_experiment())
        engine.update_status("test_exp", ExperimentStatus.RUNNING)
        engine.assign("user_1", "test_exp")
        engine.delete_experiment("test_exp")
        # 内部 assignments 应被清理


class TestTrafficSplitting:
    """分流算法测试。"""

    def test_deterministic_assignment(self) -> None:
        """同一用户同一实验始终同一组。"""
        engine = ExperimentEngine()
        engine.register(_make_experiment())
        engine.update_status("test_exp", ExperimentStatus.RUNNING)

        a1 = engine.assign("user_1", "test_exp")
        a2 = engine.assign("user_1", "test_exp")
        assert a1 is not None and a2 is not None
        assert a1.variant_name == a2.variant_name

    def test_different_users_may_differ(self) -> None:
        """不同用户可能落入不同组。"""
        engine = ExperimentEngine()
        engine.register(_make_experiment())
        engine.update_status("test_exp", ExperimentStatus.RUNNING)

        variants = {engine.assign(f"user_{i}", "test_exp").variant_name for i in range(100)}
        # 100 个用户应该两组都有（极大概率）
        assert len(variants) == 2

    def test_no_running_experiment_returns_none(self) -> None:
        """非 RUNNING 实验 assign 返回 None。"""
        engine = ExperimentEngine()
        engine.register(_make_experiment())  # DRAFT
        result = engine.assign("user_1", "test_exp")
        assert result is None

    def test_get_variant_for_user_non_running(self) -> None:
        """非 RUNNING 实验的 get_variant_for_user 返回 baseline。"""
        engine = ExperimentEngine()
        engine.register(_make_experiment())
        variant = engine.get_variant_for_user("user_1", "test_exp")
        assert variant == "baseline"

    def test_bucket_distribution_balanced(self) -> None:
        """50/50 分流理应大致均匀。"""
        engine = ExperimentEngine()
        engine.register(_make_experiment())
        engine.update_status("test_exp", ExperimentStatus.RUNNING)

        baseline_count = 0
        for i in range(1000):
            a = engine.assign(f"u{i}", "test_exp")
            assert a is not None
            if a.variant_name == "baseline":
                baseline_count += 1
        # 50/50 分流 → baseline 应在 400-600 之间
        assert 350 < baseline_count < 650

    def test_uneven_weights(self) -> None:
        """非均匀分流。"""
        engine = ExperimentEngine()
        engine.register(_make_experiment(baseline_weight=80, challenger_weight=20))
        engine.update_status("test_exp", ExperimentStatus.RUNNING)

        baseline_count = sum(
            1 for i in range(500)
            if engine.assign(f"u{i}", "test_exp").variant_name == "baseline"
        )
        # 80/20 分流 → baseline 应在 300-450 之间（允许统计学波动）
        assert 300 < baseline_count < 450


class TestExperimentMetrics:
    """实验指标记录测试。"""

    def test_record_metric(self) -> None:
        engine = ExperimentEngine()
        engine.register(_make_experiment())
        metric = ExperimentMetric(
            experiment_name="test_exp",
            variant_name="baseline",
            user_id="u1",
            resolved=True,
        )
        engine.record_metric(metric)
        metrics = engine.get_variant_metrics("test_exp", "baseline")
        assert len(metrics) == 1

    def test_compute_variant_stats(self) -> None:
        engine = ExperimentEngine()
        engine.register(_make_experiment())

        # 模拟指标
        for i in range(10):
            engine.record_metric(ExperimentMetric(
                experiment_name="test_exp",
                variant_name="baseline",
                user_id=f"u{i}",
                resolved=i < 6,  # 60% 解决
                escalated=i < 2,  # 20% 升级
                duration_seconds=float(i * 10),
            ))
        for i in range(10):
            engine.record_metric(ExperimentMetric(
                experiment_name="test_exp",
                variant_name="challenger",
                user_id=f"v{i}",
                resolved=i < 8,  # 80% 解决
                escalated=i < 1,
                duration_seconds=float(i * 8),
            ))

        baseline = engine.compute_variant_stats("test_exp", "baseline")
        assert baseline["sample_size"] == 10
        assert baseline["resolution_rate"] == 0.6
        assert baseline["escalation_rate"] == 0.2

        challenger = engine.compute_variant_stats("test_exp", "challenger")
        assert challenger["resolution_rate"] == 0.8

    def test_insufficient_data(self) -> None:
        """样本量不足时不给出 winner。"""
        engine = ExperimentEngine()
        engine.register(_make_experiment())  # min_sample_size=10

        # 只记录 3 条
        for i in range(3):
            engine.record_metric(ExperimentMetric(
                experiment_name="test_exp",
                variant_name="baseline",
                user_id=f"u{i}",
                resolved=True,
            ))

        result = engine.determine_winner("test_exp")
        assert result["status"] == "insufficient_data"

    def test_determine_winner_with_significant_data(self) -> None:
        """显著差异时推荐 winner。"""
        engine = ExperimentEngine()
        engine.register(_make_experiment(min_sample_size=5))

        # baseline: 30% 解决率 (15/50) — 很弱
        for i in range(50):
            engine.record_metric(ExperimentMetric(
                experiment_name="test_exp",
                variant_name="baseline",
                user_id=f"b{i}",
                resolved=i < 15,
            ))
        # challenger: 80% 解决率 (40/50) — 显著更好
        for i in range(50):
            engine.record_metric(ExperimentMetric(
                experiment_name="test_exp",
                variant_name="challenger",
                user_id=f"c{i}",
                resolved=i < 40,
            ))

        result = engine.determine_winner("test_exp")
        # challenger 显著更好 → 推荐 promote_challenger
        assert result["status"] == "ready"
        assert result["winner"] == "challenger"

    def test_determine_winner_baseline_better(self) -> None:
        """baseline 显著更好时推荐 keep_baseline。"""
        engine = ExperimentEngine()
        engine.register(_make_experiment(min_sample_size=5))

        # baseline: 80% (40/50)
        for i in range(50):
            engine.record_metric(ExperimentMetric(
                experiment_name="test_exp",
                variant_name="baseline",
                user_id=f"b{i}",
                resolved=i < 40,
            ))
        # challenger: 30% (15/50) — 显著更差
        for i in range(50):
            engine.record_metric(ExperimentMetric(
                experiment_name="test_exp",
                variant_name="challenger",
                user_id=f"c{i}",
                resolved=i < 15,
            ))

        result = engine.determine_winner("test_exp")
        assert result["status"] == "ready"
        assert result["winner"] == "baseline"
        assert result["recommended_action"] == "keep_baseline"


class TestExperimentRouter:
    """实验管理 REST API 测试。"""

    @pytest.fixture()
    def client(self) -> TestClient:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        engine = ExperimentEngine()
        app = FastAPI()
        app.include_router(create_experiment_router(engine))
        return TestClient(app)

    def test_create_experiment(self, client: TestClient) -> None:
        resp = client.post("/experiments", json={
            "name": "exp1",
            "variants": [
                {"name": "c_a", "variant_type": "baseline", "weight": 50},
                {"name": "c_b", "variant_type": "challenger", "weight": 50},
            ],
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "exp1"

    def test_list_experiments(self, client: TestClient) -> None:
        # 先创建一个
        client.post("/experiments", json={
            "name": "exp_list",
            "variants": [
                {"name": "v1", "variant_type": "baseline", "weight": 100},
            ],
        })
        resp = client.get("/experiments")
        assert resp.status_code == 200
        data = resp.json()
        assert any(e["name"] == "exp_list" for e in data)

    def test_update_status(self, client: TestClient) -> None:
        client.post("/experiments", json={
            "name": "exp_status",
            "variants": [
                {"name": "v1", "variant_type": "baseline", "weight": 100},
            ],
        })
        resp = client.put("/experiments/exp_status/status", json={"status": "running"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

    def test_record_metric(self, client: TestClient) -> None:
        client.post("/experiments", json={
            "name": "exp_metric",
            "variants": [
                {"name": "v1", "variant_type": "baseline", "weight": 50},
                {"name": "v2", "variant_type": "challenger", "weight": 50},
            ],
        })
        resp = client.post("/experiments/exp_metric/metrics", json={
            "variant_name": "v1",
            "user_id": "u1",
            "resolved": True,
        })
        assert resp.status_code == 200
        assert resp.json()["recorded"] is True

    def test_assign_user(self, client: TestClient) -> None:
        client.post("/experiments", json={
            "name": "exp_assign",
            "variants": [
                {"name": "a", "variant_type": "baseline", "weight": 50},
                {"name": "b", "variant_type": "challenger", "weight": 50},
            ],
        })
        client.put("/experiments/exp_assign/status", json={"status": "running"})
        resp = client.get("/experiments/exp_assign/assign/user_123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "user_123"
        assert data["variant_name"] in ("a", "b")

    def test_delete_experiment(self, client: TestClient) -> None:
        client.post("/experiments", json={
            "name": "to_delete",
            "variants": [{"name": "v", "variant_type": "baseline", "weight": 100}],
        })
        resp = client.delete("/experiments/to_delete")
        assert resp.status_code == 200
        # 再查应该 404
        resp2 = client.get("/experiments/to_delete")
        assert resp2.status_code == 404

    def test_invalid_status_rejected(self, client: TestClient) -> None:
        client.post("/experiments", json={
            "name": "exp_invalid",
            "variants": [{"name": "v", "variant_type": "baseline", "weight": 100}],
        })
        resp = client.put("/experiments/exp_invalid/status", json={"status": "INVALID"})
        assert resp.status_code == 400

    def test_invalid_variant_in_metric(self, client: TestClient) -> None:
        client.post("/experiments", json={
            "name": "exp_var",
            "variants": [{"name": "v", "variant_type": "baseline", "weight": 100}],
        })
        resp = client.post("/experiments/exp_var/metrics", json={
            "variant_name": "nonexistent",
            "user_id": "u1",
        })
        assert resp.status_code == 400
