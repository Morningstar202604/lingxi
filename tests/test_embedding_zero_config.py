"""Embedding 零配置可用性测试（对应"默认走 OpenAI、未配 key 静默零向量"缺口）。

背景：默认 `text-embedding-3-small` 需要 OpenAI key，本地/零配置部署下
embedding 调用失败被静默降级零向量 —— RAG 看起来能用，召回全是噪声。
现在改为跟随主 LLM provider，并把降级状态显式暴露。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lingxi.config.settings import EmbeddingConfig, LLMConfig
from lingxi.knowledge.embedder import Embedder


class TestEmbeddingConfigResolution:
    def test_ollama_follows_local_model(self) -> None:
        ec = EmbeddingConfig()
        assert ec.resolved_model("ollama") == "ollama/nomic-embed-text"

    def test_ollama_base_url_strips_v1(self) -> None:
        """实测：litellm 的 ollama embedding 带 /v1 必 404。"""
        ec = EmbeddingConfig()
        base = ec.resolved_base_url("ollama", "http://127.0.0.1:11434/v1")
        assert base == "http://127.0.0.1:11434"
        assert not base.endswith("/v1")

    def test_ollama_default_when_no_base(self) -> None:
        ec = EmbeddingConfig()
        assert ec.resolved_base_url("ollama", None) == "http://127.0.0.1:11434"

    def test_cloud_provider_uses_provider_default(self) -> None:
        ec = EmbeddingConfig()
        assert ec.resolved_model("openai") == "openai/text-embedding-3-small"
        # 云端 base_url 跟随主 LLM（OpenAI 兼容端点通常要 /v1）
        assert ec.resolved_base_url("openai", "https://api.deepseek.com/v1") == (
            "https://api.deepseek.com/v1"
        )

    def test_explicit_model_wins(self) -> None:
        ec = EmbeddingConfig(model="openai/text-embedding-3-large")
        assert ec.resolved_model("ollama") == "openai/text-embedding-3-large"

    def test_explicit_base_url_wins(self) -> None:
        ec = EmbeddingConfig(base_url="http://custom:1234")
        assert ec.resolved_base_url("ollama", "http://127.0.0.1:11434/v1") == (
            "http://custom:1234"
        )


def _settings_with(provider: str) -> Any:
    s = MagicMock()
    s.llm = LLMConfig(provider=provider, model="whatever", base_url="http://127.0.0.1:11434/v1")
    s.embedding = EmbeddingConfig()
    return s


class TestEmbedderFollowsLlmProvider:
    def test_zero_config_uses_ollama_embedding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from lingxi.config import get_settings

        real = get_settings()
        monkeypatch.setattr(real, "embedding", EmbeddingConfig(), raising=False)
        monkeypatch.setattr(real.llm, "provider", "ollama")
        e = Embedder()
        assert e._model == "ollama/nomic-embed-text"
        assert e._base_url == "http://127.0.0.1:11434"

    def test_explicit_override_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from lingxi.config import get_settings

        real = get_settings()
        monkeypatch.setattr(
            real, "embedding",
            EmbeddingConfig(model="openai/text-embedding-3-small", base_url="http://x:1"),
            raising=False,
        )
        e = Embedder()
        assert e._model == "openai/text-embedding-3-small"
        assert e._base_url == "http://x:1"


class TestDegradationIsVisible:
    @pytest.mark.asyncio
    async def test_success_clears_degraded(self) -> None:
        e = Embedder(model="openai/text-embedding-3-small", base_url="http://x:1")

        class _Item:
            embedding = [0.1, 0.2, 0.3]

        resp = MagicMock()
        resp.data = [_Item()]
        with patch("litellm.aembedding", new=AsyncMock(return_value=resp)):
            vecs = await e.embed_batch(["hi"])
        assert len(vecs[0]) == 3
        assert e.health()["degraded"] is False

    @pytest.mark.asyncio
    async def test_failure_marks_degraded_and_warns(self) -> None:
        """降级必须可观测：状态置位 + health() 里带原因。"""
        e = Embedder(model="openai/text-embedding-3-small", base_url="http://x:1")
        with patch(
            "litellm.aembedding",
            new=AsyncMock(side_effect=RuntimeError("no api key")),
        ):
            vecs = await e.embed_batch(["hi"])
        assert vecs and all(v == 0.0 for v in vecs[0]), "降级返回零向量"
        health = e.health()
        assert health["degraded"] is True
        assert "no api key" in str(health["degraded_reason"])

    @pytest.mark.asyncio
    async def test_dim_not_inferred_from_zero_vectors(self) -> None:
        """零向量不能污染维度推断（否则后续真实向量尺寸对不上）。"""
        e = Embedder(model="openai/x", base_url="http://x:1")
        with patch(
            "litellm.aembedding",
            new=AsyncMock(side_effect=RuntimeError("down")),
        ):
            await e.embed_batch(["hi"])
        assert e._dim is None, "失败路径不应把默认维度固化"


class TestSetupStatusExposesEmbedding:
    def test_setup_status_contains_embedding(self, admin_headers: dict) -> None:
        from fastapi.testclient import TestClient

        from lingxi.flow.dashboard_api import create_dashboard_app
        from lingxi.flow.queue import EscalationQueue
        from lingxi.flow.ticket import TicketStore

        client = TestClient(create_dashboard_app(TicketStore(), EscalationQueue()))
        resp = client.get("/api/setup/status", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        emb = data.get("embedding")
        assert emb, "setup 状态必须包含 embedding 段（RAG 可用性可见）"
        assert emb["model"], "应给出实际使用的 embedding 模型"
        assert "degraded" in emb
