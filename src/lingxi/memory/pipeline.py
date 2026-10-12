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
from typing import TYPE_CHECKING, Literal

from lingxi.core import get_logger

if TYPE_CHECKING:
    from lingxi.memory.graph_store import GraphStore

log = get_logger(__name__)

# 实体词表：客服域高频实词。用于图谱建边与召回时的实体对齐。
# 刻意不引 jieba/spaCy —— 一个几 MB 的依赖换来的分词收益，
# 在"抽实体建图"这个用途上远不如一张可审计、可扩充的词表可控。
_ENTITY_VOCAB: tuple[str, ...] = (
    # 订单与物流
    "订单", "物流", "快递", "运单", "发货", "收货", "签收", "配送", "退货",
    "退款", "换货", "发票", "预约", "上门",
    # 产品与计费
    "套餐", "订阅", "续费", "会员", "账号", "密码", "设备", "版本", "试用",
    "退款政策", "价格", "优惠", "账单",
    # 服务与工单
    "工单", "客服", "坐席", "人工", "投诉", "售后", "维修", "安装",
    # 支付
    "支付", "付款", "转账", "微信", "支付宝", "银行卡", "扣款",
    # 时间与数量
    "今天", "明天", "后天", "本周", "上周", "本月", "上月", "上午", "下午",
    "分钟", "小时", "天", "周", "月",
)


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
                # 旧实现 break：首条超预算 → 整块返回空串 →
                # injector 的 if memory_block 判空 → 记忆凭空消失（无日志）。
                # 现改为：先把超长条目截断塞进去（至少保留部分事实），
                # 再 break —— 记忆"变小"而不是"消失"。
                remaining = max_chars - total
                if remaining > 20 and item.fact:
                    fact = item.fact[: max(remaining - 12, 8)] + "…"
                    parts.append(f"- {fact}（截断）")
                log.info(
                    "memory_block_truncated",
                    memory_id=item.memory_id,
                    kept=len(parts),
                    total=len(self.items),
                )
                break
            parts.append(line)
            total += len(line)

        return "\n".join(parts)


class MemoryPipeline:
    """本地记忆管线：list-backed，支持 5 段式完整流程。"""

    def __init__(self, graph: GraphStore | None = None) -> None:
        self._memories: dict[str, MemoryItem] = {}  # memory_id → item
        # 实体图谱（可选）。传入则编码时同步建图，不传则纯记忆链路。
        self._graph = graph

    @property
    def graph(self) -> GraphStore | None:
        return self._graph

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

    def recall_hybrid(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        min_confidence: float = 0.3,
    ) -> MemoryRecallResult:
        """字面召回 + 图谱召回（去重后按原顺序合并）。

        字面召回优先（图谱结果补在后面），保证已有行为不回退；
        图谱只负责捞出"措辞不同但共享实体"的记忆。
        """
        base = self.recall(
            user_id=user_id, query=query, top_k=top_k, min_confidence=min_confidence,
        )
        if self._graph is None:
            return base

        known = {item.fact for item in base.items}
        extra: list[MemoryItem] = []
        for fact in self.graph_neighbors(user_id, query, top_k=top_k):
            if fact in known:
                continue
            item = next(
                (
                    m for m in self._memories.values()
                    if m.user_id == user_id and m.fact == fact
                ),
                None,
            )
            if item is not None and item.status != "forgotten":
                known.add(fact)
                extra.append(item)

        if not extra:
            return base
        items = [*base.items, *extra][:top_k]
        return MemoryRecallResult(
            items=items,
            total_candidates=base.total_candidates + len(extra),
        )

    def graph_neighbors(self, user_id: str, query: str, top_k: int = 5) -> list[str]:
        """图谱召回：找出与 query 共享实体、且属于该用户的记忆。

        🔗 工程逻辑
            字符 n-gram 召回对"同一实体、不同措辞"无能为力
            （"我的套餐到期了吗" vs "Pro 套餐下月续费"字面零重叠）。
            图谱补的就是这一块：先由 query 抽实体，再沿图谱反查相关记忆。

        返回命中记忆的 fact 列表；无图谱或无命中时返回空列表（调用方静默跳过）。
        """
        if self._graph is None:
            return []
        terms = self._extract_entities(query)
        if not terms:
            return []

        seen_memories: set[str] = set()
        facts: list[str] = []
        for term in terms:
            if self._graph.find_entity(term) is None:
                continue
            for rel, _neighbor in self._graph.related(term, rel_type="mentions"):
                # 边的两端是 (memory_id, entity_id)，走哪个方向都能取到 memory_id
                memory_id = (
                    rel.source_id if rel.source_id in self._memories
                    else rel.target_id
                )
                if memory_id in seen_memories:
                    continue
                item = self._memories.get(memory_id)
                if item is None or item.user_id != user_id:
                    continue
                if item.status == "forgotten":
                    continue
                seen_memories.add(memory_id)
                facts.append(item.fact)
                if len(facts) >= top_k:
                    return facts
        return facts

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
            log.debug("memory_contradicted", memory_id=memory_id, evidence=evidence[:200], new_confidence=item.confidence)

    # ── 遗忘 ──

    def forget(self, user_id: str | None = None, days: int = 30) -> int:
        """退化超过 days 天未触达的记忆。"""
        cutoff = datetime.now() - timedelta(days=days)
        aged = [
            m for m in self._memories.values()
            if m.last_accessed <= cutoff
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

    # ── 编码 (litellm 后端) ──

    async def encode_and_store(
        self,
        user_id: str,
        text: str,
        *,
        max_facts: int = 3,
        source: str = "",
    ) -> list[MemoryItem]:
        """从一段对话中提取结构化事实并自动存储 (封装 store())。

        使用 litellm.acompletion 做零代码 LLM 调用，与 agent/nodes.py
        共用同一套 litellm 路由/鉴权配置 —— 不手搓 httpx, 不重复管理 key。
        LiteLLM 已是本仓库核心依赖 (见 pyproject.toml)。

        抽取失败时静默降级：不抛异常、不中断对话，只是本轮不产出新记忆。
        """
        import uuid as _uuid

        facts = await self._llm_extract_facts(text, max_facts=max_facts)
        items: list[MemoryItem] = []
        for fact_text in facts:
            item = MemoryItem(
                memory_id=_uuid.uuid4().hex[:12],
                user_id=user_id,
                fact=fact_text.strip(),
                source=source,
                entities=self._extract_entities(fact_text.strip()),
            )
            self.store(item)
            items.append(item)
            self._link_graph(item)
        if items:
            log.debug("memories_encoded", user_id=user_id, count=len(items))
        return items

    @staticmethod
    def _extract_entities(fact: str) -> list[str]:
        """从事实里抽实体关键词，供 graph_store 建图。

        🔗 工程逻辑
            MemoryItem.entities 此前**永远为空**（只有 persistence 层读它），
            导致 graph_store 拿不到任何节点，整块图谱能力名存实亡。
            这里用「中文按 2-4 字滑窗 + 英文/数字整词」的轻量切分：
            不引 spaCy/jieba（多一个重依赖，且中文分词效果未必更好），
            只取能稳定复现的实词候选。
        """
        import re

        entities: list[str] = []
        seen: set[str] = set()

        # 英文词 / 数字（订单号、套餐名等）
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}|\d{3,}", fact):
            key = token.lower()
            if key not in seen:
                seen.add(key)
                entities.append(token)

        # 中文实词表：只保留业务里真正承载语义的词根。
        # 不用 2-4 字滑窗 —— 滑窗会产出「套餐下月」「餐下月自」这类跨词边界的
        # 垃圾片段：既污染图谱，又让图谱召回退化成另一套字面匹配
        # （query 抽出同样的垃圾 gram 就算命中，并没有语义增益）。
        # 词表命中是"宁可漏也不错"，图谱只负责补精确的字面召回兜不住的那部分。
        vocab = _ENTITY_VOCAB
        cjk = re.sub(r"[^一-鿿]+", " ", fact)
        for chunk in cjk.split():
            matched = False
            for term in vocab:
                if term in chunk and term not in seen:
                    seen.add(term)
                    entities.append(term)
                    matched = True
            if not matched and len(chunk) >= 2:
                # 词表未覆盖的长片段（如自定义产品名）：整段作为单个实体，
                # 保证图谱对未知领域仍能工作
                key = chunk[:12]
                if key not in seen:
                    seen.add(key)
                    entities.append(key)
        return entities[:16]

    def _link_graph(self, item: MemoryItem) -> None:
        """把这条记忆挂进实体图谱（存在图谱时）。

        图谱是**可选增强**：没挂 graph 就纯跳过，不影响记忆主链路。
        """
        if self._graph is None:
            return
        from lingxi.memory.graph_store import Entity, Relation

        for entity_id in item.entities:
            self._graph.add_entity(Entity(entity_id=entity_id, label=entity_id))
            self._graph.add_relation(
                Relation(
                    source_id=item.memory_id,
                    target_id=entity_id,
                    rel_type="mentions",
                ),
            )

    @staticmethod
    async def _llm_extract_facts(text: str, max_facts: int = 3) -> list[str]:
        """用 litellm 从自然语言中提取事实断言列表。

        返回值: 每个元素是一条简短事实字符串, 如 "用户是素食主义者"。
        失败 (无 key / 网络问题 / 解析异常) 返回空列表, 不抛异常。
        """
        import asyncio as _asyncio

        from lingxi.config import get_settings
        model = get_settings().llm.model

        try:
            import litellm
        except ImportError:
            return []

        prompt = (
            f"从以下对话中提取最多 {max_facts} 条用户相关的事实断言。每条事实应该:\n"
            "- 简短陈述句, 不超过 30 字\n"
            "- 关于用户偏好/历史/需求/约束的事实信息\n"
            "- 只提取可以概括长期记忆的内容, 忽略寒暄和一次性操作\n"
            "- 用中文返回, 每条一行, 不加编号\n\n"
            f"对话内容:\n{text[:1000]}\n\n"
            "事实列表:"
        )
        try:
            resp = await _asyncio.wait_for(
                litellm.acompletion(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=200,
                    temperature=0.0,
                ),
                timeout=8.0,
            )
            raw = resp.choices[0].message.content or ""
        except Exception as e:
            log.debug("memory_encode_failed", error=str(e))
            return []

        facts: list[str] = []
        for line in raw.strip().split("\n"):
            line = line.strip().lstrip("-•*0123456789.").strip()
            if line and len(line) > 5:
                facts.append(line)
            if len(facts) >= max_facts:
                break
        return facts

    @property
    def total_memories(self) -> int:
        return len(self._memories)


__all__ = ["MemoryItem", "MemoryRecallResult", "MemoryPipeline"]
