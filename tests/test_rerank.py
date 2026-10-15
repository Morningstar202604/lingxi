"""rerank（cross-encoder 重排）行为测试。

对应缺口：混合召回（向量+BM25+RRF）此前**没有重排** —— 召回能捞到，
但排序质量取决于融合分数，语义相关但措辞不同的文档常被排后。
rerank 是 RAG 质量最高性价比的单点提升（业界共识）。
"""
from __future__ import annotations

from typing import Any

import pytest

from lingxi.knowledge.rerank import rerank, rerank_status, reset_for_tests


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


class TestRerankBehaviour:
    @pytest.mark.asyncio
    async def test_empty_candidates(self) -> None:
        assert await rerank("q", [], top_k=5) == []

    @pytest.mark.asyncio
    async def test_disabled_returns_original_order(self) -> None:
        cands = [{"text": "a", "score": 0.9}, {"text": "b", "score": 0.5}]
        out = await rerank("q", cands, top_k=2, enabled=False)
        assert [c["text"] for c in out] == ["a", "b"]
        assert all("reranked" not in c for c in out)

    @pytest.mark.asyncio
    async def test_single_candidate_skips_model(self) -> None:
        """单候选没有重排意义，不该白跑一次 cross-encoder。"""
        out = await rerank("q", [{"text": "only", "score": 1.0}], top_k=1)
        assert len(out) == 1
        assert "reranked" not in out[0]

    @pytest.mark.asyncio
    async def test_top_k_respected(self) -> None:
        cands = [{"text": f"doc {i}", "score": 1.0 - i * 0.1} for i in range(5)]
        out = await rerank("doc", cands, top_k=2)
        assert len(out) <= 2

    @pytest.mark.asyncio
    async def test_failure_degrades_not_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模型不可用/推理失败必须降级原序（检索可用性优先），不抛异常。"""
        import lingxi.knowledge.rerank as rr

        def _boom(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("onnx missing")

        monkeypatch.setattr(rr, "_sync_rerank", _boom)
        cands = [{"text": "a", "score": 0.9}, {"text": "b", "score": 0.5}]
        out = await rr.rerank("q", cands, top_k=2)
        assert [c["text"] for c in out] == ["a", "b"], "失败时必须原序返回"

    def test_status_shape(self) -> None:
        st = rerank_status()
        assert set(st) >= {"loaded", "load_failed_reason"}


@pytest.mark.skipif(
    not __import__("pathlib").Path("data/rerank_models/ms-marco-MiniLM-L-12-v2").exists(),
    reason="rerank 模型未预置（离线环境跳过真实推理）",
)
class TestRealRerankModel:
    """真实模型推理：语义相关文档必须被提到前面。"""

    @pytest.mark.asyncio
    async def test_relevant_doc_moves_to_top(self) -> None:
        cands = [
            {"text": "今天天气不错适合出去玩", "score": 0.9},  # 融合分最高但不相关
            {"text": "退货政策：签收后7天内可无理由退货", "score": 0.5},
            {"text": "退款将在3个工作日内原路返回", "score": 0.4},
        ]
        out = await rerank("退货要多久到账", cands, top_k=2)
        assert out, "rerank 不应返回空"
        assert "退货" in out[0]["text"] or "退款" in out[0]["text"], (
            f"语义相关的文档应被提到首位，实际首位: {out[0]['text']}"
        )
        assert out[0].get("reranked") is True

    @pytest.mark.asyncio
    async def test_scores_are_cross_encoder_range(self) -> None:
        cands = [
            {"text": "退货政策说明", "score": 0.5},
            {"text": "完全无关的内容", "score": 0.4},
        ]
        out = await rerank("退货", cands, top_k=2)
        for h in out:
            if h.get("reranked"):
                assert 0.0 <= float(h["score"]) <= 1.0
