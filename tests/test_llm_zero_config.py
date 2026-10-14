"""LLM 零配置链路 —— 锁住"不配模型也能得到有用的回复"。

🔗 工程逻辑
    上一轮把默认 provider 改成 ollama / qwen2.5:7b 想实现"零配置即用"，
    但真实运行暴露了 4 个互相叠加的缺陷，每一个单看都不显眼：

    1. litellm 靠 model 字符串前缀认 provider，传裸 "qwen2.5:7b" 会报
       "LLM Provider NOT provided" —— 单测把 acompletion mock 掉了，看不见。
    2. stream_llm 的失败形态是 yield ``{"type": "error"}`` 而非抛异常，
       但 reason 节点只处理 token/tool_call → 空内容 → 图照常走完。
    3. 于是 SSE 只发 agent_start/agent_end，用户看到"发完什么都没出现"。
    4. 空内容被当成"还没想出结论"，白白重跑 max_steps/2 轮同样的失败调用。

    本文件把这四条逐条锁住。全部是**行为断言**：调用次数、流内容类型、
    传给 litellm 的 model 字符串，而不是"某个函数被调用过"。

🤖 AI 避坑
    - 断言 `model` 参数本身，而不是断言内部调了哪个 helper：
      provider 前缀是 litellm 的公开契约，锁契约不锁实现。
    - 关掉 fallback_models，否则测的是"第一个模型失败后会试什么"，
      与"主路径是否正确"混在一起。
"""
from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from lingxi.agent.llm import call_llm, resolve_model_name, stream_llm
from lingxi.config import get_settings
from lingxi.streaming.sse import _sse_event, _stream_native

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _resp(content: str) -> Any:
    r = AsyncMock()
    choice = AsyncMock()
    choice.message.content = content
    choice.message.tool_calls = None
    r.choices = [choice]
    r.usage = {"prompt_tokens": 1, "completion_tokens": 1}
    return r


class _Chunk:
    def __init__(self, text: str) -> None:
        self.choices = [AsyncMock()]
        self.choices[0].delta.content = text
        self.choices[0].delta.tool_calls = None


@pytest.fixture()
def no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉降级链，让断言只针对主模型。"""
    monkeypatch.setattr(get_settings().llm, "fallback_models", [], raising=False)


def _failing_stream(messages: Any, tools: Any = None) -> AsyncIterator[Any]:
    """替代 stream_llm：直接产出一条 error 事件（模拟所有模型都失败）。"""

    async def gen() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "error", "error": "模型没有返回任何内容"}

    return gen()


# ── 1. provider 前缀：litellm 的真实契约 ──


class TestModelNameResolution:
    @pytest.mark.parametrize(
        ("provider", "model", "want"),
        [
            ("ollama", "qwen2.5:7b", "ollama/qwen2.5:7b"),
            ("openai", "gpt-4o-mini", "openai/gpt-4o-mini"),
            ("anthropic", "claude-3-5-sonnet", "anthropic/claude-3-5-sonnet"),
            ("deepseek", "deepseek-chat", "deepseek/deepseek-chat"),
        ],
    )
    def test_prefix_added(self, provider: str, model: str, want: str) -> None:
        assert resolve_model_name(provider, model) == want

    def test_already_prefixed_left_alone(self) -> None:
        """模型名自带斜杠时不得二次加前缀。"""
        assert (
            resolve_model_name("openai", "openrouter/meta-llama/llama-3")
            == "openrouter/meta-llama/llama-3"
        )

    def test_litellm_provider_not_prefixed(self) -> None:
        """provider=litellm 时 litellm 自己就是 provider，加前缀反而找不到。"""
        assert resolve_model_name("litellm", "azure/gpt-4o") == "azure/gpt-4o"

    @pytest.mark.asyncio
    async def test_call_llm_sends_prefixed_model(self, no_fallback: None) -> None:
        """端到端：真正传给 litellm.acompletion 的 model 必须带 provider。"""
        seen: dict[str, Any] = {}

        async def capture(**kw: Any) -> Any:
            seen.update(kw)
            return _resp("好")

        with patch("litellm.acompletion", new=AsyncMock(side_effect=capture)):
            await call_llm([{"role": "user", "content": "hi"}])

        assert "/" in seen["model"], (
            f"传给 litellm 的 model 缺 provider 前缀: {seen['model']!r}；"
            "litellm 会报 'LLM Provider NOT provided'"
        )

    @pytest.mark.asyncio
    async def test_stream_llm_sends_prefixed_model(self, no_fallback: None) -> None:
        seen: dict[str, Any] = {}

        async def gen(**kw: Any) -> AsyncIterator[Any]:
            seen.update(kw)
            yield _Chunk("你")
            yield _Chunk("好")

        # side_effect 每次造一个新生成器：return_value 会被多个模型共用同一个
        # 已耗尽的生成器，第二个模型起就"静默成功"，把测试结论带偏。
        with patch("litellm.acompletion", new=AsyncMock(side_effect=lambda **kw: gen(**kw))):
            _ = [e async for e in stream_llm([{"role": "user", "content": "hi"}])]

        assert "/" in seen["model"], (
            f"流式路径同样必须带 provider 前缀: {seen.get('model')!r}"
        )


class TestBaseUrlNormalization:
    """base_url 必须按 provider 归一化。

    实测（2026-10-14）：litellm 的 ollama 走原生 API（/api/chat），
    默认 base_url 带 OpenAI 兼容的 /v1 时**每次调用都 404**
    （`OllamaException - 404 page not found`）——而配置默认值恰好是
    `http://127.0.0.1:11434/v1`，README 承诺的零配置路径实际跑不通。
    归一化层去掉该后缀，用户环境变量里写好的 /v1 也一并纠正。
    """

    def test_ollama_v1_suffix_stripped(self) -> None:
        from lingxi.agent.llm import resolve_base_url

        assert resolve_base_url("ollama", "http://127.0.0.1:11434/v1") == (
            "http://127.0.0.1:11434"
        )

    def test_ollama_trailing_slash_tolerated(self) -> None:
        from lingxi.agent.llm import resolve_base_url

        assert resolve_base_url("ollama", "http://127.0.0.1:11434/v1/") == (
            "http://127.0.0.1:11434"
        )

    def test_ollama_native_url_unchanged(self) -> None:
        from lingxi.agent.llm import resolve_base_url

        assert resolve_base_url("ollama", "http://127.0.0.1:11434") == (
            "http://127.0.0.1:11434"
        )

    def test_openai_compatible_keeps_v1(self) -> None:
        """OpenAI 兼容端点要求 /v1，不能被误删。"""
        from lingxi.agent.llm import resolve_base_url

        assert resolve_base_url("openai", "https://api.deepseek.com/v1") == (
            "https://api.deepseek.com/v1"
        )

    def test_none_passthrough(self) -> None:
        from lingxi.agent.llm import resolve_base_url

        assert resolve_base_url("ollama", None) is None

    @pytest.mark.asyncio
    async def test_call_llm_sends_normalized_base_url(self, no_fallback: None) -> None:
        """端到端：默认配置（带 /v1）传给 litellm 时已被去掉。"""
        from lingxi.agent.llm import call_llm

        seen: dict[str, Any] = {}

        async def capture(**kw: Any) -> Any:
            seen.update(kw)
            return _resp("好")

        with patch("litellm.acompletion", new=AsyncMock(side_effect=capture)):
            await call_llm([{"role": "user", "content": "hi"}])

        base = seen.get("base_url") or ""
        assert not base.endswith("/v1"), (
            f"ollama 的 base_url 带着 /v1 传给 litellm 必 404: {base!r}"
        )


class TestJsonMode:
    """无工具时下发 response_format（结构化输出走官方参数，不靠手搓解析）。"""

    @pytest.mark.asyncio
    async def test_json_mode_sets_response_format(self, no_fallback: None) -> None:
        from lingxi.agent.llm import call_llm

        seen: dict[str, Any] = {}

        async def capture(**kw: Any) -> Any:
            seen.update(kw)
            return _resp('{"answer": "好"}')

        with patch("litellm.acompletion", new=AsyncMock(side_effect=capture)):
            await call_llm([{"role": "user", "content": "hi"}], json_mode=True)

        assert seen.get("response_format") == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_tools_take_precedence_over_json_mode(self, no_fallback: None) -> None:
        """带工具时走 function calling，不同时下发 json 约束。"""
        from lingxi.agent.llm import call_llm

        seen: dict[str, Any] = {}

        async def capture(**kw: Any) -> Any:
            seen.update(kw)
            return _resp("好")

        tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
        with patch("litellm.acompletion", new=AsyncMock(side_effect=capture)):
            await call_llm(
                [{"role": "user", "content": "hi"}], tools=tools, json_mode=True
            )

        assert "response_format" not in seen, (
            "tools= 与 response_format 同时下发会被部分 provider 拒绝"
        )
        assert seen.get("tool_choice") == "auto"

    @pytest.mark.asyncio
    async def test_json_mode_off_by_default(self, no_fallback: None) -> None:
        from lingxi.agent.llm import call_llm

        seen: dict[str, Any] = {}

        async def capture(**kw: Any) -> Any:
            seen.update(kw)
            return _resp("好")

        with patch("litellm.acompletion", new=AsyncMock(side_effect=capture)):
            await call_llm([{"role": "user", "content": "hi"}])

        assert "response_format" not in seen


# ── 2. 失败必须是可见事件，而不是空流 ──


class TestStreamFailureIsVisible:
    @pytest.mark.asyncio
    async def test_failure_yields_error_event(self) -> None:
        """所有模型失败 → yield error 事件（不是 raise，也不是空流）。"""

        async def boom(**kw: Any) -> AsyncIterator[Any]:
            raise RuntimeError("gateway down")
            yield  # pragma: no cover

        with patch("litellm.acompletion", new=AsyncMock(side_effect=lambda **kw: boom(**kw))):
            events = [
                e async for e in stream_llm([{"role": "user", "content": "hi"}])
            ]

        assert events, "失败时流不能是空的"
        assert events[-1]["type"] == "error", events

    @pytest.mark.asyncio
    async def test_error_event_says_how_to_fix(self) -> None:
        """"没配好"必须告诉用户怎么配，否则等于把排查成本推回去。"""

        async def boom(**kw: Any) -> AsyncIterator[Any]:
            raise RuntimeError("gateway down")
            yield  # pragma: no cover

        with patch("litellm.acompletion", new=AsyncMock(side_effect=lambda **kw: boom(**kw))):
            events = [
                e async for e in stream_llm([{"role": "user", "content": "hi"}])
            ]

        msg = events[-1].get("error", "")
        assert "LINGXI_LLM__" in msg, f"缺少可执行的环境变量建议: {msg}"

    @pytest.mark.asyncio
    async def test_reason_node_consumes_error_event(self) -> None:
        """reason 节点必须消费 error 事件。

        漏掉这个分支时：parts 为空 → 图照常走完 → SSE 只剩
        agent_start/agent_end（用户表现 = 发完消息什么都没出现）。
        """
        import lingxi.agent.nodes as nodes_mod

        state = {
            "messages": [{"role": "user", "content": "hi"}],
            "chat_id": "c1",
            "step_count": 0,
            "max_steps": 4,
        }
        written: list[dict[str, Any]] = []

        class _Writer:
            def __call__(self, ev: dict[str, Any]) -> None:
                written.append(ev)

        class _Cfg:
            @staticmethod
            def get_stream_writer() -> Any:
                return _Writer()

        with (
            patch.dict(sys.modules, {"langgraph.config": _Cfg}),
            patch("lingxi.agent.llm.stream_llm", new=_failing_stream),
        ):
            update = await nodes_mod._reason_stream(state, None)  # type: ignore[no-any-return]

        assert any(w.get("type") == "error" for w in written), written
        assert update["next_step"] == "escalate", update
        assert update["final_reply"], "必须给用户一句能看的话，不能静默"


# ── 3. 空输出必须立即转人工，不许空转 ──


class TestEmptyOutputDoesNotSpin:
    def test_empty_content_escalates_immediately(self) -> None:
        """空内容不是"还没想出结论" → 一次就 escalate，不重跑多轮。"""
        from lingxi.agent.nodes import _build_reason_update

        state = {
            "messages": [{"role": "user", "content": "hi"}],
            "chat_id": "c1",
            "step_count": 0,
            "max_steps": 8,
        }
        update = _build_reason_update(state, "", None)
        assert update["next_step"] == "escalate", update
        assert update["step_count"] == 1, "空输出不应消耗多步预算"
        assert update["final_reply"], "必须给用户一句能看的话"

    def test_normal_text_still_responds(self) -> None:
        """回归锁：非空输出照旧走 respond，别把"空内容即转人工"改过头。"""
        from lingxi.agent.nodes import _build_reason_update

        state = {
            "messages": [{"role": "user", "content": "hi"}],
            "chat_id": "c1",
            "step_count": 0,
            "max_steps": 8,
        }
        update = _build_reason_update(state, "好的，已为您查询", None)
        assert update["next_step"] == "respond", update
        assert update["final_reply"] == "好的，已为您查询", update

    def test_whitespace_only_content_escalates(self) -> None:
        """只有空白也算空输出（网关有时返回单个换行）。"""
        from lingxi.agent.nodes import _build_reason_update

        state = {
            "messages": [{"role": "user", "content": "hi"}],
            "chat_id": "c1",
            "step_count": 0,
            "max_steps": 8,
        }
        assert _build_reason_update(state, "  \n\t ", None)["next_step"] == "escalate"


# ── 4. SSE 层契约：零内容流不允许 ──


class TestSSEContract:
    @pytest.mark.asyncio
    async def test_empty_agent_stream_becomes_error_event(self) -> None:
        """Agent 一个事件都不产 → 必须补一条 error，而不是静默结束。"""

        class _SilentAgent:
            async def astream(self, **kw: Any) -> AsyncIterator[dict[str, Any]]:
                return
                yield  # pragma: no cover

        events = [
            e
            async for e in _stream_native(
                _SilentAgent(), "hi", "c1", "u1", "qq_private", "SP", None,
            )
        ]
        assert [e["event"] for e in events] == ["error"], events

    @pytest.mark.asyncio
    async def test_real_reply_is_not_replaced_by_error(self) -> None:
        """有正常输出时不得追加兜底 error（否则用户看到成功后又报错）。"""

        class _Agent:
            async def astream(self, **kw: Any) -> AsyncIterator[dict[str, Any]]:
                yield {"type": "token", "content": "您好"}

        events = [
            e
            async for e in _stream_native(
                _Agent(), "hi", "c1", "u1", "qq_private", "SP", None,
            )
        ]
        assert [e["event"] for e in events] == ["message", "message"], events

    @pytest.mark.asyncio
    async def test_agent_error_event_forwarded_once(self) -> None:
        class _Agent:
            async def astream(self, **kw: Any) -> AsyncIterator[dict[str, Any]]:
                yield {"type": "error", "error": "网关超时"}

        events = [
            e
            async for e in _stream_native(
                _Agent(), "hi", "c1", "u1", "qq_private", "SP", None,
            )
        ]
        errors = [e for e in events if e["event"] == "error"]
        assert len(errors) == 1, events
        assert json.loads(errors[0]["data"])["error"] == "网关超时"


def test_sse_event_shape() -> None:
    ev = _sse_event("x", {"a": 1})
    assert ev["event"] == "x"
    assert json.loads(ev["data"]) == {"a": 1}
