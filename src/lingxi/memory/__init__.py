"""Layer 3: 记忆系统 — 5 段式管线 + 可选 Mem0 后端。

🔗 工程逻辑
    Mem0 提供 semantic recall + 自动抽取 + 遗忘，但需要 embedding provider。
    当无 embedding provider 或 Mem0 未安装时，自动 fallback 到内置 n-gram。

🤖 AI 避坑
    - Mem0 不是必须依赖；系统应在无 Mem0 情况下也能运行
    - 切换 MemoryBackend 不应影响上层 pipeline 代码
"""
from lingxi.memory.graph_store import (
    Entity,
    GraphStore,
    InMemoryGraphStore,
    Relation,
    SQLiteGraphStore,
)
from lingxi.memory.lorebook import Lorebook, LorebookEntry
from lingxi.memory.pipeline import (
    MemoryItem,
    MemoryPipeline,
    MemoryRecallResult,
)

__all__ = [
    "Entity",
    "GraphStore",
    "InMemoryGraphStore",
    "Lorebook",
    "LorebookEntry",
    "MemoryItem",
    "MemoryPipeline",
    "MemoryRecallResult",
    "Relation",
    "SQLiteGraphStore",
]
