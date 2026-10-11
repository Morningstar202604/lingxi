"""文档切分器 — RecursiveCharacterTextSplitter 的 tiktoken 原生实现。

🔗 工程逻辑
    遵循 LangChain 的 RecursiveCharacterTextSplitter 已验证算法:
    按分隔符层级递归切分 (段落 → 句子 → 字词),
    每块用 tiktoken token 计数确保不超 chunk_size, 块间保留 overlap 上下文。

    直接用 tiktoken 而非 langchain_text_splitters 依赖, 减小依赖面。

🤖 AI 避坑
    - chunk_size 按 token 数而非字符数计量 (LLM 真实上下文窗口)
    - overlap 用 chunk_size * overlap_ratio 而非固定字符数
    - 空 chunk 必须剔除, 避免 ChromaDB 写入空向量
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lingxi.core import get_logger

log = get_logger(__name__)


@dataclass
class Chunk:
    """切分后的文本块。"""
    text: str
    chunk_index: int
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class RecursiveSplitter:
    """递归字符切分器。

    按 separators 顺序逐级切分: 先按段落 ("\\n\\n"), 段内超限则按句子 ("\\. "),
    再超限则按空格, 最后硬切。确保 chunk_size (token) 软上限。
    """

    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: float = 0.1,
        model: str = "cl100k_base",
        separators: list[str] | None = None,
    ) -> None:
        self.chunk_size = chunk_size
        self.overlap = int(chunk_size * chunk_overlap)
        self.separators = separators or ["\n\n", "\n", ". ", " ", ""]
        self._model = model
        # 懒加载 tiktoken
        self._enc: Any = None

    @property
    def enc(self) -> Any:
        if self._enc is None:
            import tiktoken
            self._enc = tiktoken.get_encoding(self._model)
        return self._enc

    def _count_tokens(self, text: str) -> int:
        return len(self.enc.encode(text))

    def split(self, text: str, *, source: str = "") -> list[Chunk]:
        """将长文本切分为 Chunk 列表。"""
        chunks = self._recursive_split(text, self.separators)
        return [
            Chunk(text=chunk.strip(), chunk_index=i, source=source)
            for i, chunk in enumerate(chunks)
            if chunk.strip()
        ]

    def _recursive_split(self, text: str, separators: list[str]) -> list[str]:
        """递归切分: 当前 separator 切完后若仍超限, 换更细的 separator。"""
        if not text:
            return []

        if len(separators) == 0:
            # 最终兜底: 硬切 (token 级)
            return self._hard_split(text)

        sep = separators[0]
        rest = separators[1:]

        if sep == "":
            # 最后一级: 硬切
            return self._hard_split(text)

        parts = text.split(sep)
        merged: list[str] = []
        buf = ""

        for part in parts:
            candidate = (buf + sep + part) if buf else part
            if self._count_tokens(candidate) <= self.chunk_size:
                buf = candidate
            else:
                if buf:
                    merged.append(buf)
                    # overlap: 保留 buf 尾部
                    if self.overlap > 0:
                        overlap_text = self._tail_tokens(buf, self.overlap)
                        buf = overlap_text + sep + part if overlap_text else part
                    else:
                        buf = part
                else:
                    # part 本身超限, 递归换细分
                    merged.extend(self._recursive_split(part, rest))
                    buf = ""

        if buf:
            merged.append(buf)

        # 对仍然超限的 chunk 用更细 separator 递归
        result: list[str] = []
        for chunk in merged:
            if self._count_tokens(chunk) > self.chunk_size and rest:
                result.extend(self._recursive_split(chunk, rest))
            else:
                result.append(chunk)
        return result

    def _hard_split(self, text: str) -> list[str]:
        """Token 级硬切 (不超 chunk_size token)。"""
        tokens = self.enc.encode(text)
        chunks: list[str] = []
        for i in range(0, len(tokens), self.chunk_size - self.overlap):
            slice_tokens = tokens[i:i + self.chunk_size]
            chunks.append(self.enc.decode(slice_tokens))
        return chunks if chunks else [""]

    def _tail_tokens(self, text: str, n: int) -> str:
        """返回 text 的最后 n token 对应文本 (用于 overlap)。"""
        tokens = self.enc.encode(text)
        return self.enc.decode(tokens[-n:]) if len(tokens) > n else ""
