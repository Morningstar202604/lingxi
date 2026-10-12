"""记忆图谱接线 + 实体抽取的行为锁。

🔗 工程逻辑
    `memory/graph_store.py` 此前是一个**完全孤立的模块**：除 `__init__.py` 再导出
    与单元测试外零使用点，而 `MemoryItem.entities` 又永远为空 —— 整块"知识图谱"
    能力名存实亡（见 docs/AUDIT-2026-10-10.md 4.3）。

    本文件锁两件事：
    1. 编码时真的会抽出实体（entities 不再恒空）
    2. 传入图谱时会把「记忆 → 实体」关系写进去，图谱不再重启即丢

🤖 AI 避坑
    - 不传 graph 时必须能正常工作（图谱是可选增强，不是硬依赖）
    - 实体抽取是轻量规则，不是 NLP 模型：断言"有输出且去重"，不断言具体词
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from lingxi.memory.graph_store import InMemoryGraphStore
from lingxi.memory.pipeline import MemoryItem, MemoryPipeline


class TestEntityExtraction:
    def test_entities_no_longer_empty(self) -> None:
        """中文事实应抽出实体候选。"""
        ents = MemoryPipeline._extract_entities("用户购买了 Pro 套餐，订单号 A12345")
        assert ents, "实体抽取返回空 —— 图谱将永远没有节点"
        assert len(ents) == len(set(ents)), "实体应去重"
        assert len(ents) <= 16

    def test_english_and_digits_extracted(self) -> None:
        ents = MemoryPipeline._extract_entities("用户升级到 Enterprise 版本")
        lowered = [e.lower() for e in ents]
        assert "enterprise" in lowered

    def test_empty_text_yields_nothing(self) -> None:
        assert MemoryPipeline._extract_entities("") == []
        assert MemoryPipeline._extract_entities("   ") == []

    def test_stopwords_not_entities(self) -> None:
        """纯虚词片段不该变成实体节点。"""
        ents = MemoryPipeline._extract_entities("的了是我")
        assert all(e not in ("的", "了", "是", "我") for e in ents)


class TestGraphWiring:
    @pytest.mark.asyncio
    async def test_encode_and_store_populates_graph(self) -> None:
        graph = InMemoryGraphStore()
        pipeline = MemoryPipeline(graph=graph)
        mock_resp = AsyncMock()
        msg = AsyncMock()
        msg.content = '["用户购买了 Pro 套餐"]'
        msg.tool_calls = None
        choice = AsyncMock()
        choice.message = msg
        mock_resp.choices = [choice]

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            items = await pipeline.encode_and_store(
                user_id="u1", text="我想买 Pro", source="ticket:t1",
            )

        assert items, "LLM 抽出事实后应产出记忆"
        assert items[0].entities, "MemoryItem.entities 仍为空 —— 图谱接线没生效"
        assert graph.entity_count > 0, "图谱里没有实体节点"

    @pytest.mark.asyncio
    async def test_works_without_graph(self) -> None:
        """不传 graph 时记忆链路必须照常工作（图谱是可选增强）。"""
        pipeline = MemoryPipeline()
        assert pipeline.graph is None
        pipeline.store(MemoryItem(memory_id="m1", user_id="u1", fact="事实A"))
        result = pipeline.recall(user_id="u1", query="事实A")
        assert result.items, "无图谱时记忆召回失效"


class TestHybridRecall:
    @pytest.mark.asyncio
    async def test_graph_recall_raises_precision(self) -> None:
        """图谱的增量价值是**精确率**：字符级召回只要有任一汉字重合就算命中。

        「套餐」会同时召回「套餐到期后自动续费」和「餐具消毒柜已送达」；
        沿实体边走则只命中真正关于套餐的那条。
        """
        graph = InMemoryGraphStore()
        pipeline = MemoryPipeline(graph=graph)
        for mid, fact in (
            ("m1", "套餐到期后自动续费"),
            ("m2", "餐具消毒柜已送达"),
        ):
            item = MemoryItem(
                memory_id=mid,
                user_id="u1",
                fact=fact,
                entities=MemoryPipeline._extract_entities(fact),
            )
            pipeline.store(item)
            pipeline._link_graph(item)

        query = "套餐"
        assert "套餐" in MemoryPipeline._extract_entities(query)

        literal = {i.memory_id for i in pipeline.recall(user_id="u1", query=query).items}
        assert literal == {"m1", "m2"}, "字面召回的精确率不该这么好"

        assert pipeline.graph_neighbors("u1", query) == ["套餐到期后自动续费"]

    @pytest.mark.asyncio
    async def test_hybrid_never_duplicates(self) -> None:
        graph = InMemoryGraphStore()
        pipeline = MemoryPipeline(graph=graph)
        mock_resp = AsyncMock()
        msg = AsyncMock()
        msg.content = '["订单 A12345 已发货"]'
        msg.tool_calls = None
        choice = AsyncMock()
        choice.message = msg
        mock_resp.choices = [choice]
        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            await pipeline.encode_and_store(user_id="u1", text="x")

        hybrid = pipeline.recall_hybrid(user_id="u1", query="订单 A12345 发货了吗")
        facts = [i.fact for i in hybrid.items]
        assert len(facts) == len(set(facts)), "字面与图谱命中同一记忆导致重复"

    def test_hybrid_without_graph_is_passthrough(self) -> None:
        pipeline = MemoryPipeline()
        pipeline.store(MemoryItem(memory_id="m1", user_id="u1", fact="用户喜欢快递上门"))
        r = pipeline.recall_hybrid(user_id="u1", query="快递")
        assert r.items and r.items[0].fact == "用户喜欢快递上门"

    def test_graph_recall_respects_user_boundary(self) -> None:
        """A 用户的记忆不能被 B 用户通过图谱捞到。"""
        graph = InMemoryGraphStore()
        pipeline = MemoryPipeline(graph=graph)
        item = MemoryItem(
            memory_id="m1", user_id="u1", fact="套餐已升级", entities=["套餐"],
        )
        pipeline.store(item)
        pipeline._link_graph(item)  # noqa: SLF001
        assert pipeline.graph_neighbors("u1", "套餐怎么收费")
        assert not pipeline.graph_neighbors("u2", "套餐怎么收费"), "跨用户泄漏"


class TestGraphStoreOps:
    def test_relation_links_memory_to_entity(self) -> None:
        graph = InMemoryGraphStore()
        pipeline = MemoryPipeline(graph=graph)
        pipeline._link_graph(  # noqa: SLF001 — 直接测内部建图
            MemoryItem(
                memory_id="m1",
                user_id="u1",
                fact="用户购买 Pro",
                entities=["Pro"],
            ),
        )
        rels = graph.related("m1")
        assert rels, "记忆 → 实体的关系没有写进图谱"
        assert rels[0][0].rel_type == "mentions"
        assert rels[0][1].entity_id == "Pro"

    def test_related_filter_by_type(self) -> None:
        graph = InMemoryGraphStore()
        pipeline = MemoryPipeline(graph=graph)
        pipeline._link_graph(  # noqa: SLF001
            MemoryItem(memory_id="m1", user_id="u1", fact="x", entities=["A"]),
        )
        assert graph.related("m1", rel_type="mentions")
        assert not graph.related("m1", rel_type="BOUGHT")

    def test_find_and_search(self) -> None:
        graph = InMemoryGraphStore()
        pipeline = MemoryPipeline(graph=graph)
        pipeline._link_graph(  # noqa: SLF001
            MemoryItem(memory_id="m1", user_id="u1", fact="x", entities=["订单"]),
        )
        assert graph.find_entity("订单") is not None
        assert graph.search(label="订单")
        assert not graph.search(label="不存在")


class TestEngineGraphInjection:
    def test_default_engine_wires_graph_store(self) -> None:
        """build_default_engine 必须把图谱接进记忆管线（否则生产仍是死代码）。"""
        import lingxi.__main__ as main

        engine: Any = main.build_default_engine()
        assert engine.memory.graph is not None, "生产引擎没接图谱"


class TestPersistentMemoryGraph:
    def test_graph_passthrough(self, tmp_path: Any) -> None:
        """PersistentMemoryPipeline 必须透传 graph。

        此前 `MemoryPipeline()` 无参构造 —— 持久化模式
        （settings.memory.persistent 默认 True）下实体图谱
        整体失效：编码不建边、recall_hybrid 图谱分支恒空、
        MCP graph://stats 资源不注册。
        """
        from lingxi.flow.persistence import (
            PersistentMemoryPipeline,
            _SqliteWriter,
        )

        writer = _SqliteWriter(str(tmp_path / "zhixi_graph_test.db"))
        graph = InMemoryGraphStore()
        pipeline = PersistentMemoryPipeline(writer, graph=graph)
        assert pipeline.graph is graph
        assert pipeline._inner.graph is graph  # noqa: SLF001
        # 透传后编码出的记忆应真实建边
        pipeline._inner._link_graph(  # noqa: SLF001
            MemoryItem(
                memory_id="m_p1", user_id="u1",
                fact="用户购买 Pro", entities=["Pro"],
            ),
        )
        assert graph.related("m_p1")
