"""Embedding 调用层 — 通过 litellm.aembedding 统一调用。

🔗 工程逻辑
    复用 litellm 作为统一 LLM/Embedding API 代理, 与项目其他组件一致。
    litellm 支持 OpenAI/Baidu/Qianwen/Voyage/Cohere/HuggingFace 等 100+ 嵌入模型。

    - 自动从 llm.model 提取 provider (openai/text-embedding-3-small 等)
    - 支持批量 embedding (单次最多 100 条, 超量自动分 batch)
    - 异步调用 + 超时链路
"""
from __future__ import annotations

import asyncio

from lingxi.config import get_settings
from lingxi.core import get_logger

log = get_logger(__name__)


class Embedder:
    """基于 litellm.aembedding 的嵌入器。"""

    def __init__(
        self,
        model: str = "",
        base_url: str = "",
        api_key: str = "",
        timeout: float = 10.0,
        max_batch: int = 64,
    ) -> None:
        cfg = get_settings()
        ec = cfg.embedding
        self._model = model or ec.model
        self._base_url = base_url or ec.base_url or ""
        self._api_key = api_key or (ec.api_key.get_secret_value() if ec.api_key else "")
        self._timeout = timeout
        self._max_batch = max_batch
        # 复用项目现有 litellm 实例
        self._dim: int | None = None

    async def embed_one(self, text: str) -> list[float]:
        """单条文本嵌入。"""
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入 (自动分批, 超时保护)。"""
        if not texts:
            return []

        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), self._max_batch):
            batch = texts[i:i + self._max_batch]
            try:
                import litellm
                resp = await asyncio.wait_for(
                    litellm.aembedding(
                        model=self._model,
                        input=batch,
                        api_base=self._base_url or None,
                        api_key=self._api_key or None,
                    ),
                    timeout=self._timeout,
                )
                embeddings = [item.embedding for item in resp.data]
                all_embeddings.extend(embeddings)
            except Exception as e:
                log.warning("embedding_batch_failed", batch_size=len(batch), error=str(e))
                # 降级: 返回零向量避免崩溃
                dim = len(all_embeddings[0]) if all_embeddings else self.dim
                all_embeddings.extend([[0.0] * dim] * len(batch))

        # 缓存首次成功 embedding 的实际维度
        if all_embeddings and self._dim is None:
            self._dim = len(all_embeddings[0])

        return all_embeddings

    @property
    def dim(self) -> int:
        """向量维度 (首条嵌入后推断)。"""
        return self._dim or 1536


_default_embedder: Embedder | None = None


def get_default_embedder() -> Embedder:
    """全局默认 Embedder 单例。"""
    global _default_embedder
    if _default_embedder is None:
        _default_embedder = Embedder()
    return _default_embedder
