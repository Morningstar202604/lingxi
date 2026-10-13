"""Mem0 记忆管线行为面测试（注入假 memory 对象，不依赖 Ollama）。

🔗 工程逻辑
    Mem0MemoryPipeline.create 接受 memory= 注入：生产传 mem0.Memory（Ollama），
    测试传假对象验证管线自身的归一化 / 隔离 / 维护语义。
    活 Ollama 的端到端验证见真实进程启动（LINGXI_MEM0__ENABLED=true）。
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from lingxi.config import Settings
from lingxi.memory.mem0_backend import Mem0MemoryPipeline


class FakeMem0:
    """假 Mem0 Memory：记录调用，返回各版本可能的形状。"""

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.updated: list[tuple[str, str]] = []

    def add(self, text: str, user_id: str = "", metadata: dict | None = None):
        self.added.append({"text": text, "user_id": user_id, "metadata": metadata})
        return {
            "results": [
                {"id": str(uuid.uuid4()), "memory": "用户偏好中文回复",
                 "event": "ADD", "user_id": user_id, "score": 0.9,
                 "metadata": {"source": metadata or {}}}
            ]
        }

    def search(self, query: str, user_id: str = "", limit: int = 5):
        return [
            {"id": "m1", "memory": "用户偏好中文回复", "score": 0.85,
             "metadata": {"source": "chat"}},
        ][:limit]

    def get_all(self, user_id: str = ""):
        return {"results": [{"id": "m1", "memory": "用户偏好中文回复"}]}

    def update(self, memory_id: str, data: str) -> None:
        self.updated.append((memory_id, data))

    def delete(self, memory_id: str) -> None:
        self.deleted.append(memory_id)

    def delete_all(self, user_id: str = "") -> None:
        self.deleted.append(f"all:{user_id}")

    def reset(self) -> None:
        self.deleted.append("reset")


@pytest.fixture
def mem0_pipeline(tmp_data_dir) -> tuple[Mem0MemoryPipeline, FakeMem0]:
    settings = Settings(data_dir=tmp_data_dir)
    fake = FakeMem0()
    pipe = Mem0MemoryPipeline.create(settings, tmp_data_dir, memory=fake)
    return pipe, fake


class TestMem0Pipeline:
    """与内置 MemoryPipeline 对齐的行为面。"""

    def test_encode_and_store_returns_items(self, mem0_pipeline) -> None:
        import asyncio

        pipe, fake = mem0_pipeline
        items = asyncio.run(pipe.encode_and_store("u1", "我喜欢简洁的回答"))
        assert len(items) == 1
        assert items[0].fact == "用户偏好中文回复"
        assert items[0].user_id == "u1"
        assert fake.added[0]["text"] == "我喜欢简洁的回答"

    def test_encode_and_store_empty_text_short_circuits(
        self, mem0_pipeline
    ) -> None:
        import asyncio

        pipe, fake = mem0_pipeline
        assert asyncio.run(pipe.encode_and_store("u1", "   ")) == []
        assert fake.added == []

    def test_recall_normalizes_shapes(self, mem0_pipeline) -> None:
        pipe, _ = mem0_pipeline
        result = pipe.recall("u1", "回复风格", top_k=5)
        assert len(result.items) == 1
        assert result.items[0].fact == "用户偏好中文回复"
        assert result.total_candidates >= 1

    def test_recall_filters_low_confidence(self, mem0_pipeline) -> None:
        pipe, _ = mem0_pipeline
        result = pipe.recall("u1", "x", min_confidence=0.99)
        # 假数据 score=0.85 < 0.99 → 被滤掉
        assert result.items == []

    def test_recall_hybrid_is_delegated(self, mem0_pipeline) -> None:
        pipe, _ = mem0_pipeline
        assert pipe.recall_hybrid("u1", "x") is not None

    def test_contradict_deletes(self, mem0_pipeline) -> None:
        pipe, fake = mem0_pipeline
        pipe.contradict("m1", "新证据")
        assert fake.deleted == ["m1"]

    def test_update_fact(self, mem0_pipeline) -> None:
        pipe, fake = mem0_pipeline
        pipe.update_fact("m1", "改为英文回复", 0.8)
        assert fake.updated == [("m1", "改为英文回复")]

    def test_forget_user(self, mem0_pipeline) -> None:
        pipe, fake = mem0_pipeline
        assert pipe.forget("u1") == 1
        assert fake.deleted == ["all:u1"]

    def test_total_memories(self, mem0_pipeline) -> None:
        pipe, _ = mem0_pipeline
        assert pipe.total_memories == 1

    def test_missing_mem0_raises_fail_fast(self, tmp_data_dir) -> None:
        """未装 mem0ai 时 create() 显式报错（不静默降级 n-gram）。"""
        settings = Settings(data_dir=tmp_data_dir)
        import builtins

        real_import = builtins.__import__

        def broken_import(name, *args, **kwargs):
            if name == "mem0":
                raise ImportError("mem0ai not installed")
            return real_import(name, *args, **kwargs)

        import builtins as _b

        _b.__import__ = broken_import
        try:
            with pytest.raises(RuntimeError, match="mem0ai is not installed"):
                Mem0MemoryPipeline.create(settings, tmp_data_dir)
        finally:
            _b.__import__ = real_import
