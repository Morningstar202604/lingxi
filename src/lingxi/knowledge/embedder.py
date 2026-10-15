"""Embedding 调用层 — 通过 litellm.aembedding 统一调用。

🔗 工程逻辑
    复用 litellm 作为统一 LLM/Embedding API 代理, 与项目其他组件一致。
    litellm 支持 OpenAI/Baidu/Qianwen/Voyage/Cohere/HuggingFace 等 100+ 嵌入模型。

    **零配置可用**：未显式配置时跟随主 LLM 的 provider —— ollama（默认）
    就用本地 nomic-embed-text，云端就用那家的 embedding 端点。
    见 EmbeddingConfig.resolved_model / resolved_base_url。

    - 支持批量 embedding (单次最多 100 条, 超量自动分 batch)
    - 异步调用 + 超时链路
    - **失败不静默**：降级为零向量的同时记录 degraded 状态，
      通过 health() 暴露（/api/setup/status 会展示）

🤖 AI 避坑
    - litellm 的 ollama embedding 必须用**原生 base_url**（不带 /v1），
      带 /v1 一律 404（实测 2026-10-15）。
    - 响应可能是对象也可能是 dict（不同 provider/版本），两种都要读。
    - 零向量降级会让 RAG 召回退化成噪声 —— 必须可观测，不能悄悄发生。
"""
from __future__ import annotations

import asyncio

from lingxi.config import get_settings
from lingxi.core import get_logger

log = get_logger(__name__)


def _embedding_of(item: object) -> list[float]:
    """从 litellm 响应元素里取向量（兼容对象与 dict 两种形态）。"""
    value = getattr(item, "embedding", None)
    if value is None and isinstance(item, dict):
        value = item.get("embedding")
    return list(value or [])


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
        llm = cfg.llm
        # 零配置：未显式给定 model/base_url 时跟随主 LLM provider 推导
        self._model = model or ec.resolved_model(llm.provider)
        self._base_url = base_url or ec.resolved_base_url(llm.provider, llm.base_url)
        self._api_key = api_key or (ec.api_key.get_secret_value() if ec.api_key else "")
        self._timeout = timeout
        self._max_batch = max_batch
        self._dim: int | None = None
        # 降级状态：发生零向量降级时置位，供 /api/setup/status 展示
        self._degraded_reason: str | None = None

    async def embed_one(self, text: str) -> list[float]:
        """单条文本嵌入。"""
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入 (自动分批, 超时保护)。

        provider 调用失败时**降级为零向量**（保持调用方可用），
        但记录 degraded 原因并打 warning —— 不再静默。
        """
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
                embeddings = [_embedding_of(item) for item in resp.data]
                self._degraded_reason = None  # 成功即清除降级状态
                all_embeddings.extend(embeddings)
            except Exception as e:
                self._degraded_reason = f"{type(e).__name__}: {e}"
                log.warning(
                    "embedding_batch_degraded_to_zero_vectors",
                    batch_size=len(batch),
                    model=self._model,
                    base_url=self._base_url or "(default)",
                    error=str(e)[:200],
                    hint="RAG 召回会退化为噪声；检查 embedding 配置或本地模型服务",
                )
                dim = len(all_embeddings[0]) if all_embeddings else self.dim
                all_embeddings.extend([[0.0] * dim] * len(batch))

        # 缓存首次**成功** embedding 的实际维度（零向量不算成功）
        first = all_embeddings[0] if all_embeddings else []
        if first and any(v != 0.0 for v in first) and self._dim is None:
            self._dim = len(first)

        return all_embeddings

    @property
    def dim(self) -> int:
        """向量维度 (首条嵌入后推断)。"""
        return self._dim or 1536

    def health(self) -> dict[str, object]:
        """当前 embedding 状态（供 /api/setup/status 与排障）。"""
        return {
            "model": self._model,
            "base_url": self._base_url,
            "degraded": self._degraded_reason is not None,
            "degraded_reason": self._degraded_reason or "",
        }


_default_embedder: Embedder | None = None


def get_default_embedder() -> Embedder:
    """全局默认 Embedder 单例。"""
    global _default_embedder
    if _default_embedder is None:
        _default_embedder = Embedder()
    return _default_embedder


def reset_default_embedder() -> None:
    """重置单例（配置变更 / 测试用）。"""
    global _default_embedder
    _default_embedder = None


__all__ = ["Embedder", "get_default_embedder", "reset_default_embedder"]
