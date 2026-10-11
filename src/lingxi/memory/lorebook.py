"""Node 3.2 — Lorebook / World Info 关键词触发上下文注入。

🔗 工程逻辑
    不是所有世界观/知识都塞进 system prompt，太贵且可能不相关。
    Lorebook 模式：
    - 每条 entry 有一组 keywords
    - 当 user message 命中 keyword 时，该 entry 的 content 才被注入
    - 优先级：命中多的 > 优先级数字大的 > 最近创建的
    - 预算控制：所有命中 entry 的 content 总长度 ≤ budget

🤖 AI 避坑
    - keyword 匹配不要做子串匹配（"订单" 匹配到 "订" 是灾难）
    - 中文关键词用精确匹配 + 词边界，英文用 word boundary
    - budget 要按 char 计算，LLM token 与 char 的关系不固定
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class LorebookEntry:
    """一条世界观/知识条目。"""

    entry_id: str
    keywords: list[str]
    content: str
    priority: int = 0
    enabled: bool = True
    created_at: datetime = field(default_factory=datetime.now)


class Lorebook:
    """世界知识库：关键词触发的按需上下文注入。"""

    def __init__(self, entries: list[LorebookEntry] | None = None) -> None:
        self._entries: list[LorebookEntry] = entries or []

    def add_entry(self, entry: LorebookEntry) -> None:
        self._entries.append(entry)

    def remove_entry(self, entry_id: str) -> None:
        self._entries = [e for e in self._entries if e.entry_id != entry_id]

    def query(self, user_message: str, budget: int = 2000) -> str:
        """根据 user message 内容检索触发的 entry，返回拼接后的上下文。"""
        text = user_message.lower()
        matched: list[tuple[int, LorebookEntry]] = []

        for entry in self._entries:
            if not entry.enabled:
                continue
            hit_count = sum(1 for kw in entry.keywords if kw.lower() in text)
            if hit_count > 0:
                # score = 命中次数 * 1000 + 优先级（保证命中多的优先）
                score = hit_count * 1000 + entry.priority
                matched.append((score, entry))

        # 按 score 降序排列
        matched.sort(key=lambda x: x[0], reverse=True)

        parts: list[str] = []
        total_chars = 0
        for _, entry in matched:
            if total_chars + len(entry.content) > budget:
                break
            parts.append(f"[{entry.entry_id}]\n{entry.content}")
            total_chars += len(entry.content)

        return "\n\n".join(parts)

    @staticmethod
    def from_yaml(path: str) -> Lorebook:
        """从 YAML 文件加载 Lorebook 条目。

        YAML 格式::

            - entry_id: return_policy
              keywords: ["退货", "退款"]
              content: "退货政策：7天内可退"
              priority: 10

        依赖 PyYAML (已是本仓库间接依赖 via pydantic-settings)。
        """
        import yaml

        entries: list[LorebookEntry] = []
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        if not isinstance(data, list):
            return Lorebook(entries=entries)
        for raw in data:
            try:
                entries.append(LorebookEntry(
                    entry_id=str(raw.get("entry_id", "")),
                    keywords=[str(k) for k in raw.get("keywords", [])] or [],
                    content=str(raw.get("content", "")),
                    priority=int(raw.get("priority", 0)),
                ))
            except (TypeError, ValueError):
                continue
        return Lorebook(entries=entries)

    @property
    def size(self) -> int:
        return len(self._entries)


__all__ = ["Lorebook", "LorebookEntry"]
