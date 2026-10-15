"""ChromaDB 向量存储 — 本地持久化 + BM25 + RRF 混合检索。

🔗 工程逻辑
    用 ChromaDB (Python-native 嵌入式向量库) 做持久化,
    无需外部向量服务 (Milvus/Qdrant/Weaviate 需要独立部署, 复杂度高)。

    - 每个 collection 对应一个知识库 (默认 "zhixi_default")
    - metadata 存 source/chunk_index, citation 溯源用
    - 支持 upsert (同 doc_id 覆盖) 和 delete
    - 相似度用 cosine (ChromaDB 默认)
    - 混合检索: 向量召回 (语义理解) + BM25 (关键词精确命中)
      + RRF 融合 (取长补短)

🤖 AI 避坑
    - ChromaDB 不是线程安全的, 单进程 asyncio 场景用 awaitable 包装
    - 不要在每个请求都 new PersistentClient, 应该复用一个 client
    - embedding_function 不传, 我们自己 embeddings 传入 (更灵活控制维度)
    - BM25 内存索引在 add_chunks 时同步构建, delete_doc/reset 时同步清理
    - RRF 不需要归一化分数, 只用排名 — 这天然解决了向量分量和 BM25 分量量纲不一致
"""
from __future__ import annotations

import contextlib
import re
import uuid
from typing import Any

from lingxi.core import get_logger
from lingxi.knowledge.embedder import Embedder, get_default_embedder

log = get_logger(__name__)


def _tokenize(text: str) -> list[str]:
    """中英文混合分词 (字符级 + 双字 bigram)。

    - 英文: 按空白/标点切分, 保留长度 >= 1 的 token
    - 中文: 滑动窗口 bigram (相邻两字一组)
    - 混合: 合并两类 token

    不使用 jieba (避免安装依赖), 用字符级 bigram 覆盖中文;
    英文按空格/标点切分已足够精确。
    """
    tokens: list[str] = []
    # 英文 token: 按非字母数字中文切分
    for word in re.findall(r"[a-zA-Z0-9]+", text.lower()):
        tokens.append(word)
    # 中文 bigram: 滑动窗口
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", text)
    for i in range(len(chinese_chars) - 1):
        tokens.append(chinese_chars[i] + chinese_chars[i + 1])
    # 单字兜底 (只有一个中文字符时)
    if len(chinese_chars) == 1:
        tokens.append(chinese_chars[0])
    return tokens


class BM25Index:
    """BM25 关键词召回（**官方 `rank_bm25` 实现**，我们只提供中文分词器）。

    🔗 工程逻辑
        BM25 的公式实现（IDF 平滑、k1/b 调参、文档长度归一化）交给
        `rank_bm25.BM25Okapi` —— 这是被广泛使用、有测试覆盖的实现。
        我们保留的只有**领域适配**：中文用字符级 bigram 分词
        （`_tokenize`），因为通用 tokenizer 对中文无效。

        向量召回 + BM25 取长补短：
        - 向量: 语义理解（"快递丢了" ≈ "包裹不见了"）
        - BM25: 关键词精确命中（"SF12345678"、"订单20240115"）

    🤖 AI 避坑
        - rank_bm25 在**构造时**吃整个 corpus（不像我们旧实现支持增量
          add/remove），所以维护一份 `_docs` 列表，变更后重建索引；
          倒排重建是 O(n) 且只发生在写入/删除时，检索路径零重建。
        - 文档数为 0 或查询无 token 时直接返回 []（官方实现对空 corpus
          的 `get_scores` 会抛 ZeroDivisionError）。
        - 旧实现自己算 IDF/TF；替换后索引语义等价，但**分数尺度会变**
          （rank_bm25 的 epsilon/平滑细节不同）—— 排序结果一致，
          不要拿绝对分数做阈值判断。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._docs: dict[str, str] = {}  # doc_id → text
        self._ids: list[str] = []        # 与 _corpus 同序（rank_bm25 按下标返回）
        self._corpus: list[list[str]] = []
        self._bm25: Any = None

    def _rebuild(self) -> None:
        """按当前 _docs 重建官方索引（仅在增删后调用）。"""
        if not self._docs:
            self._ids = []
            self._corpus = []
            self._bm25 = None
            return
        self._ids = list(self._docs.keys())
        self._corpus = [_tokenize(self._docs[i]) for i in self._ids]
        try:
            from rank_bm25 import BM25Okapi

            self._bm25 = BM25Okapi(self._corpus, k1=self._k1, b=self._b)
        except ImportError:  # pragma: no cover — 可选依赖缺失时退化为无关键词召回
            log.warning("rank_bm25_missing", hint="pip install rank-bm25")
            self._bm25 = None

    def add_document(self, doc_id: str, text: str) -> None:
        """添加文档（幂等：同 doc_id 覆盖）。"""
        if not text.strip():
            return
        self._docs[doc_id] = text
        self._rebuild()

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """BM25 搜索，返回 [(doc_id, score)]，按分数降序。"""
        if self._bm25 is None or not self._docs or not query.strip():
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        try:
            scores = self._bm25.get_scores(query_tokens)
        except (ZeroDivisionError, ValueError) as e:
            log.warning("bm25_scoring_failed", error=str(e))
            return []
        ranked = [
            (self._ids[i], float(score))
            for i, score in enumerate(scores)
            if score > 0
        ]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    def remove_document(self, doc_id: str) -> None:
        """移除文档并重建索引。"""
        if doc_id not in self._docs:
            return
        del self._docs[doc_id]
        self._rebuild()

    def reset(self) -> None:
        """清空索引。"""
        self._docs.clear()
        self._rebuild()

    @property
    def doc_count(self) -> int:
        return len(self._docs)


def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[str, float]]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """RRF 融合多路召回结果。

    score = Σ (1 / (k + rank_i))

    k=60 是微软/Bing 默认值, 能有效抑制浅层相关。
    不做归一化 — 不同召回源的分数天然不需要可比,
    只靠排名就能体现相关性。
    """
    fused: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (doc_id, _) in enumerate(ranked, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda x: x[1], reverse=True)


class VectorStore:
    """ChromaDB 封装, 管理一个知识库 collection (向量 + BM25 + RRF 混合)。"""

    def __init__(
        self,
        collection_name: str = "zhixi_default",
        persist_dir: str = "./data/chroma",
        embedder: Embedder | None = None,
    ) -> None:
        self._name = collection_name
        self._persist_dir = persist_dir
        self._embedder = embedder or get_default_embedder()
        self._client: Any = None
        self._collection: Any = None
        self._bm25 = BM25Index()
        # chunk_id → {text, source, chunk_index, doc_id} 用于 RRF 融合结果反查
        self._chunk_map: dict[str, dict[str, Any]] = {}

    def _get_client(self) -> Any:
        if self._client is None:
            import chromadb
            self._client = chromadb.PersistentClient(path=self._persist_dir)
        return self._client

    def _get_collection(self) -> Any:
        if self._collection is None:
            client = self._get_client()
            # 不传 embedding_function: 我们自己管理向量
            try:
                self._collection = client.get_or_create_collection(
                    name=self._name,
                    metadata={"hnsw:space": "cosine"},
                )
            except Exception:
                # ChromaDB 旧版 API 兼容
                self._collection = client.get_or_create_collection(name=self._name)
        return self._collection

    async def add_chunks(self, chunks: list[Any], *, doc_id: str = "") -> int:
        """嵌入 + 写入 chunks + 同步 BM25, 返回写入条数。"""
        if not chunks:
            return 0

        # Chunk 对象 → text/source/index 提取
        texts = [c.text for c in chunks]
        sources = [c.source for c in chunks]
        chunk_indices = [c.chunk_index for c in chunks]

        embeddings = await self._embedder.embed_batch(texts)

        ids = [
            f"{doc_id}_{chunk_indices[i]}_{uuid.uuid4().hex[:8]}"
            for i in range(len(texts))
        ]
        metadatas = [
            {
                "source": sources[i],
                "chunk_index": chunk_indices[i],
                "doc_id": doc_id,
            }
            for i in range(len(texts))
        ]

        coll = self._get_collection()
        coll.upsert(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)

        # 同步 BM25 索引
        for i, chunk_id in enumerate(ids):
            self._bm25.add_document(chunk_id, texts[i])
            self._chunk_map[chunk_id] = {
                "text": texts[i],
                "source": sources[i],
                "chunk_index": chunk_indices[i],
                "doc_id": doc_id,
            }
        return len(texts)

    async def recall(
        self,
        query: str,
        top_k: int = 5,
        score_threshold: float = 0.3,
        mode: str = "vector",
        bm25_weight: float = 0.3,
    ) -> list[dict[str, Any]]:
        """检索召回 (支持 vector / bm25 / hybrid 三种模式)。

        模式:
        - "vector": 纯向量召回 (默认, 向后兼容)
        - "bm25": 纯 BM25 召回
        - "hybrid": RRF 融合向量 + BM25

        bm25_weight 仅在概念上控制融合,"hybrid" 模式下 RRF 不直接使用
        该权重, 而是通过排名融合自动平衡两路召回的贡献。
        该参数保留用于未来扩展 (加权 RRF 变体)。
        """
        if mode == "vector":
            return await self._recall_vector(query, top_k, score_threshold)
        if mode == "bm25":
            return self._recall_bm25(query, top_k)
        if mode == "hybrid":
            return await self._recall_hybrid(query, top_k, score_threshold)
        raise ValueError(f"unknown recall mode: {mode!r} (expected 'vector'|'bm25'|'hybrid')")

    async def _recall_vector(
        self,
        query: str,
        top_k: int,
        score_threshold: float,
    ) -> list[dict[str, Any]]:
        """纯向量召回 (原逻辑)。"""
        query_vec = await self._embedder.embed_one(query)
        coll = self._get_collection()
        # hybrid 场景需要更多候选做融合, 但 vector 模式保持精确 top_k
        result = coll.query(query_embeddings=[query_vec], n_results=top_k)

        if not result["ids"] or not result["ids"][0]:
            return []

        hits: list[dict[str, Any]] = []
        for i in range(len(result["ids"][0])):
            dist = result["distances"][0][i] if result["distances"] else 1.0
            similarity = 1.0 - dist
            if similarity < score_threshold:
                continue
            meta = (result["metadatas"][0][i] if result["metadatas"] else {}) or {}
            text = result["documents"][0][i] if result["documents"] else ""
            hits.append({
                "text": text,
                "source": meta.get("source", ""),
                "chunk_index": meta.get("chunk_index", 0),
                "doc_id": meta.get("doc_id", ""),
                "score": round(similarity, 4),
                "recall_mode": "vector",
            })
        return hits

    def _recall_bm25(
        self,
        query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """纯 BM25 召回。"""
        bm25_results = self._bm25.search(query, top_k=top_k)
        hits: list[dict[str, Any]] = []
        for chunk_id, score in bm25_results:
            info = self._chunk_map.get(chunk_id, {})
            hits.append({
                "text": info.get("text", ""),
                "source": info.get("source", ""),
                "chunk_index": info.get("chunk_index", 0),
                "doc_id": info.get("doc_id", ""),
                "score": round(score, 4),
                "recall_mode": "bm25",
            })
        return hits

    async def _recall_hybrid(
        self,
        query: str,
        top_k: int,
        score_threshold: float,
    ) -> list[dict[str, Any]]:
        """RRF 混合召回: 向量 + BM25 融合。"""
        # 取更多候选用于融合 (RRF 后排 rank 会重新聚合)
        fetch_k = max(top_k * 3, 15)

        # 1. 向量召回 — 直接用 ChromaDB 拿到真实 chunk_id
        query_vec = await self._embedder.embed_one(query)
        coll = self._get_collection()
        result = coll.query(query_embeddings=[query_vec], n_results=fetch_k)

        vector_ranked: list[tuple[str, float]] = []
        vec_meta: dict[str, dict[str, Any]] = {}
        if result["ids"] and result["ids"][0]:
            for i in range(len(result["ids"][0])):
                dist = result["distances"][0][i] if result["distances"] else 1.0
                similarity = 1.0 - dist
                if similarity < score_threshold:
                    continue
                chunk_id = result["ids"][0][i]
                meta = (result["metadatas"][0][i] if result["metadatas"] else {}) or {}
                text = result["documents"][0][i] if result["documents"] else ""
                vector_ranked.append((chunk_id, similarity))
                vec_meta[chunk_id] = {
                    "text": text,
                    "source": meta.get("source", ""),
                    "chunk_index": meta.get("chunk_index", 0),
                    "doc_id": meta.get("doc_id", ""),
                }

        # 2. BM25 召回 — chunk_id 就是 BM25 的 doc_id
        bm25_ranked = self._bm25.search(query, top_k=fetch_k)

        # 3. RRF 融合 (纯 rank-based, 不需要归一化分)
        fused = reciprocal_rank_fusion([vector_ranked, bm25_ranked])

        # 4. 组装结果 (优先从 ChromaDB metadata, 否则从 chunk_map)
        hits: list[dict[str, Any]] = []
        for chunk_id, rrf_score in fused[:top_k]:
            info = vec_meta.get(chunk_id) or self._chunk_map.get(chunk_id)
            if info is None:
                continue
            hits.append({
                "text": info.get("text", ""),
                "source": info.get("source", ""),
                "chunk_index": info.get("chunk_index", 0),
                "doc_id": info.get("doc_id", ""),
                "score": round(rrf_score, 4),
                "recall_mode": "hybrid",
            })
        return hits

    def delete_doc(self, doc_id: str) -> int:
        """删除指定 doc_id 的所有 chunks (同步 BM25)。"""
        coll = self._get_collection()
        result = coll.get(where={"doc_id": doc_id})
        ids = result.get("ids", [])
        if ids:
            coll.delete(ids=ids)
            # 同步清理 BM25
            for chunk_id in ids:
                self._bm25.remove_document(chunk_id)
                self._chunk_map.pop(chunk_id, None)
        return len(ids)

    def list_docs(self) -> list[dict[str, str]]:
        """列出所有文档 ID + sample metadata。"""
        coll = self._get_collection()
        all_data = coll.get()
        if not all_data or not all_data.get("ids"):
            return []
        seen: dict[str, dict[str, str]] = {}
        for _, meta in enumerate(all_data.get("metadatas", [])):
            if meta and "doc_id" in meta:
                did = meta["doc_id"]
                if did not in seen:
                    seen[did] = {"doc_id": did, "source": meta.get("source", "")}
        return list(seen.values())

    def stats(self) -> dict[str, Any]:
        """Collection 统计。"""
        coll = self._get_collection()
        return {
            "name": self._name,
            "count": coll.count(),
        }

    def reset(self) -> None:
        """清空 collection + BM25 (危险操作, 仅 CLI/API 管理员)。"""
        client = self._get_client()
        with contextlib.suppress(Exception):
            client.delete_collection(self._name)
        self._collection = None
        self._bm25.reset()
        self._chunk_map.clear()
