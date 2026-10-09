"""Node 3.1 — 记忆管线（Mem0 5 段式：编码→存储→召回→更新→遗忘）。

🔗 工程逻辑
    这不是简单的"把对话历史存起来"。完整记忆管线必须闭环：
    - 编码：从原始对话中抽取结构化事实（不是存全文）
    - 存储：事实写入回忆库（可向量检索）+ 图谱（实体关系）
    - 召回：LLM 决策哪部分记忆与当前 query 相关（不是纯向量相似度）
    - 更新：新事实覆盖旧事实，处理矛盾
    - 遗忘：过期/低置信度/长期未触达的记忆渐进退化

    本地实现用 list 存储 + 精确匹配召回（不需要 embedding 模型即可工作），
    后续可无缝升级到 Mem0。

🤖 AI 避坑
    - 不要把整个对话历史当记忆（token 浪费 + 注意力稀释）
    - 编码阶段必须由 LLM 执行（规则抽取会漏掉隐含事实）
    - 遗忘不是删除：先降级为摘要，30 天后再考虑删除
    - 记忆的"置信度"会随时间衰减，新证据 > 旧证据
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from lingxi.core import get_logger

log = get_logger(__name__)


@dataclass
class MemoryItem:
    """一条结构化记忆（LLM 编码后的产物）。"""

    memory_id: str
    user_id: str
    fact: str                    # 事实断言，例: "用户购买了 Pro 年付套餐"
    entities: list[str] = field(default_factory=list)  # 提及的实体 ID
    confidence: float = 1.0     # 置信度 0.0-1.0
    source: str = ""            # 来源：对话 ID / API / 手动注入
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed: datetime = field(default_factory=datetime.now)
    access_count: int = 0       # 命中次数（用于遗忘决策）
    status: Literal["active", "degraded", "forgotten"] = "active"

    def touch(self) -> None:
        self.last_accessed = datetime.now()
        self.access_count += 1


@dataclass
class MemoryRecallResult:
    """记忆召回结果。"""

    items: list[MemoryItem]
    total_candidates: int = 0

    def render_for_prompt(self, max_chars: int = 1500) -> str:
        """把召回的记忆渲染为 prompt 注入格式。"""
        if not self.items:
            return "（无相关记忆）"

        parts: list[str] = []
        total = 0
        for item in self.items:
            line = f"- {item.fact}（置信度 {item.confidence:.0%}）"
            if total + len(line) > max_chars:
                break
            parts.append(line)
            total += len(line)

        return "\n".join(parts)


class MemoryPipeline:
    """本地记忆管线：list-backed，支持 5 段式完整流程。"""

    def __init__(self) -> None:
        self._memories: dict[str, MemoryItem] = {}  # memory_id → item

    # ── 存储 ──

    def store(self, item: MemoryItem) -> None:
        """存储一条记忆。如果同一 user_id + 相似的 fact 已存在，更新而非重复。"""
        # 去重：完全相同的 fact 已存在 → 更新置信度和最后访问时间
        for existing in self._memories.values():
            if existing.user_id == item.user_id and existing.fact == item.fact:
                existing.confidence = max(existing.confidence, item.confidence)
                existing.touch()
                log.debug("memory_updated", memory_id=existing.memory_id)
                return

        self._memories[item.memory_id] = item
        log.debug("memory_stored", memory_id=item.memory_id)

    # ── 召回 ──

    def recall(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        min_confidence: float = 0.3,
    ) -> MemoryRecallResult:
        """为用户召回与 query 相关的记忆。

        当前实现：基于字符 n-gram 重叠打分（适用于中英文混合）。
        生产环境替换为 Mem0 向量召回 + LLM rerank。
        """
        import math
        import re

        def _tokenize(text: str) -> set[str]:
            """简单的 CJK 感知 tokenize：中文字符逐字切分，英文按空格/标点切分。"""
            cjk_chars = set(re.findall(r"[一-鿿]", text))
            latin_tokens = set(re.findall(r"[a-zA-Z0-9]+", text.lower()))
            return cjk_chars | latin_tokens

        query_terms = _tokenize(query)
        candidates: list[tuple[float, MemoryItem]] = []

        for item in self._memories.values():
            if item.user_id != user_id:
                continue
            if item.status != "active":
                continue
            if item.confidence < min_confidence:
                continue

            fact_terms = _tokenize(item.fact)
            if not fact_terms:
                continue

            overlap = len(query_terms & fact_terms) / max(len(query_terms), 1)
            score = overlap * item.confidence * (1 + math.log1p(item.access_count) * 0.1)
            if score > 0:
                candidates.append((score, item))

        candidates.sort(key=lambda x: x[0], reverse=True)
        selected = candidates[:top_k]

        for _, item in selected:
            item.touch()

        return MemoryRecallResult(
            items=[item for _, item in selected],
            total_candidates=len(candidates),
        )

    # ── 更新 ──

    def update_fact(self, memory_id: str, new_fact: str, new_confidence: float) -> None:
        """更新事实内容。"""
        item = self._memories.get(memory_id)
        if item:
            item.fact = new_fact
            item.confidence = new_confidence

    def contradict(self, memory_id: str, evidence: str) -> None:
        """新证据与记忆矛盾 → 降低置信度。"""
        item = self._memories.get(memory_id)
        if item:
            item.confidence *= 0.5
            if item.confidence < 0.2:
                item.status = "degraded"

    # ── 遗忘 ──

    def forget(self, user_id: str | None = None, days: int = 30) -> int:
        """退化超过 days 天未触达的记忆。"""
        cutoff = datetime.now() - timedelta(days=days)
        aged = [
            m for m in self._memories.values()
            if m.last_accessed < cutoff
            and (user_id is None or m.user_id == user_id)
        ]
        for item in aged:
            if item.status == "active":
                item.status = "degraded"
                item.confidence *= 0.5
            elif item.status == "degraded":
                item.status = "forgotten"
        return len(aged)

    # ── 查询 ──

    def list_user_memories(self, user_id: str) -> list[MemoryItem]:
        return [
            m for m in self._memories.values()
            if m.user_id == user_id and m.status != "forgotten"
        ]

    @property
    def total_memories(self) -> int:
        return len(self._memories)


__all__ = ["MemoryItem", "MemoryRecallResult", "MemoryPipeline"]
