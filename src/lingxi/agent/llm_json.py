"""LLM 文本输出的结构化解析（JSON 围栏容忍 + tool_calls 归一）。

🔗 工程逻辑
    ReAct 节点的降级路径：模型不支持结构化输出时，会返回一段文本
    （常常是带 Markdown 围栏的 JSON）。解析逻辑独立成模块，便于单测；
    原先混在 nodes.py 里与四个节点函数纠缠（AUDIT-2026-10-10 第四章 4.5）。

🤖 AI 避坑
    - 围栏不能只认小写 ```json：模型实测会输出 ```JSON / ```jsonc /
      单行 ```{"a":1}``` —— 用正则剥"任意语言 tag + 首尾空白"
    - 解析失败不要把原文吞掉：非"思考噪声"的文本按 final_answer 兜底
    - tool_calls 列表是权威（模型一轮可返回多个）；tool_call 保留列表
      首项仅为兼容旧调用方
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lingxi.agent.state import ToolCall

# 开围栏：``` + 可选语言 tag（json/JSON/jsonc/...）+ 其后的空白（含换行）
_FENCE_OPEN = re.compile(r"^```[a-zA-Z0-9_.-]*\s*")
# 闭围栏：尾部的 ```（前面允许空白）
_FENCE_CLOSE = re.compile(r"\s*```\s*$")


def strip_code_fence(text: str) -> str:
    """剥掉 Markdown 代码围栏（任意语言 tag / 大小写 / 单行围栏都能处理）。"""
    if not text.startswith("```"):
        return text
    text = _FENCE_OPEN.sub("", text, count=1)
    text = _FENCE_CLOSE.sub("", text, count=1)
    return text


def parse_reason_output(content: str, tool_calls_raw: list[Any] | None) -> dict[str, Any]:
    """解析 LLM 返回的内容 + 结构化 tool_calls。

    支持 LLM 输出 confidence 字段（0.0–1.0），用于 pipeline 判断是否升级人工。
    期望的 JSON 格式（结构化输出不可用时的降级路径）：
        {"answer": "...", "confidence": 0.9}
        {"tool": "shell", "tool_args": {...}, "confidence": 0.6}
    """
    result: dict[str, Any] = {}

    # 优先：结构化 tool_calls（OpenAI function calling 格式）
    if tool_calls_raw:
        calls: list[ToolCall] = []
        for tc in tool_calls_raw:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            call: ToolCall = {
                "tool_name": tc.function.name,
                "arguments": args,
            }
            call_id = getattr(tc, "id", "") or ""
            if call_id:
                call["id"] = call_id
            # 尝试从 args 中提取 confidence
            if "confidence" in args and isinstance(args["confidence"], (int, float)):
                result["confidence"] = float(args["confidence"])
            calls.append(call)
        if calls:
            result["tool_calls"] = calls
            result["tool_call"] = calls[0]
        return result

    # 其次：文本中的 JSON 块
    text = strip_code_fence(content.strip())
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            if data.get("tool"):
                call = {
                    "tool_name": data["tool"],
                    "arguments": data.get("tool_args") or {},
                }
                result["tool_calls"] = [call]
                result["tool_call"] = call
            if data.get("answer"):
                result["final_answer"] = data["answer"]
            # LLM 显式置信度
            if "confidence" in data and isinstance(data["confidence"], (int, float)):
                result["confidence"] = float(data["confidence"])
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # 兜底：把整体输出作为 final_answer（只有意义明确时）
    if text and text.lower() not in ("...", "thinking", "thought"):
        result["final_answer"] = text
    return result


__all__ = ["parse_reason_output", "strip_code_fence"]
