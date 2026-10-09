"""Layer 3 测试 — Lorebook + GraphStore + MemoryPipeline。"""
from __future__ import annotations

import pytest

from lingxi.memory.lorebook import Lorebook, LorebookEntry
from lingxi.memory.graph_store import Entity, Relation, InMemoryGraphStore
from lingxi.memory.pipeline import MemoryItem, MemoryPipeline


# ── Lorebook ──

class TestLorebook:
    def _make_book(self) -> Lorebook:
        return Lorebook([
            LorebookEntry(entry_id="refund-policy", keywords=["退款", "退钱", "return"],
                          content="退款需提交订单号，3 个工作日内处理。", priority=10),
            LorebookEntry(entry_id="vip-benefits", keywords=["VIP", "会员", "vip"],
                          content="会员享受 9 折和优先客服。", priority=5),
        ])

    def test_no_match_returns_empty(self) -> None:
        book = self._make_book()
        assert book.query("今天天气") == ""

    def test_keyword_match(self) -> None:
        book = self._make_book()
        result = book.query("我要退款")
        assert "退款" in result
        assert "订单号" in result

    def test_budget_limit(self) -> None:
        book = Lorebook([
            LorebookEntry(entry_id="big", keywords=["触发"],
                          content="A" * 1000, priority=0),
            LorebookEntry(entry_id="big2", keywords=["触发"],
                          content="B" * 1000, priority=0),
        ])
        result = book.query("触发", budget=500)
        # 只够放一个 entry
        assert len(result) < 2000

    def test_disabled_entry_skipped(self) -> None:
        entry = LorebookEntry(entry_id="x", keywords=["触发"],
                              content="内容", enabled=False)
        book = Lorebook([entry])
        assert book.query("触发") == ""

    def test_priority_ordering(self) -> None:
        book = Lorebook([
            LorebookEntry(entry_id="low", keywords=["订单"],
                          content="低优先级", priority=1),
            LorebookEntry(entry_id="high", keywords=["订单"],
                          content="高优先级", priority=100),
        ])
        result = book.query("订单")
        # 高优先级先出现
        assert result.index("高优先级") < result.index("低优先级") or \
               "高优先级" in result[:len(result) // 2]


# ── GraphStore ──

class TestGraphStore:
    def test_add_and_find_entity(self) -> None:
        store = InMemoryGraphStore()
        store.add_entity(Entity(entity_id="e1", label="Person", properties={"name": "张三"}))
        found = store.find_entity("e1")
        assert found is not None
        assert found.properties["name"] == "张三"

    def test_add_and_find_relation(self) -> None:
        store = InMemoryGraphStore()
        store.add_entity(Entity(entity_id="zhang", label="Person"))
        store.add_entity(Entity(entity_id="product-a", label="Product"))
        store.add_relation(Relation(source_id="zhang", target_id="product-a", rel_type="BOUGHT"))

        related = store.related("zhang", rel_type="BOUGHT")
        assert len(related) == 1
        rel, target = related[0]
        assert target.entity_id == "product-a"
        assert rel.rel_type == "BOUGHT"

    def test_search_by_label(self) -> None:
        store = InMemoryGraphStore()
        store.add_entity(Entity(entity_id="p1", label="Person"))
        store.add_entity(Entity(entity_id="p2", label="Person"))
        store.add_entity(Entity(entity_id="loc1", label="Location"))

        persons = store.search(label="Person")
        assert len(persons) == 2

    def test_search_by_property(self) -> None:
        store = InMemoryGraphStore()
        store.add_entity(Entity(entity_id="p1", label="Person", properties={"city": "北京"}))
        store.add_entity(Entity(entity_id="p2", label="Person", properties={"city": "上海"}))

        beijing = store.search(label="Person", city="北京")
        assert len(beijing) == 1
        assert beijing[0].entity_id == "p1"

    def test_not_found(self) -> None:
        store = InMemoryGraphStore()
        assert store.find_entity("ghost") is None


# ── MemoryPipeline ──

class TestMemoryPipeline:
    def _make_memory(self, mid: str, user: str, fact: str) -> MemoryItem:
        return MemoryItem(memory_id=mid, user_id=user, fact=fact)

    def test_store_and_recall(self) -> None:
        pipe = MemoryPipeline()
        pipe.store(self._make_memory("m1", "u1", "用户购买了 Pro 套餐"))
        result = pipe.recall("u1", "用户买了什么套餐")
        assert len(result.items) >= 1
        # 召回的事实应包含关键词
        assert any("Pro" in item.fact for item in result.items) or \
               any("套餐" in item.fact for item in result.items)

    def test_recall_only_own_user(self) -> None:
        pipe = MemoryPipeline()
        pipe.store(self._make_memory("m1", "u1", "用户 u1 的秘密"))
        pipe.store(self._make_memory("m2", "u2", "用户 u2 的秘密"))
        result = pipe.recall("u1", "秘密")
        assert all(item.user_id == "u1" for item in result.items)

    def test_dedup_same_fact(self) -> None:
        pipe = MemoryPipeline()
        pipe.store(self._make_memory("m1", "u1", "完全相同的事实"))
        pipe.store(self._make_memory("m2", "u1", "完全相同的事实"))  # 同 user_id 同 fact
        assert pipe.total_memories == 1  # 去重只留一条

    def test_forgetting(self) -> None:
        pipe = MemoryPipeline()
        pipe.store(self._make_memory("m1", "u1", "旧记忆"))
        aged = pipe.forget(user_id="u1", days=0)  # days=0 → 全部 aged
        assert aged == 1

    def test_contradict_reduces_confidence(self) -> None:
        pipe = MemoryPipeline()
        pipe.store(MemoryItem(memory_id="m1", user_id="u1", fact="事实", confidence=1.0))
        pipe.contradict("m1", "新证据")
        assert list(pipe._memories.values())[0].confidence < 1.0

    def test_update_fact(self) -> None:
        pipe = MemoryPipeline()
        pipe.store(self._make_memory("m1", "u1", "旧内容"))
        pipe.update_fact("m1", "新内容", new_confidence=0.9)
        assert pipe._memories["m1"].fact == "新内容"
        assert pipe._memories["m1"].confidence == 0.9

    def test_render_for_prompt_with_items(self) -> None:
        pipe = MemoryPipeline()
        pipe.store(self._make_memory("m1", "u1", "购买了 Pro"))
        result = pipe.recall("u1", "购买")
        rendered = result.render_for_prompt()
        assert "Pro" in rendered

    def test_render_for_prompt_empty(self) -> None:
        from lingxi.memory.pipeline import MemoryRecallResult
        empty = MemoryRecallResult(items=[])
        assert "无相关记忆" in empty.render_for_prompt()
