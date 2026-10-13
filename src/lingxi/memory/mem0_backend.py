"""Mem0 语义记忆后端（自动事实抽取 + 向量召回 + 内置图谱）。

🔗 工程逻辑
    与内置 SQLite 记忆管线实现同一套公开行为面：
    - encode_and_store: Mem0 自动抽取事实并做向量编码
      （本项目不再自己调 LLM 抽事实，交给 Mem0 抽取管线）
    - recall: 向量语义召回（"措辞不同但同义"的字面管线兜不住的场景）
    - forget / list_user_memories / update_fact / contradict: 维护面

    Mem0 Memory 是同步 API（内部阻塞网络调用），本项目主循环是 asyncio ——
    所有方法用 asyncio.to_thread 桥过去，不阻塞事件循环。

    依赖本地 Ollama 服务的 embedding + LLM（provider=ollama）：
    服务不可达时 from_config 即 fail-fast（Memory 构造时验证连接），
    不静默降级成内置 n-gram（那是"配了 Mem0 却零语义能力"的坑）。

🤖 AI 避坑
    - 同一 user_id 的记忆按 user 隔离，与内置管线的 user 隔离语义一致
    - mem0 各版本 search 返回值形状不一（dict({"results":[...]}) 或 list），
      统一在 _results() 归一化，不直接碰版本细节
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from lingxi.core import get_logger
from lingxi.memory.pipeline import MemoryItem, MemoryRecallResult

log = get_logger(__name__)


def _build_mem0_config(
    *,
    collection: str,
    path: str,
    history_db: str,
    base_url: str,
    embed_model: str,
    llm_model: str,
    api_key: str | None = None,
) -> dict[str, Any]:
    """构造 mem0 Memory 配置 dict（Ollama embedding + LLM + 本地 Chroma）。"""
    # mem0 的 ollama 客户端用原生 API 根（自己拼 /api/tags、/api/embed），
    # 传 OpenAI 兼容的 /v1 前缀会 404 —— 这里统一去掉尾缀与 /v1。
    native_base = base_url.rstrip("/")
    if native_base.endswith("/v1"):
        native_base = native_base[:-3]

    cfg: dict[str, Any] = {
        "llm": {
            "provider": "ollama",
            "config": {"model": llm_model, "ollama_base_url": native_base},
        },
        "embedder": {
            "provider": "ollama",
            "config": {
                "model": embed_model,
                "ollama_base_url": native_base,
                "embedding_dims": 512,
            },
        },
        "vector_store": {
            "provider": "chroma",
            "config": {"collection_name": collection, "path": path},
        },
        "history_db_path": history_db,
    }
    if api_key:
        cfg["embedder"]["config"]["api_key"] = api_key
    return cfg


def _results(payload: Any) -> list[dict[str, Any]]:
    """归一化 mem0 各版本返回值：dict({"results":[...]}) | list | None。"""
    if payload is None:
        return []
    if isinstance(payload, dict):
        out = payload.get("results")
        return out if isinstance(out, list) else []
    if isinstance(payload, list):
        return payload
    return []


class Mem0MemoryPipeline:
    """Mem0 后端：与内置 MemoryPipeline 同面的记忆管线（异步主循环安全）。"""

    def __init__(self, memory: Any, settings: Any, data_dir: Any) -> None:
        self._mem = memory
        self._settings = settings
        self._data_dir = data_dir

    @classmethod
    def create(
        cls, settings: Any, data_dir: Any, memory: Any | None = None
    ) -> Mem0MemoryPipeline:
        """构造 Mem0 实例并接线。失败即抛（fail-fast，不静默降级）。"""
        try:
            from mem0 import Memory
        except ImportError as e:
            raise RuntimeError(
                "mem0ai is not installed. Run: pip install 'lingxi[mem0]'"
            ) from e

        if memory is not None:
            base = memory
        else:
            cfg = settings.mem0
            base = Memory.from_config(
                _build_mem0_config(
                    collection=cfg.collection,
                    path=str(data_dir / "mem0_chroma"),
                    history_db=str(data_dir / cfg.history_db),
                    base_url=cfg.base_url,
                    embed_model=cfg.embed_model,
                    llm_model=cfg.llm_model,
                    api_key=(cfg.api_key.get_secret_value() if cfg.api_key else None),
                )
            )
        inst = cls(base, settings, data_dir)
        log.info("mem0_backend_created", collection=settings.mem0.collection)
        return inst

    # ── 存储 ──

    async def encode_and_store(
        self,
        user_id: str,
        text: str,
        *,
        max_facts: int = 3,
        source: str = "",
    ) -> list[MemoryItem]:
        """Mem0 自动抽取事实 + 向量编码 + 存储。失败静默降级（本轮不产出新记忆）。"""
        if not text or not text.strip():
            return []
        try:
            result = await asyncio.to_thread(
                self._mem.add,
                text,
                user_id=user_id,
                metadata={"source": source, "max_facts": max_facts},
            )
        except Exception as e:
            log.warning("mem0_add_failed", error=str(e))
            return []

        items: list[MemoryItem] = []
        for r in _results(result):
            fact = str(r.get("memory") or r.get("text") or "").strip()
            if not fact:
                continue
            items.append(
                MemoryItem(
                    memory_id=str(r.get("id") or ""),
                    user_id=user_id,
                    fact=fact,
                    source=source,
                )
            )
        if items:
            log.debug("mem0_stored", user_id=user_id, count=len(items))
        return items

    def store(self, item: MemoryItem) -> None:
        """直接写一条结构化事实进 Mem0（绕过抽取，供持久化水合等场景）。"""
        try:
            self._mem.add(
                item.fact,
                user_id=item.user_id,
                metadata={
                    "source": item.source or "direct",
                    "confidence": item.confidence,
                },
            )
        except Exception as e:
            log.warning("mem0_store_failed", error=str(e))

    # ── 召回 ──

    def recall(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        min_confidence: float = 0.3,
    ) -> MemoryRecallResult:
        """向量语义召回（同步，供无事件循环的调用方直接使用）。"""
        try:
            payload = self._search(query, user_id, top_k)
        except Exception as e:
            log.warning("mem0_search_failed", error=str(e))
            return MemoryRecallResult(items=[], total_candidates=0)

        items: list[MemoryItem] = []
        for r in _results(payload):
            score = r.get("score")
            if score is not None and float(score) < min_confidence:
                continue
            fact = str(r.get("memory") or r.get("text") or "").strip()
            if not fact:
                continue
            items.append(
                MemoryItem(
                    memory_id=str(r.get("id") or ""),
                    user_id=user_id,
                    fact=fact,
                    source=str((r.get("metadata") or {}).get("source", "")) if isinstance(r.get("metadata"), dict) else "mem0",
                )
            )
        return MemoryRecallResult(
            items=items[:top_k], total_candidates=len(items)
        )

    def _search(self, query: str, user_id: str, top_k: int) -> Any:
        """版本容忍的 search：2.x 用 filters=，旧版用顶层 user_id/limit。"""
        try:
            return self._mem.search(query, top_k=top_k, filters={"user_id": user_id})
        except TypeError:
            return self._mem.search(query, user_id=user_id, limit=top_k)

    def recall_hybrid(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        min_confidence: float = 0.3,
    ) -> MemoryRecallResult:
        """Mem0 自带向量 + 图谱混合检索（内部已做 dedupe + rerank）。"""
        return self.recall(user_id, query, top_k=top_k, min_confidence=min_confidence)

    # ── 查询 / 维护 ──

    def list_user_memories(self, user_id: str) -> list[MemoryItem]:
        payload = self._get_all(user_id)
        out: list[MemoryItem] = []
        for r in _results(payload):
            fact = str(r.get("memory") or r.get("text") or "").strip()
            if fact:
                out.append(
                    MemoryItem(
                        memory_id=str(r.get("id") or ""),
                        user_id=user_id,
                        fact=fact,
                        source="mem0",
                        created_at=datetime.now(),
                    )
                )
        return out

    def _get_all(self, user_id: str) -> Any:
        """版本容忍的 get_all：2.x 必须带 filters，旧版用顶层 user_id。"""
        try:
            return self._mem.get_all(filters={"user_id": user_id}, top_k=1000)
        except TypeError:
            return self._mem.get_all(user_id=user_id)

    @property
    def total_memories(self) -> int:
        """全局记忆数。

        mem0 2.x 的 get_all 必须带 entity filter（没有"列全部"参数），
        无参调用会 ValueError —— 此时直接问底层向量库计数（本项目固定
        chroma）；再拿不到就返回 0（仅用于启动日志，不做业务判断）。
        """
        try:
            return len(_results(self._mem.get_all()))
        except (TypeError, ValueError):
            return self._vector_store_count()
        except Exception as e:
            log.warning("mem0_total_failed", error=str(e))
            return 0

    def _vector_store_count(self) -> int:
        vs = getattr(self._mem, "vector_store", None)
        client = getattr(vs, "client", None)
        if client is None:
            return 0
        try:
            total = 0
            for col in client.list_collections():
                name = getattr(col, "name", None) or str(col)
                try:
                    total += int(client.get_collection(name).count())
                except Exception:
                    continue
            return total
        except Exception:
            return 0

    def update_fact(self, memory_id: str, new_fact: str, new_confidence: float) -> None:
        try:
            self._mem.update(memory_id, new_fact)
        except Exception as e:
            log.warning("mem0_update_failed", memory_id=memory_id, error=str(e))

    def contradict(self, memory_id: str, evidence: str) -> None:
        """Mem0 无置信度降级概念：矛盾证据 → 删除旧记忆，由后续 add 重建。"""
        try:
            self._mem.delete(memory_id)
            log.debug("mem0_contradicted_deleted", memory_id=memory_id)
        except Exception as e:
            log.warning("mem0_delete_failed", memory_id=memory_id, error=str(e))

    def forget(self, user_id: str | None = None, days: int = 30) -> int:
        try:
            if user_id:
                self._mem.delete_all(user_id=user_id)
                return 1
            self._mem.reset()
            return 1
        except Exception as e:
            log.warning("mem0_forget_failed", error=str(e))
            return 0

    @property
    def graph(self) -> Any:
        """Mem0 内置实体图谱（graph store），供 MCP graph:// 统计读取。"""
        return getattr(self._mem, "entity_store", None)

    def close(self) -> None:
        close = getattr(self._mem, "close", None)
        if callable(close):
            close()


__all__ = ["Mem0MemoryPipeline"]
