"""会话摘要器：LLM 生成长对话的简洁摘要, 供人工坐席快速读懂上下文。

🔗 工程逻辑
    当一段对话消息数超过阈值 (默认 20) 时, 调用 LLM 生成 ≤ 200 字的中文摘要,
    涵盖: 用户诉求、关键信息、当前状态。

    LLM 调用失败时回退到纯文本截断 (取首尾消息拼接), 不抛异常。

🤖 AI 避坑
    - max_input_chars 截断输入避免超出上下文窗口
    - acompletion 必须用 await, 同步调用会阻塞事件循环
    - fallback 不上 LLM, 只用文本截断, 保证摘要永远可用
"""
from __future__ import annotations

from typing import Any

from lingxi.config import get_settings
from lingxi.core import get_logger

log = get_logger(__name__)


class SessionSummarizer:
    """用 LLM 对长对话生成简洁摘要供人工坐席阅读。"""

    # 默认角色名, 让 fallback 里助手消息可识别
    ASSISTANT_ROLES = frozenset({"agent", "assistant", "agent_escalated", "bot"})

    def __init__(self, model: str = "", max_input_chars: int = 4000) -> None:
        cfg = get_settings()
        self._model = model or cfg.llm.model
        self._max_input = max_input_chars

    # ── 公开 API ───────────────────────────────────────────────────

    def should_summarize(self, messages: list[Any], threshold: int = 20) -> bool:
        """消息超 threshold 条时触发。"""
        return len(messages) >= threshold

    async def summarize(self, messages: list[Any]) -> str:
        """生成 ≤ 200 字的对话摘要。

        格式: "**会话摘要**: 用户咨询了... 关键信息是... Agent 回复了... 当前状态..."

        调用 litellm.acompletion(model=self._model, messages=[...], max_tokens=256)。
        system prompt: "你是对话摘要专家, 用中文总结以下对话, ≤ 200 字, 涵盖: 用户诉求, 关键信息, 当前状态。"
        失败时回退到基于首尾消息的文本截断。
        """
        if not messages:
            return "**会话摘要**:（无消息）"

        formatted = self.format_thread(messages)
        # 截断输入避免超出上下文窗口
        if len(formatted) > self._max_input:
            half = self._max_input // 2
            formatted = formatted[:half] + "\n...(中间省略)...\n" + formatted[-half:]

        try:
            from litellm import acompletion

            resp = await acompletion(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是对话摘要专家, 用中文总结以下对话, "
                            "≤ 200 字, 涵盖: 用户诉求, 关键信息, 当前状态。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": formatted,
                    },
                ],
                max_tokens=256,
            )
            summary = resp.choices[0].message.content
            return f"**会话摘要**: {summary.strip()}"
        except ImportError:
            log.warning("summarizer_litellm_not_available", fallback="text_truncation")
        except (TimeoutError, OSError) as e:
            log.warning("summarizer_llm_failed", error=str(e), fallback="text_truncation")
        except Exception as e:
            log.warning("summarizer_llm_error", error=str(e), fallback="text_truncation")

        # fallback: 基于首尾消息拼接的不含 LLM 文本截断
        return self._fallback_summary(messages)

    def format_thread(self, messages: list[Any]) -> str:
        """把 messages 格式化为 text/plain 对话线程。

        输出格式::

            user: 我的快递到哪了
            assistant: 请提供单号
            user: 12345
        """
        lines: list[str] = []
        for msg in messages:
            role = getattr(msg, "role", None)
            if role is None and isinstance(msg, dict):
                role = msg.get("role", "unknown")
            elif role is None:
                role = "unknown"

            content = getattr(msg, "content", None)
            if content is None and isinstance(msg, dict):
                content = msg.get("content", "")
            elif content is None:
                content = ""

            # 统一 role 名到 user/assistant 以提升 LLM 理解
            if role in ("user", "human"):
                display = "user"
            elif role in self.ASSISTANT_ROLES:
                display = "assistant"
            elif role == "system":
                display = "system"
            else:
                display = str(role)
            lines.append(f"{display}: {content}")
        return "\n".join(lines)

    # ── 私有方法 ───────────────────────────────────────────────────

    def _fallback_summary(self, messages: list[Any]) -> str:
        """LLM 失败时，用首尾消息拼一个简短文本摘要。"""
        first = messages[0]
        first_role = self._role(first)
        first_content = self._content(first)

        last = messages[-1]
        last_role = self._role(last)
        last_content = self._content(last)

        role_name = {"user": "用户", "assistant": "Agent"}.get
        first_label = role_name(first_role, first_role)
        last_label = role_name(last_role, last_role)

        n = len(messages)
        text = (
            f"**会话摘要**: 对话共 {n} 轮。"
            f" 首轮{first_label}：{first_content[:80]}。"
            f" 末轮{last_label}：{last_content[:80]}。"
        )
        return text

    @staticmethod
    def _role(msg: Any) -> str:
        r = getattr(msg, "role", None)
        if r is not None:
            return str(r)
        if isinstance(msg, dict):
            return str(msg.get("role", "unknown"))
        return "unknown"

    @staticmethod
    def _content(msg: Any) -> str:
        c = getattr(msg, "content", None)
        if c is not None:
            return str(c)
        if isinstance(msg, dict):
            return str(msg.get("content", ""))
        return ""


__all__ = ["SessionSummarizer"]
