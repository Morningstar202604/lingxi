"""Phase 1 RAG 向量知识库 — Knowledge 管理 + 向量召回。

🔗 工程逻辑
    Document → RecursiveSplitter(chunk_size, chunk_overlap) →
    Embedding (litellm.aembedding) → ChromaDB 持久化 → 语义召回 →
    注入 Agent system prompt (带 citation 溯源)。

    不重复造轮子: ChromaDB 向量库、litellm embedding 统一调用、tiktoken 准确计数。
"""
from lingxi.knowledge.embedder import Embedder, get_default_embedder
from lingxi.knowledge.service import KnowledgeService
from lingxi.knowledge.splitter import Chunk, RecursiveSplitter

__all__ = [
    "Chunk",
    "Embedder",
    "KnowledgeService",
    "RecursiveSplitter",
    "get_default_embedder",
]
