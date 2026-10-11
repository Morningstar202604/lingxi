"""Knowledge 统一服务 — 协调切分 + 嵌入 + 召回 + CRUD。

🔗 工程逻辑
    单一入口 KnowledgeService, 被 Dashboard API 和 Agent Pipeline 共同使用。
    Document → RecursiveSplitter.split() → VectorStore.add_chunks() → 持久化。

🤖 AI 避坑
    - 文档上传不要阻塞主流程 (切分+嵌入可能耗时数秒)
    - 同一 doc_id 重复上传 = upsert (覆盖更新)
    - 不要把所有 chunks 一次性 embed (分批避免 OOM)
"""
from __future__ import annotations

import uuid
from typing import Any

from lingxi.config import get_settings
from lingxi.core import get_logger
from lingxi.knowledge.embedder import Embedder
from lingxi.knowledge.splitter import RecursiveSplitter
from lingxi.knowledge.vector_store import VectorStore

log = get_logger(__name__)


class KnowledgeService:
    """知识库服务: upload / recall / delete / list / reset。"""

    def __init__(
        self,
        splitter: RecursiveSplitter | None = None,
        store: VectorStore | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        cfg = get_settings()
        ec = cfg.embedding
        kc = cfg.knowledge
        self._splitter = splitter or RecursiveSplitter(
            chunk_size=ec.chunk_size,
            chunk_overlap=ec.chunk_overlap,
        )
        self._embedder = embedder or Embedder(
            model=ec.model,
            base_url=ec.base_url or "",
            api_key=(ec.api_key.get_secret_value() if ec.api_key else ""),
        )
        self._store = store or VectorStore(
            collection_name=kc.collection,
            persist_dir=str(cfg.data_dir / kc.persist_dir.replace("./", "")),
            embedder=self._embedder,
        )

    # ── 上传 (doc_id 可选, 不传自动生成) ──

    async def upload_document(
        self,
        content: str,
        *,
        doc_id: str = "",
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """上传文档: 切分 → 嵌入 → 写入 ChromaDB。"""
        if not content or not content.strip():
            raise ValueError("document content is empty")

        doc_id = doc_id or f"doc_{uuid.uuid4().hex[:12]}"
        source = source or doc_id

        # 切分
        chunks = self._splitter.split(content, source=source)
        if metadata:
            for chunk in chunks:
                chunk.metadata.update(metadata)

        if not chunks:
            log.warning("split_produced_no_chunks", doc_id=doc_id, source=source)
            return {"doc_id": doc_id, "chunk_count": 0, "source": source}

        # 嵌入 + 写入
        count = await self._store.add_chunks(chunks, doc_id=doc_id)
        log.info("knowledge_uploaded", doc_id=doc_id, chunks=count, source=source)

        return {"doc_id": doc_id, "chunk_count": count, "source": source}

    # ── 召回 ──

    async def recall(
        self,
        query: str,
        *,
        top_k: int = 5,
        score_threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """语义召回相关 chunks (带 citation 溯源信息)。"""
        if not query.strip():
            return []
        return await self._store.recall(query, top_k=top_k, score_threshold=score_threshold)

    async def recall_hybrid(
        self,
        query: str,
        top_k: int = 5,
        mode: str = "hybrid",
    ) -> list[dict[str, Any]]:
        """客户端入口: 自动选择召回模式。

        mode:
        - "vector": 纯向量召回 (向后兼容)
        - "bm25": 纯关键词召回
        - "hybrid": RRF 融合 (默认, 推荐)
        """
        if not query.strip():
            return []
        return await self._store.recall(query, top_k=top_k, mode=mode)

    async def build_context(
        self,
        query: str,
        *,
        top_k: int = 5,
        score_threshold: float = 0.3,
        max_chars: int = 3000,
    ) -> str:
        """召回并格式化为 Agent system prompt 注入文本。

        格式:
          ## 知识库参考
          [1] (source:xxx, score:0.82)
          文本内容...
          [2] (source:yyy, score:0.71)
          ...
        """
        hits = await self.recall(query, top_k=top_k, score_threshold=score_threshold)
        if not hits:
            return ""

        sections: list[str] = ["## 知识库参考"]
        total_chars = 0
        for i, hit in enumerate(hits, 1):
            entry = f"[{i}] (source:{hit.get('source', 'unknown')}, score:{hit['score']})\n{hit['text']}"
            if total_chars + len(entry) > max_chars:
                break
            total_chars += len(entry)
            sections.append(entry)

        return "\n\n".join(sections)

    # ── CRUD / Stats ──

    async def delete_document(self, doc_id: str) -> int:
        return self._store.delete_doc(doc_id)

    async def list_documents(self) -> list[dict[str, str]]:
        return self._store.list_docs()

    async def stats(self) -> dict[str, Any]:
        return self._store.stats()

    async def reset(self) -> None:
        self._store.reset()

    @property
    def splitter(self) -> RecursiveSplitter:
        return self._splitter

    @property
    def store(self) -> VectorStore:
        return self._store


# 全局单例
_default_service: KnowledgeService | None = None


async def get_knowledge_service() -> KnowledgeService:
    global _default_service
    if _default_service is None:
        _default_service = KnowledgeService()
    return _default_service
