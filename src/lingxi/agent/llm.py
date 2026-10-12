"""LLM 调用层 — 重试、退避、token 计量、空响应防御。

🔗 工程逻辑
    `agent/nodes.py` 只关心「拿到响应」，不关心重试与异常分类。
    这层封装把三件横切的事收在一处：

    1. **重试 + 指数退避**：LLM 网关的瞬时抖动（超时 / 429 / 5xx）不该
       直接变成「转人工」。真实客服场景里一次抖动就升级，等于把
       运维波动全部转嫁给坐席。
    2. **异常分类**：区分**可重试**（超时/限流/服务端错误）与**不可重试**
       （鉴权失败/参数非法）。对不可重试的错误重试只是浪费时间并放大配额消耗。
    3. **token 计量**：把 usage 累加进 AgentState，让成本可观测。
       没有这一项就无法回答「这轮对话花了多少钱」。

🤖 AI 避坑
    - 别在节点里直接 `await acompletion()` 后裸 try/except：那会把重试、
      分类、计量三件事散落到每个节点，改一次漏一个
    - 退避要带 jitter，否则一批并发请求会同时重试形成尖峰
    - 空 choices 是**模型侧的正常边界情况**（内容过滤/截断），不是崩溃，
      要给出可读兜底而不是伪装成"服务不可用"
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import TYPE_CHECKING, Any

from lingxi.config import get_settings
from lingxi.core import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger(__name__)


class LLMCallError(Exception):
    """LLM 调用失败（已耗尽重试或属不可重试类）。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmptyLLMResponseError(LLMCallError):
    """模型返回空 choices（内容过滤 / 截断 / 网关异常）。"""

    def __init__(self) -> None:
        super().__init__("模型返回空响应（choices 为空）", retryable=True)


# 可重试的错误特征：超时、限流、临时不可用
_RETRYABLE_MARKERS = (
    "timeout", "timed out", "rate limit", "ratelimit", "429",
    "500", "502", "503", "504",
    "overloaded", "temporarily unavailable", "connection",
    "apiconnectionerror", "serviceunavailable", "internalservererror",
)

# 明确不可重试：错误配置，重试多少次都一样
_FATAL_MARKERS = (
    "invalid api key", "authentication", "unauthorized", "401", "403",
    "model not found", "does not exist", "invalid_request", "context length",
    "maximum context", "unsupported",
)


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重试。

    先看致命特征（凭据/参数错），再看可重试特征；都没有时**按可重试处理**
    —— 网络类异常五花八门，宁可多重试一次也不要一次抖动就转人工。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return not any(m in text for m in _FATAL_MARKERS)


class TokenUsage:
    """一次或多次 LLM 调用的 token 累计。"""

    __slots__ = ("prompt_tokens", "completion_tokens", "calls")

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    def add(self, response: Any) -> None:
        self.calls += 1
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        # litellm 返回 pydantic 对象，也可能是 dict
        get = usage.get if isinstance(usage, dict) else lambda k, d=0: getattr(usage, k, d)
        self.prompt_tokens += int(get("prompt_tokens", 0) or 0)
        self.completion_tokens += int(get("completion_tokens", 0) or 0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "llm_calls": self.calls,
        }

    def merge(self, other: TokenUsage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls


def extract_content(response: Any) -> str:
    """取首条消息文本，兼容 content 为 list（部分网关返回分块数组）。"""
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise EmptyLLMResponseError()
    message = getattr(choices[0], "message", None)
    if message is None:
        raise EmptyLLMResponseError()
    content = getattr(message, "content", None)
    if isinstance(content, list):
        # OpenAI 兼容格式：[{"type":"text","text":"..."}, ...]
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(parts)
    return content or ""


async def call_llm(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    usage: TokenUsage | None = None,
) -> Any:
    """带重试的 LLM 调用。失败抛 LLMCallError。

    Args:
        messages: 完整消息历史
        tools: OpenAI function-calling 工具清单（None = 不带工具）
        usage: 可选的 token 计量累加器

    Returns:
        litellm 的原始响应对象（调用方自己读 choices / tool_calls）
    """
    from litellm import acompletion

    settings = get_settings()
    llm_cfg = settings.llm
    retries = settings.agent.llm_max_retries
    base_delay = settings.agent.llm_retry_base_delay

    kwargs: dict[str, Any] = {
        "model": llm_cfg.model,
        "messages": messages,
        "temperature": llm_cfg.temperature,
        "max_tokens": llm_cfg.max_tokens,
        "api_key": llm_cfg.api_key.get_secret_value() if llm_cfg.api_key else None,
        "base_url": llm_cfg.base_url,
        "timeout": 30,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            response = await acompletion(**kwargs)
            if usage is not None:
                usage.add(response)
            extract_content(response)  # 提前验空，把边界情况变成明确异常
            return response
        except EmptyLLMResponseError:
            # 空响应是可重试的（网关抖动），交给下一轮
            last_error = LLMCallError("模型返回空响应（choices 为空）", retryable=True)
        except Exception as e:  # noqa: BLE001 — 这里就是要兜住所有网关异常
            last_error = e
            if not is_retryable(e):
                log.warning("llm_call_fatal", error=f"{type(e).__name__}: {e}")
                raise LLMCallError(f"{type(e).__name__}: {e}", retryable=False) from e

        if attempt < retries:
            # 指数退避 + jitter：避免并发请求同时重试形成尖峰
            delay = base_delay * (2 ** attempt) * (0.5 + random.random())
            log.warning(
                "llm_call_retry",
                attempt=attempt + 1,
                max_retries=retries,
                delay=round(delay, 3),
                error=f"{type(last_error).__name__}: {last_error}",
            )
            await asyncio.sleep(delay)

    raise LLMCallError(
        f"LLM 调用失败（重试 {retries} 次后）: {type(last_error).__name__}: {last_error}",
        retryable=True,
    ) from last_error


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """同时兼容对象属性与 dict 形态的 chunk。

    litellm 归一化后是 pydantic 对象，但测试替身与部分网关会直接给 dict。
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class _ToolCallAccumulator:
    """按 index 聚合流式 tool_calls 增量。

    🔗 工程逻辑
        OpenAI 兼容网关把一次 tool_call 拆成多片：第 1 片带 id + name，
        后续片只带 arguments 的一部分（且 id/name 为 null）。
        不按 index 归并就会得到一堆"只有半截 arguments"的调用，
        json.loads 直接炸 —— 流式会话里工具调用就永远用不了。
    """

    def __init__(self) -> None:
        self._slots: dict[int, dict[str, Any]] = {}

    def feed(self, deltas: list[Any]) -> None:
        for delta in deltas:
            index = int(_field(delta, "index", 0) or 0)
            slot = self._slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call_id = _field(delta, "id")
            if call_id:
                slot["id"] = call_id
            fn = _field(delta, "function")
            if fn is not None:
                name = _field(fn, "name")
                if name:
                    slot["name"] = name
                args = _field(fn, "arguments")
                if isinstance(args, str):
                    slot["arguments"] += args

    def drain(self) -> list[dict[str, Any]]:
        """取出聚合结果并清空。"""
        out = [
            {
                "id": slot["id"],
                "tool_name": slot["name"],
                "arguments": slot["arguments"],
            }
            for _, slot in sorted(self._slots.items())
            if slot["name"]
        ]
        self._slots.clear()
        return out


async def stream_llm(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """流式 LLM 调用。

    Yields:
        ``{"type": "token", "content": "..."}``      —— 文本增量
        ``{"type": "tool_call", "tool_name": ..., "arguments": {...}, "id": ...}``

    🔗 工程逻辑
        走 `stream=True`，由 LiteLLM 统一处理各家网关的流式协议差异；
        tool_calls 的多片增量由 `_ToolCallAccumulator` 按 index 归并。
    """
    from litellm import acompletion

    settings = get_settings()
    llm_cfg = settings.llm
    kwargs: dict[str, Any] = {
        "model": llm_cfg.model,
        "messages": messages,
        "temperature": llm_cfg.temperature,
        "max_tokens": llm_cfg.max_tokens,
        "api_key": llm_cfg.api_key.get_secret_value() if llm_cfg.api_key else None,
        "base_url": llm_cfg.base_url,
        "timeout": 30,
        "stream": True,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    response = await acompletion(**kwargs)
    acc = _ToolCallAccumulator()
    async for chunk in response:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = _field(choices[0], "delta")
        if delta is None:
            continue
        content = _field(delta, "content")
        if isinstance(content, str) and content:
            yield {"type": "token", "content": content}
        tool_deltas = _field(delta, "tool_calls")
        if tool_deltas:
            acc.feed(list(tool_deltas))

    for call in acc.drain():
        raw_args = call["arguments"]
        try:
            parsed = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        payload: dict[str, Any] = {
            "type": "tool_call",
            "tool_name": call["tool_name"],
            "arguments": parsed,
        }
        if call["id"]:
            payload["id"] = call["id"]
        yield payload


__all__ = [
    "EmptyLLMResponseError",
    "LLMCallError",
    "TokenUsage",
    "call_llm",
    "extract_content",
    "is_retryable",
    "stream_llm",
]
