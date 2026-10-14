"""LLM JSON 解析模块测试（agent/llm_json.py）——围栏变体全覆盖。

对应 AUDIT-2026-10-10 第四章 4.2 的老问题：手写围栏解析
`split("\n",1)` 处理不了单行 ```json```，且只认小写 tag。
"""
from __future__ import annotations

from lingxi.agent.llm_json import parse_reason_output, strip_code_fence


class TestStripCodeFence:
    def test_plain_text_untouched(self) -> None:
        assert strip_code_fence("你好") == "你好"

    def test_lowercase_json_fence(self) -> None:
        assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_uppercase_json_fence(self) -> None:
        """模型实测会输出 ```JSON（旧实现只认小写，解析直接失败）。"""
        assert strip_code_fence('```JSON\n{"a": 1}\n```') == '{"a": 1}'

    def test_other_language_tag(self) -> None:
        assert strip_code_fence('```jsonc\n{"a": 1}\n```') == '{"a": 1}'

    def test_single_line_fence(self) -> None:
        """单行围栏 ```{"a":1}```（旧 split("\\n",1) 的经典翻车点）。"""
        assert strip_code_fence('```{"a": 1}```') == '{"a": 1}'

    def test_bare_fence(self) -> None:
        assert strip_code_fence("```\n{\"a\": 1}\n```") == '{"a": 1}'


class TestParseReasonOutput:
    def test_plain_json_answer(self) -> None:
        out = parse_reason_output('{"answer": "订单已发货", "confidence": 0.9}', None)
        assert out["final_answer"] == "订单已发货"
        assert out["confidence"] == 0.9

    def test_uppercase_fenced_answer(self) -> None:
        out = parse_reason_output('```JSON\n{"answer": "ok"}\n```', None)
        assert out["final_answer"] == "ok"

    def test_single_line_fenced_tool(self) -> None:
        out = parse_reason_output(
            '```{"tool": "knowledge_search", "tool_args": {"query": "退款"}}```',
            None,
        )
        assert out["tool_call"]["tool_name"] == "knowledge_search"
        assert out["tool_call"]["arguments"]["query"] == "退款"

    def test_plain_text_fallback(self) -> None:
        out = parse_reason_output("这是普通回答", None)
        assert out["final_answer"] == "这是普通回答"

    def test_thinking_noise_not_kept(self) -> None:
        assert "final_answer" not in parse_reason_output("...", None)
        assert "final_answer" not in parse_reason_output("thinking", None)

    def test_broken_json_kept_as_text(self) -> None:
        """损坏的 JSON 不吞掉：整体按文本兜底，用户至少能看到内容。"""
        out = parse_reason_output('{"answer": "没闭合', None)
        assert out["final_answer"] == '{"answer": "没闭合'

    def test_structured_tool_calls_take_priority(self) -> None:
        class _Fn:
            name = "shell"
            arguments = '{"command": "ls", "confidence": 0.7}'

        class _TC:
            function = _Fn()
            id = "call_1"

        out = parse_reason_output("（模型文本被忽略）", [_TC()])
        assert out["tool_calls"][0]["tool_name"] == "shell"
        assert out["tool_calls"][0]["id"] == "call_1"
        assert out["confidence"] == 0.7
