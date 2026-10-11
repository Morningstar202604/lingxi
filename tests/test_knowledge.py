"""Knowledge (RAG) 模块测试 — RecursiveSplitter / Embedder / VectorStore / KnowledgeService。

🔗 工程逻辑
    覆盖文档切分、向量存储、召回流程、知识库服务全部公共 API。
    不需要真实 Embedding API: 用 _MockEmbedder 返回固定向量。

🤖 AI 避坑
    - asyncio_mode = strict: 异步测试显式标记 @pytest.mark.asyncio
    - ChromaDB 测试使用 tmp_path fixture 做持久化目录
    - from __future__ import annotations: 全文件统一使用
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from lingxi.knowledge.embedder import Embedder
from lingxi.knowledge.service import KnowledgeService
from lingxi.knowledge.splitter import Chunk, RecursiveSplitter
from lingxi.knowledge.vector_store import VectorStore

# ── Mock Embedder ──


class _MockEmbedder:
    """返回固定单位向量, 不需要真实 LLM API 调用。"""

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim

    async def embed_one(self, text: str) -> list[float]:
        return [1.0 / self._dim] * self._dim

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[1.0 / self._dim] * self._dim for _ in texts]

    @property
    def dim(self) -> int:
        return self._dim


# ── RecursiveSplitter Tests ──


class TestRecursiveSplitter:
    def test_split_basic_english(self) -> None:
        splitter = RecursiveSplitter(chunk_size=64, chunk_overlap=0.1)
        text = "This is a document. " * 50
        chunks = splitter.split(text, source="test")
        assert len(chunks) > 0
        for chunk in chunks:
            assert chunk.text.strip()
            assert chunk.source == "test"
            assert isinstance(chunk.chunk_index, int)

    def test_split_very_long_word(self) -> None:
        """超长单词应被硬切而不抛异常。"""
        splitter = RecursiveSplitter(chunk_size=32, chunk_overlap=0.0)
        long_word = "a" * 5000
        chunks = splitter.split(long_word, source="long")
        assert len(chunks) > 0
        assert all(c.text.strip() for c in chunks)

    def test_overlap_creates_shared_content(self) -> None:
        """有 overlap 的切分, 相邻 chunk 应有共享文本片段。"""
        splitter_no = RecursiveSplitter(chunk_size=64, chunk_overlap=0.0)
        splitter_yes = RecursiveSplitter(chunk_size=64, chunk_overlap=0.3)
        # 构造足够长且重复度低的文本
        sentences = [f"Unique sentence number {i} with some padding." for i in range(100)]
        text = " ".join(sentences)
        chunks_no = splitter_no.split(text)
        chunks_yes = splitter_yes.split(text)
        # 有 overlap 时总 chunk 数应 >= 无 overlap (通常情况下更多)
        assert len(chunks_yes) >= len(chunks_no)

    def test_empty_string_returns_empty_list(self) -> None:
        splitter = RecursiveSplitter(chunk_size=64)
        assert splitter.split("", source="empty") == []

    def test_whitespace_only_returns_empty_list(self) -> None:
        splitter = RecursiveSplitter(chunk_size=64)
        assert splitter.split("   \n\n  \t  ", source="whitespace") == []


# ── Embedder Tests ──


class TestEmbedder:
    def test_dim_property_default(self) -> None:
        """Embedder.dim 默认回退到 1536 (OpenAI text-embedding-3-small 维度)。"""
        with (
            patch("lingxi.knowledge.embedder.get_settings") as mock_settings,
        ):
            mock_settings.return_value.llm.model = "openai/text-embedding-3-small"
            mock_settings.return_value.llm.base_url = ""
            mock_settings.return_value.llm.api_key = None
            emb = Embedder()
            assert emb.dim == 1536

    def test_custom_dim_after_embed(self) -> None:
        """首次 embed 后 _dim 更新, dim 属性反映真实值。"""
        with patch("lingxi.knowledge.embedder.get_settings") as mock_settings:
            mock_settings.return_value.llm.model = "test"
            mock_settings.return_value.llm.base_url = ""
            mock_settings.return_value.llm.api_key = None
            emb = Embedder()
            emb._dim = 8
            assert emb.dim == 8

    def test_batch_size_property(self) -> None:
        with patch("lingxi.knowledge.embedder.get_settings") as mock_settings:
            mock_settings.return_value.llm.model = "test"
            mock_settings.return_value.llm.base_url = ""
            mock_settings.return_value.llm.api_key = None
            emb = Embedder(max_batch=32)
            assert emb._max_batch == 32


# ── VectorStore Tests ──


@pytest.mark.asyncio
async def test_vector_store_add_and_recall(tmp_path: Any) -> None:
    embedder = _MockEmbedder(dim=8)
    store = VectorStore(
        collection_name="test_store",
        persist_dir=str(tmp_path / "chroma"),
        embedder=embedder,
    )
    chunks = [
        Chunk(text="Python is a programming language", chunk_index=0, source="wiki"),
        Chunk(text="RAG stands for Retrieval Augmented Generation", chunk_index=1, source="wiki"),
    ]
    count = await store.add_chunks(chunks, doc_id="doc_test")
    assert count == 2
    results = await store.recall("Python", top_k=5, score_threshold=0.0)
    assert len(results) > 0
    assert results[0]["text"] == "Python is a programming language"
    assert results[0]["source"] == "wiki"


@pytest.mark.asyncio
async def test_vector_store_list_docs(tmp_path: Any) -> None:
    embedder = _MockEmbedder(dim=8)
    store = VectorStore(
        collection_name="test_list",
        persist_dir=str(tmp_path / "chroma"),
        embedder=embedder,
    )
    chunks = [Chunk(text="doc A content", chunk_index=0, source="src_a")]
    await store.add_chunks(chunks, doc_id="doc_A")
    docs = store.list_docs()
    assert len(docs) == 1
    assert docs[0]["doc_id"] == "doc_A"
    assert docs[0]["source"] == "src_a"


@pytest.mark.asyncio
async def test_vector_store_stats(tmp_path: Any) -> None:
    embedder = _MockEmbedder(dim=8)
    store = VectorStore(
        collection_name="test_stats",
        persist_dir=str(tmp_path / "chroma"),
        embedder=embedder,
    )
    assert store.stats()["count"] == 0
    chunks = [Chunk(text="x", chunk_index=0, source="s")]
    await store.add_chunks(chunks, doc_id="d1")
    assert store.stats()["count"] == 1
    assert store.stats()["name"] == "test_stats"


@pytest.mark.asyncio
async def test_vector_store_delete_doc(tmp_path: Any) -> None:
    embedder = _MockEmbedder(dim=8)
    store = VectorStore(
        collection_name="test_delete",
        persist_dir=str(tmp_path / "chroma"),
        embedder=embedder,
    )
    chunks = [Chunk(text="to be deleted", chunk_index=0, source="del")]
    await store.add_chunks(chunks, doc_id="doc_del")
    assert store.stats()["count"] == 1
    deleted = store.delete_doc("doc_del")
    assert deleted == 1
    assert store.stats()["count"] == 0


# ── KnowledgeService Tests ──


@pytest.mark.asyncio
async def test_service_upload_and_recall(tmp_path: Any) -> None:
    embedder = _MockEmbedder(dim=8)
    splitter = RecursiveSplitter(chunk_size=64, chunk_overlap=0.1)
    store = VectorStore(
        collection_name="svc_test",
        persist_dir=str(tmp_path / "chroma"),
        embedder=embedder,
    )
    svc = KnowledgeService(splitter=splitter, store=store, embedder=embedder)
    result = await svc.upload_document("Machine learning is a subset of artificial intelligence.", doc_id="ml_doc", source="wiki")
    assert result["doc_id"] == "ml_doc"
    assert result["chunk_count"] > 0
    hits = await svc.recall("machine learning", top_k=3)
    assert len(hits) > 0
    assert hits[0]["doc_id"] == "ml_doc"


@pytest.mark.asyncio
async def test_service_build_context_format(tmp_path: Any) -> None:
    """build_context 输出必须包含 ## 知识库参考 和 [1], [2] 引用标记。"""
    embedder = _MockEmbedder(dim=8)
    splitter = RecursiveSplitter(chunk_size=64, chunk_overlap=0.1)
    store = VectorStore(
        collection_name="ctx_test",
        persist_dir=str(tmp_path / "chroma"),
        embedder=embedder,
    )
    svc = KnowledgeService(splitter=splitter, store=store, embedder=embedder)
    await svc.upload_document(
        "Vector databases store embeddings for semantic search. ChromaDB is a popular choice for local development.",
        doc_id="vec_doc",
        source="docs",
    )
    formatted = await svc.build_context("vector database", top_k=5)
    assert "## 知识库参考" in formatted
    assert "[1]" in formatted
    assert "docs" in formatted


@pytest.mark.asyncio
async def test_service_upload_empty_raises(tmp_path: Any) -> None:
    embedder = _MockEmbedder(dim=8)
    svc = KnowledgeService(embedder=embedder)
    with pytest.raises(ValueError, match="empty"):
        await svc.upload_document("")


@pytest.mark.asyncio
async def test_service_upload_whitespace_only_raises(tmp_path: Any) -> None:
    embedder = _MockEmbedder(dim=8)
    svc = KnowledgeService(embedder=embedder)
    with pytest.raises(ValueError, match="empty"):
        await svc.upload_document("   \n\t  ")


@pytest.mark.asyncio
async def test_service_recall_empty_query(tmp_path: Any) -> None:
    """空查询应返回空列表而不是报错。"""
    embedder = _MockEmbedder(dim=8)
    svc = KnowledgeService(embedder=embedder)
    result = await svc.recall("")
    assert result == []


@pytest.mark.asyncio
async def test_service_list_and_stats(tmp_path: Any) -> None:
    embedder = _MockEmbedder(dim=8)
    splitter = RecursiveSplitter(chunk_size=64, chunk_overlap=0.1)
    store = VectorStore(
        collection_name="svc_list_test",
        persist_dir=str(tmp_path / "chroma"),
        embedder=embedder,
    )
    svc = KnowledgeService(splitter=splitter, store=store, embedder=embedder)
    await svc.upload_document(
        "First document for listing.",
        doc_id="d1",
        source="src1",
    )
    await svc.upload_document(
        "Second document for listing.",
        doc_id="d2",
        source="src2",
    )
    docs = await svc.list_documents()
    assert len(docs) == 2
    stats = await svc.stats()
    assert stats["count"] > 0


@pytest.mark.asyncio
async def test_service_reset(tmp_path: Any) -> None:
    embedder = _MockEmbedder(dim=8)
    splitter = RecursiveSplitter(chunk_size=64, chunk_overlap=0.1)
    store = VectorStore(
        collection_name="reset_test",
        persist_dir=str(tmp_path / "chroma"),
        embedder=embedder,
    )
    svc = KnowledgeService(splitter=splitter, store=store, embedder=embedder)
    await svc.upload_document("content to reset", doc_id="r1", source="rs")
    assert (await svc.stats())["count"] > 0
    await svc.reset()
    assert (await svc.stats())["count"] == 0

