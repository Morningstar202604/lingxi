"""Node 4.3 — LangGraph 节点实现：reason / execute_tool / escalate / respond。

🔗 工程逻辑
    每个节点是一个纯函数：接收 AgentState，返回要 merge 进 state 的 dict。
    LangGraph 用 TypedDict 的 key 级 merge（返回的 dict key 覆盖 state）。

    状态机流转：
        entry → reason → (tool → reason)* → respond
                  ↓ (权限不足 / 步数耗尽)      ↓
              escalate  ─────────────→ done

🤖 AI 避坑
    - 节点函数不要有 IO 副作用（不发消息，不写 DB），返回 observation 让 graph 决定下一步
    - 不要修改 state 里的已有字段（返回覆盖 dict，LangGraph 负责 merge）
    - reason 节点里调用 LLM 必须用 async + timeout
"""
from __future__ import annotations

import json
from typing import Any

from lingxi.agent.state import AgentState, ToolResult
from lingxi.agent.tools import ToolRegistry, execute_tool
from lingxi.config import get_settings
from lingxi.core import get_logger
from lingxi.persona.guardrails import PermissionLevel, PermissionDenied, detect_role_override

log = get_logger(__name__)


# ── 节点 1：reason（LLM 推理）──

async def reason_node(state: AgentState) -> dict[str, Any]:
    """调用 LLM 生成 thought +（可选）tool_call 或 final_answer。

    当 LLM 返回 tool_call 时，把 next_step 设为 "tool"；
    当直接给出 answer 时，next_step 设为 "respond"。
    """
    settings = get_settings()
    # 防止 role override 攻击
    if detect_role_override(state["messages"]):
        log.warning("role_override_detected", chat_id=state["chat_id"])
        return {
            "next_step": "escalate",
            "escalate_reason": "检测到 system 消息被篡改（可能的注入攻击）",
            "final_reply": "我无法继续处理这条消息，已转交人工审核。",
        }

    # 超出步数上限 → 友好的兜底回复（不直接 escalate，让 pipeline 的 confidence 机制决定）
    if state["step_count"] >= state["max_steps"]:
        log.warning("max_steps_reached", step=state["step_count"], max=state["max_steps"])
        return {
            "next_step": "respond",
            "final_reply": "这个问题我需要多一点时间思考，让我先简单回答：建议您提供更多细节，或者我可以帮您转接到真人同事。",
        }

    # 使用 litellm 异步调用 LLM
    try:
        from litellm import acompletion

        llm_cfg = settings.llm
        response = await acompletion(
            model=llm_cfg.model,
            messages=state["messages"],
            temperature=llm_cfg.temperature,
            max_tokens=llm_cfg.max_tokens,
            api_key=llm_cfg.api_key.get_secret_value() if llm_cfg.api_key else None,
            base_url=llm_cfg.base_url,
            timeout=30,
        )
        content = response.choices[0].message.content or ""
        tool_calls_raw = getattr(response.choices[0].message, "tool_calls", None)

    except Exception as e:
        log.exception("llm_call_failed", error=str(e))
        return {
            "next_step": "escalate",
            "escalate_reason": f"LLM 调用失败: {type(e).__name__}",
            "final_reply": "服务暂时不可用，请稍后再试。",
            "step_count": 1,
        }

    # 解析 LLM 输出
    parsed = _parse_reason_output(content, tool_calls_raw)

    update: dict[str, Any] = {
        "messages": [*state["messages"], {"role": "assistant", "content": content}],
        "step_count": 1,
    }

    if parsed.get("tool_call"):
        update["pending_tool_call"] = parsed["tool_call"]
        update["next_step"] = "tool"
    elif parsed.get("final_answer"):
        update["final_reply"] = parsed["final_answer"]
        update["next_step"] = "respond"
    else:
        # LLM 返回了 thought 但没明确结论 → 再试一次
        # 安全兜底：如果已经用了 ≥50% 步数还没结论，给一个兜底回复而非无限循环
        if state["step_count"] >= state["max_steps"] * 0.5:
            update["final_reply"] = "我已经思考了多步但尚未找到确切答案，建议您提供更多上下文或联系人工同事。"
            update["next_step"] = "respond"
        else:
            update["next_step"] = "reason"

    return update


def _parse_reason_output(content: str, tool_calls_raw: list | None) -> dict[str, Any]:
    """解析 LLM 返回的内容 + 结构化 tool_calls。"""
    result: dict[str, Any] = {}

    # 优先：结构化 tool_calls（OpenAI function calling 格式）
    if tool_calls_raw:
        tc = tool_calls_raw[0]
        try:
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
        except json.JSONDecodeError:
            args = {}
        result["tool_call"] = {
            "tool_name": tc.function.name,
            "arguments": args,
        }
        return result

    # 其次：文本中的 JSON 块
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            if data.get("tool"):
                result["tool_call"] = {
                    "tool_name": data["tool"],
                    "arguments": data.get("tool_args") or {},
                }
            if data.get("answer"):
                result["final_answer"] = data["answer"]
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # 兜底：把整体输出作为 final_answer（只有意义明确时）
    if text and text.lower() not in ("...", "thinking", "thought"):
        result["final_answer"] = text
    return result


# ── 节点 2：execute_tool ──

async def execute_tool_node(
    state: AgentState,
    registry: ToolRegistry,
    current_permission: PermissionLevel = 1,
) -> dict[str, Any]:
    """执行 pending_tool_call 中的工具调用。"""
    tool_call = state.get("pending_tool_call")
    if tool_call is None:
        return {"next_step": "reason", "step_count": 1}

    tool_name = tool_call["tool_name"]
    arguments = tool_call.get("arguments", {})

    exec_result = await execute_tool(
        registry=registry,
        tool_name=tool_name,
        arguments=arguments,
        current_permission=current_permission,
    )

    tool_result: ToolResult = {
        "tool_name": tool_name,
        "success": exec_result["success"],
        "observation": exec_result["observation"],
    }

    # 把工具返回追加进 messages（作为 user 角色，让 LLM 继续推理）
    tool_msg = {
        "role": "user",
        "content": f"[Tool: {tool_name}]\n{exec_result['observation']}",
    }

    # 权限不足 → 直接 escalate，不再让 LLM 继续
    next_step = "reason"
    escalate_reason = ""
    if not exec_result["success"] and "权限不足" in exec_result["observation"]:
        next_step = "escalate"
        escalate_reason = f"工具 '{tool_name}' 权限不足"

    return {
        "pending_tool_call": None,
        "tool_results": [*state["tool_results"], tool_result],
        "messages": [*state["messages"], tool_msg],
        "next_step": next_step,
        "escalate_reason": escalate_reason,
        "step_count": 1,
    }


# ── 节点 3：escalate ──

def escalate_node(state: AgentState) -> dict[str, Any]:
    """升级节点：暂停当前会话，等待人工接管。

    在真实实现中，这里会：
    1. 把当前会话状态写入 escalation queue（Redis / DB）
    2. 推送通知给坐席（Webhook / WebSocket）
    3. 发给用户一条兜底回复
    """
    reason = state.get("escalate_reason") or "unknown"
    log.warning("session_escalated", chat_id=state["chat_id"], reason=reason)

    fallback = (
        f"这个问题我需要转交给同事处理（原因：{_reason_zh(reason)}）。"
        f"请稍等，他们会尽快联系您。"
    )

    return {
        "next_step": "done",
        "final_reply": state.get("final_reply") or fallback,
        "escalate_reason": reason,
    }


def _reason_zh(reason: str) -> str:
    """把 escalate_reason 转成中文用户友好描述。"""
    mapping = {
        "max_steps_exceeded": "问题比较复杂",
        "权限不足": "权限受限",
        "injection_detected": "安全检测触发",
    }
    return mapping.get(reason, reason)


# ── 节点 4：respond ──

def respond_node(state: AgentState) -> dict[str, Any]:
    """最终回复节点：确保 final_reply 不为空，然后进入 done。"""
    reply = state.get("final_reply", "")
    if not reply.strip():
        reply = "已收到您的消息，我们会尽快处理。"

    return {
        "final_reply": reply,
        "next_step": "done",
    }
