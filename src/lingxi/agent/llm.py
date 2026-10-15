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

import json
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


async def _retrying_call(
    fn: Any,
    kwargs: dict[str, Any],
    *,
    retries: int,
    base_delay: float,
    model: str,
) -> Any:
    """带指数退避 + jitter 的调用（tenacity 官方实现）。

    🔗 工程逻辑
        重试策略（指数退避、随机抖动、停止条件）是 tenacity 的标准能力，
        不必手搓 `for attempt in range` + 手算 delay + asyncio.sleep。
        我们只提供领域知识：`is_retryable` 决定哪些异常值得再试
        （凭据/参数错重试就是白烧配额）。

    🤖 AI 避坑
        - `retry_if_exception(is_retryable)`：不可重试异常**直接抛出**，
          由调用方判定是否切下一个 fallback 模型（不要在这里吞掉）。
        - `reraise=True`：重试耗尽后抛最后一次的原始异常（而不是
          tenacity 的 RetryError），否则上层的错误分类与友好提示会失真。
        - 抖动用 wait_random 叠加：防止多实例同时重试形成惊群。
    """
    from tenacity import (
        AsyncRetrying,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential,
        wait_random,
    )

    attempts = retries + 1
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=max(base_delay, 0.001), exp_base=2, max=30)
        + wait_random(0, base_delay),
        retry=retry_if_exception(is_retryable),
        reraise=True,
    ):
        with attempt:
            if attempt.retry_state.attempt_number > 1:
                log.warning(
                    "llm_call_retry",
                    model=model,
                    attempt=attempt.retry_state.attempt_number - 1,
                    max_retries=retries,
                    error=str(attempt.retry_state.outcome.exception()) if attempt.retry_state.outcome else "",
                )
            return await fn(**kwargs)
    # 理论不可达（reraise=True 会抛出）；兜底避免类型检查报错
    raise LLMCallError("重试流程异常终止", retryable=False)


# litellm 通过 model 字符串的前缀决定走哪家 provider（"ollama/qwen2.5:7b"）。
# 配置里分开写 provider + model 是给人看的，调用前必须合成 litellm 认的形态 ——
# 否则 litellm 报 "LLM Provider NOT provided"，而单测里 acompletion 被 mock，
# 这个错会一路绿灯到真实运行才炸。
_PREFIXABLE_PROVIDERS = frozenset(
    {"openai", "anthropic", "deepseek", "ollama", "gemini", "azure", "xai"}
)


def resolve_model_name(provider: str, model: str) -> str:
    """把 (provider, model) 合成 litellm 的 ``provider/model`` 字符串。

    规则（缺省即最省心的一种）：
    - model 已含 ``/`` → 原样返回（用户自己写了完整路径，尊重之）
    - provider == "litellm" → 原样返回（litellm 自己就是 provider，不该加前缀）
    - provider 在可加前缀名单里 → 拼 ``provider/model``
    - provider 未知 → 原样返回，交给 litellm 报错，别在这里猜

    🤖 AI 避坑
        ``ollama/qwen2.5:7b`` 里那个斜杠是 provider 分隔符；模型名本身
        带斜杠的（如 ``openrouter/meta-llama/llama-3``）已在第一步被
        ``contains("/")`` 拦下，不会被二次加前缀。
    """
    if not provider or provider == "litellm" or not model:
        return model
    if "/" in model:
        return model
    if provider in _PREFIXABLE_PROVIDERS:
        return f"{provider}/{model}"
    return model


# 各 provider 的 base_url 归一化规则。
#
# 背景（实测 2026-10-14）：litellm 的 ollama 走**原生 API**（/api/chat），
# 默认 base_url 若带 OpenAI 兼容的 /v1 后缀，每次调用都是
# `APIConnectionError: OllamaException - 404 page not found`。
# README 承诺"装好 Ollama 即默认配置可用"，而默认 base_url 恰好是
# `http://127.0.0.1:11434/v1` —— 零配置路径实际跑不通（本地实测复现）。
#
# 处理方式：在归一化层按 provider 去掉该后缀，而不是改配置默认值 ——
# 用户环境变量里已经写好的 /v1 也一并纠正，不需要用户改配置。
_STRIP_V1_PROVIDERS = frozenset({"ollama"})


def resolve_base_url(provider: str, base_url: str | None) -> str | None:
    """按 provider 归一化 base_url（官方客户端要求的形态）。

    - ollama：litellm 走原生 API，去掉尾部的 /v1（带它必 404）
    - 其余 provider：原样返回（OpenAI 兼容端点通常就要求 /v1）
    """
    if not base_url:
        return base_url
    if provider in _STRIP_V1_PROVIDERS:
        trimmed = base_url.rstrip("/")
        if trimmed.endswith("/v1"):
            return trimmed[:-3]
    return base_url


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
    json_mode: bool = False,
    usage: TokenUsage | None = None,
) -> Any:
    """带重试 + 多模型降级的 LLM 调用。失败抛 LLMCallError。

    降级链：配置模型 → fallback_models → 最后兜底报错（含友好提示）

    Args:
        messages: 完整消息历史
        tools: OpenAI function-calling 工具清单（None = 不带工具）
        json_mode: True 时下发 response_format={"type": "json_object"}，
            让服务端保证输出是合法 JSON（litellm 官方参数，各 provider 声明
            支持）。仅在**无工具**时生效：带 tools= 时模型走 function calling，
            两个约束同时下发会被部分 provider 拒绝。
        usage: 可选的 token 计量累加器

    Returns:
        litellm 的原始响应对象（调用方自己读 choices / tool_calls）
    """
    from litellm import acompletion

    settings = get_settings()
    llm_cfg = settings.llm
    retries = settings.agent.llm_max_retries
    base_delay = settings.agent.llm_retry_base_delay

    # 构建尝试链：主模型 + 备用模型。
    # 合成 litellm 的 "provider/model" 形态；只在这里做一次，
    # call_llm / stream_llm 共用同一个 helper，别处再拼一遍必然漂移。
    models_to_try = [llm_cfg.model] + [
        m for m in llm_cfg.fallback_models if m != llm_cfg.model
    ]
    wire_models = [resolve_model_name(llm_cfg.provider, m) for m in models_to_try]

    # ── Router 路径（多 key 轮转 / 熔断 / 预算）──
    # 只在配了多凭据或显式开启时启用；单 key 场景保持下面的直连路径
    # （Router 会多包一层，对零配置本地模型是负收益）。
    try:
        from lingxi.agent.llm_router import build_router

        router = build_router(settings)
    except Exception as e:  # noqa: BLE001 — Router 不可用不影响直连
        log.warning("llm_router_unavailable", error=str(e))
        router = None

    if router is not None:
        router_kwargs: dict[str, Any] = {
            "messages": messages,
            "temperature": llm_cfg.temperature,
            "max_tokens": llm_cfg.max_tokens,
            "timeout": 30,
        }
        if tools:
            router_kwargs["tools"] = tools
            router_kwargs["tool_choice"] = "auto"
        elif json_mode:
            router_kwargs["response_format"] = {"type": "json_object"}
        try:
            response = await router.acompletion(**router_kwargs)
            if usage is not None:
                usage.add(response)
            extract_content(response)
            return response
        except Exception as e:  # noqa: BLE001
            # Router 内部已做过重试/轮转/冷却；这里再失败说明整组不可用，
            # 落到下面的直连路径做最后尝试（例如 Router 配置有误）
            log.warning("llm_router_call_failed_fallback_direct", error=str(e)[:200])

    for model_idx, (model, wire_model) in enumerate(
        zip(models_to_try, wire_models, strict=True),
    ):
        kwargs: dict[str, Any] = {
            "model": wire_model,
            "messages": messages,
            "temperature": llm_cfg.temperature,
            "max_tokens": llm_cfg.max_tokens,
            "api_key": llm_cfg.api_key.get_secret_value() if llm_cfg.api_key else None,
            # provider 归一化：ollama 必须去掉 /v1（litellm 走原生 API，带它必 404）
            "base_url": resolve_base_url(llm_cfg.provider, llm_cfg.base_url),
            "timeout": 30,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        elif json_mode:
            # 无工具时的结构化输出：让服务端保证 JSON 合法性，
            # 把"围栏/散文混排"的解析负担交还给 provider（llm_json 只兜底）
            kwargs["response_format"] = {"type": "json_object"}

        last_error: BaseException | None = None
        # 重试用 tenacity（官方标准库）：退避+jitter+重试判定交给它，
        # 我们只提供"哪些异常可重试"的领域知识（is_retryable）。
        # 此前是手搓 `for attempt in range` + 手算 delay + asyncio.sleep。
        try:
            response = await _retrying_call(
                acompletion, kwargs, retries=retries, base_delay=base_delay, model=model,
            )
            if usage is not None:
                usage.add(response)
            extract_content(response)
            if model_idx > 0:
                log.info("llm_fallback_succeeded", model=model, fallback_idx=model_idx)
            return response
        except EmptyLLMResponseError:
            last_error = LLMCallError("模型返回空响应（choices 为空）", retryable=True)
        except Exception as e:  # noqa: BLE001
            last_error = e
            if not is_retryable(e):
                log.warning("llm_call_fatal", model=model, error=f"{type(e).__name__}: {e}")
        # 该模型重试耗尽（或致命错误），尝试下一个 fallback
        log.warning(
            "llm_model_exhausted",
            model=model,
            next_fallback=(
                models_to_try[model_idx + 1]
                if model_idx + 1 < len(models_to_try)
                else None
            ),
        )
        continue

    # 所有模型都失败了，给出友好错误
    friendly_msg = _build_friendly_error(last_error)
    raise LLMCallError(friendly_msg, retryable=False) from last_error


def _build_friendly_error(last_error: BaseException | None) -> str:
    """生成用户友好的错误信息，包含排查建议。

    目标是「用户能照着做」：只说 "调用失败" 等于把排查成本推给用户。
    因此先判定**哪一类**没配好（没起服务 / 没下模型 / 没给凭据 / 名字写错），
    再只给对应的那几步 —— 全量罗列 4 步会让人不知道先做哪个。
    """
    settings = get_settings()
    cfg = settings.llm
    base = "所有可用模型均调用失败"
    if last_error:
        base += f"：{type(last_error).__name__}: {last_error}"

    err_text = f"{type(last_error).__name__} {last_error}".lower() if last_error else ""

    def has(*markers: str) -> bool:
        return any(m in err_text for m in markers)

    if has("api key", "unauthorized", "authentication", "401"):
        steps = [
            "当前 provider 是云端 API，但没给可用凭据：设置 "
            "LINGXI_LLM__API_KEY，或改用本地模型："
            "LINGXI_LLM__PROVIDER=ollama",
        ]
    elif has("provider not provided", "llm provider"):
        steps = [
            f"litellm 无法判断模型走哪家服务商：确认 LINGXI_LLM__PROVIDER 与 "
            f"LINGXI_LLM__MODEL 配置一致（当前 provider={cfg.provider}、model={cfg.model}）",
        ]
    elif has("model not found", "does not exist", "not found", "404"):
        steps = [
            f"服务端没有这个模型名：执行 `ollama list` 核对，"
            f"或用 `ollama pull {cfg.model}` 下载",
        ]
    elif has("connection", "refused", "unreachable", "name or service"):
        steps = [
            f"连不上 {cfg.base_url or '默认地址'}：确认本地模型服务已启动"
            "（Ollama：`ollama serve`），或在环境变量里改 LINGXI_LLM__BASE_URL",
        ]
    else:
        steps = [
            f"1. 核对配置：LINGXI_LLM__PROVIDER={cfg.provider}、"
            f"LINGXI_LLM__MODEL={cfg.model}、"
            f"LINGXI_LLM__BASE_URL={cfg.base_url}",
            "2. 用云端模型则补 LINGXI_LLM__API_KEY；用本地模型则先 "
            "`ollama serve` 并 `ollama pull <模型名>`",
            "3. 查看启动终端的完整异常堆栈（含 litellm 原始报错）",
        ]

    return base + "\n" + "\n".join(steps)


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
    """流式 LLM 调用（含多模型降级）。

    Yields:
        ``{"type": "token", "content": "..."}``      —— 文本增量
        ``{"type": "tool_call", "tool_name": ..., "arguments": {...}, "id": ...}``
        ``{"type": "error", "error": "..."}``        —— 所有模型失败时的友好错误

    🔗 工程逻辑
        走 `stream=True`，由 LiteLLM 统一处理各家网关的流式协议差异；
        tool_calls 的多片增量由 `_ToolCallAccumulator` 按 index 归并。
        模型失败时自动尝试 fallback_models，最终失败 yield 友好错误。
    """
    from litellm import acompletion

    settings = get_settings()
    llm_cfg = settings.llm
    models_to_try = [llm_cfg.model] + [
        m for m in llm_cfg.fallback_models if m != llm_cfg.model
    ]

    last_error: BaseException | None = None
    for model in models_to_try:
        kwargs: dict[str, Any] = {
            "model": resolve_model_name(llm_cfg.provider, model),
            "messages": messages,
            "temperature": llm_cfg.temperature,
            "max_tokens": llm_cfg.max_tokens,
            "api_key": llm_cfg.api_key.get_secret_value() if llm_cfg.api_key else None,
            # provider 归一化：ollama 必须去掉 /v1（litellm 走原生 API，带它必 404）
            "base_url": resolve_base_url(llm_cfg.provider, llm_cfg.base_url),
            "timeout": 30,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
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
            return  # 成功则返回，不再尝试后续模型

        except Exception as e:  # noqa: BLE001
            last_error = e
            log.warning("stream_llm_model_failed", model=model, error=f"{type(e).__name__}: {e}")
            continue  # 尝试下一个模型

    # 所有模型失败：yield 结构化 error 事件（不是 raise，也不是空流）
    yield {"type": "error", "error": _build_friendly_error(last_error)}


__all__ = [
    "EmptyLLMResponseError",
    "LLMCallError",
    "TokenUsage",
    "call_llm",
    "extract_content",
    "is_retryable",
    "resolve_model_name",
    "stream_llm",
]
