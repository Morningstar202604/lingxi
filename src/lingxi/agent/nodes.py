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
from typing import TYPE_CHECKING, Any

from lingxi.agent.llm import LLMCallError, TokenUsage, call_llm
from lingxi.agent.llm import extract_content as _content_of
from lingxi.agent.llm_json import parse_reason_output as _parse_reason_output
from lingxi.agent.tools import (
    ToolRegistry,
    execute_tool,
    needs_approval,
    resolve_required_level,
)
from lingxi.config import get_settings
from lingxi.core import get_logger
from lingxi.persona.guardrails import (
    PERM_FORBIDDEN,
    InjectionVerdict,
    detect_injection,
    detect_role_override,
)

if TYPE_CHECKING:
    from lingxi.agent.state import AgentState, ToolCall, ToolResult
    from lingxi.persona.guardrails import PermissionLevel

log = get_logger(__name__)

# execute_tool 在权限/黑名单拒绝时使用的前缀（execute_tool_node 据此判定 escalate）
_PERMISSION_DENIED_PREFIXES = (
    "权限不足",
    "命令被黑名单拦截",
    "Shell 命令权限被拒绝",
)


# ── 节点 1：reason（LLM 推理）──

async def reason_node(
    state: AgentState,
    registry: ToolRegistry | None = None,
    *,
    stream: bool = False,
) -> dict[str, Any]:
    """调用 LLM 生成 thought +（可选）tool_call 或 final_answer。

    当 LLM 返回 tool_call 时，把 next_step 设为 "tool"；
    当直接给出 answer 时，next_step 设为 "respond"。

    registry 不为空时，会把工具清单（OpenAI function-calling 格式）随请求下发。
    这是"多步推理 + 工具调用"真正生效的唯一开关 —— 不传 tools= 时模型根本
    无从返回 tool_calls，只能输出散文（见 docs/AUDIT-2026-10-10.md #1）。
    """
    settings = get_settings()
    # 防线 2：system 消息被篡改（多 system / 角色覆盖）
    if detect_role_override(state["messages"]):
        log.warning("role_override_detected", chat_id=state["chat_id"])
        return {
            "next_step": "escalate",
            "escalate_reason": "检测到 system 消息被篡改（可能的注入攻击）",
            "final_reply": "我无法继续处理这条消息，已转交人工审核。",
        }

    # 防线 3：用户文本注入检测。此处此前从未接入生产路径，
    # 导致 guardrails.detect_injection 只是测试里的死代码（见 AUDIT-2026-10-10 #11）。
    user_text = state.get("raw_message", "")
    verdict, pattern = detect_injection(user_text)
    if verdict == InjectionVerdict.BLOCK:
        log.warning(
            "prompt_injection_blocked",
            chat_id=state["chat_id"],
            pattern=pattern,
        )
        return {
            "next_step": "escalate",
            "escalate_reason": "injection_detected",
            "final_reply": "这条消息包含我无法处理的内容，已转交人工处理。",
        }

    # 超出步数上限 → 友好的兜底回复（不直接 escalate，让 pipeline 的 confidence 机制决定）
    if state["step_count"] >= state["max_steps"]:
        log.warning("max_steps_reached", step=state["step_count"], max=state["max_steps"])
        return {
            "next_step": "respond",
            "final_reply": "这个问题我需要多一点时间思考，让我先简单回答：建议您提供更多细节，或者我可以帮您转接到真人同事。",
        }

    # 流式路径：逐 token 推给上层（经 LangGraph stream writer），
    # 与非流式共用同一套图与状态机，只是产出方式不同。
    if stream:
        return await _reason_stream(state, registry)

    # 调用 LLM（重试 / 退避 / token 计量 / 空响应防御都在这一层）
    usage = TokenUsage()
    tool_schemas = (
        registry.openai_tools()
        if registry is not None and settings.agent.tool_calling
        else None
    )
    try:
        response = await call_llm(
            state["messages"],
            tools=tool_schemas,
            # 无工具时要求服务端返回合法 JSON（结构化输出）。
            # 有工具时模型走 function calling，不再叠加 json 约束。
            json_mode=not tool_schemas,
            usage=usage,
        )
    except LLMCallError as e:
        # 鉴权/参数这类不可重试的错误不该说"稍后再试"，用户重试也没用
        reply = (
            "我暂时无法处理这条消息，已转交人工同事。"
            if e.retryable
            else "服务配置异常，已转交人工同事处理。"
        )
        log.error("llm_call_failed", error=str(e), retryable=e.retryable)
        return {
            "next_step": "escalate",
            "escalate_reason": f"LLM 调用失败: {type(e).__name__}",
            "final_reply": reply,
            "step_count": state["step_count"] + 1,
        }

    content = _content_of(response)
    tool_calls_raw = getattr(response.choices[0].message, "tool_calls", None)

    # 解析 / 置信度 / 路由 / assistant 消息构造全部收敛到 _build_reason_update，
    # 与流式路径共用同一份逻辑（否则两条路径会漂移）
    return _build_reason_update(state, content, tool_calls_raw, usage)


def _merge_token_usage(state: AgentState, usage: TokenUsage) -> dict[str, int]:
    """token 用量跨轮累加。

    LangGraph 的 LastValue 语义要求调用方每轮传入基数，所以节点里手工加上
    上一次的值。不用 Annotated 累加器 —— 那样会跨多轮对话无限累积，
    与 step_count 当初踩的是同一个坑。
    """
    prev = state.get("token_usage") or {}
    merged = usage.as_dict()
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "llm_calls"):
        merged[key] += int(prev.get(key, 0))
    return merged


async def _reason_stream(state: AgentState, registry: ToolRegistry | None) -> dict[str, Any]:
    """流式推理节点：逐 token 推事件，结束后走与非流式相同的状态流转。

    「解析 → 置信度 → next_step」抽在 `_build_reason_update()` 供两条路径共用，
    否则流式修好了非流式还是坏的，或反过来。
    """
    from langgraph.config import get_stream_writer

    from lingxi.agent.llm import stream_llm

    settings = get_settings()
    writer = get_stream_writer()
    tool_schemas = (
        registry.openai_tools()
        if registry is not None and settings.agent.tool_calling
        else None
    )

    parts: list[str] = []
    tool_calls_raw: list[Any] = []
    stream_error: str | None = None
    try:
        async for event in stream_llm(state["messages"], tools=tool_schemas):
            kind = event.get("type")
            if kind == "token":
                text = event.get("content", "")
                if text:
                    parts.append(text)
                    writer({"type": "token", "content": text})
            elif kind == "tool_call":
                # 流式同样支持工具调用：把聚合后的调用包成与
                # 非流式相同的 tool_calls 结构，复用 _build_reason_update
                tool_calls_raw.append(_FakeToolCall(
                    id=event.get("id", ""),
                    name=event.get("tool_name", ""),
                    arguments=json.dumps(event.get("arguments") or {}, ensure_ascii=False),
                ))
                writer({
                    "type": "tool_call",
                    "tool_name": event.get("tool_name", ""),
                    "arguments": event.get("arguments", {}),
                })
            elif kind == "error":
                # ⚠️ 别漏这个分支。stream_llm 的失败形态是 yield error 事件
                # 而不是抛异常（为了让前端拿到结构化错误），漏掉它时
                # parts 为空 → 图照常走完 → SSE 只剩 agent_start/agent_end，
                # 用户看到"发完消息什么都没有"。
                stream_error = event.get("error", "模型调用失败")
    except Exception as e:  # noqa: BLE001 — 流式边界统一转成 escalate 结论
        log.error("llm_stream_failed", chat_id=state["chat_id"], error=str(e))
        # 双向收敛：state 走 escalate（pipeline 会入队转人工），
        # 同时给前端一个 error 事件（连接不能被静默掐断）
        writer({"type": "error", "error": f"{type(e).__name__}: {e}"})
        return {
            "next_step": "escalate",
            "escalate_reason": f"LLM 调用失败: {type(e).__name__}",
            "final_reply": "服务暂时不可用，请稍后再试。",
            "step_count": state["step_count"] + 1,
        }

    if stream_error:
        log.error(
            "llm_stream_exhausted", chat_id=state["chat_id"], error=stream_error,
        )
        writer({"type": "error", "error": stream_error})
        return {
            "next_step": "escalate",
            "escalate_reason": "LLM 调用失败",
            "final_reply": stream_error,
            "step_count": state["step_count"] + 1,
        }

    content = "".join(parts)
    return _build_reason_update(state, content, tool_calls_raw or None)


class _FakeFunction:
    """流式 tool_call 的 function 部分（对齐 litellm 的 tc.function.name/arguments）。"""

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    """流式 tool_call 的最小替身 —— 让流式与非流式共用同一段解析逻辑。"""

    def __init__(self, id: str, name: str, arguments: str) -> None:
        self.id = id
        self.function = _FakeFunction(name, arguments)


def _build_reason_update(
    state: AgentState,
    content: str,
    tool_calls_raw: list[Any] | None,
    usage: TokenUsage | None = None,
) -> dict[str, Any]:
    """由 LLM 原始输出构造 state 增量（非流式与流式共用）。

    拆出来是为了让两条路径的「解析 / 置信度 / 路由」逻辑物理上只有一份 ——
    否则流式修好了非流式还是坏的，或反过来。
    """
    parsed = _parse_reason_output(content, tool_calls_raw)

    # 空输出不是"想不出结论"，而是**这次调用没拿到东西**。
    # 当成"再想一轮"会让图白跑 max_steps/2 次同样的失败调用（实测 4 轮），
    # 既拖慢响应又放大配额消耗。直接转人工并把原因带给用户。
    if not content.strip() and not parsed.get("tool_calls"):
        log.error(
            "llm_empty_output",
            chat_id=state.get("chat_id"),
            provider=get_settings().llm.provider,
            model=get_settings().llm.model,
        )
        return {
            "messages": [*state["messages"], {"role": "assistant", "content": ""}],
            "step_count": state["step_count"] + 1,
            "confidence": 0.0,
            "next_step": "escalate",
            "escalate_reason": "LLM 返回空内容",
            "final_reply": (
                "模型这次没有返回任何内容，我已转接人工同事协助处理。"
            ),
        }

    confidence = parsed.get("confidence") or 0.0
    if confidence == 0.0:
        if parsed.get("final_answer"):
            confidence = 0.85
        elif parsed.get("tool_calls"):
            confidence = 0.60
        else:
            confidence = 0.30

    assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
    if parsed.get("tool_calls"):
        wire_calls = [
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc["tool_name"],
                    "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                },
            }
            for tc in parsed["tool_calls"]
            if tc.get("id")
        ]
        # 只要有一个缺 id 就整体退回纯文本模式，避免半截 tool_calls 无法配对
        if len(wire_calls) == len(parsed["tool_calls"]):
            assistant_msg["tool_calls"] = wire_calls

    update: dict[str, Any] = {
        "messages": [*state["messages"], assistant_msg],
        "step_count": state["step_count"] + 1,
        "confidence": confidence,
    }

    if parsed.get("tool_calls"):
        calls: list[ToolCall] = parsed["tool_calls"]
        update["pending_tool_calls"] = calls
        update["pending_tool_call"] = calls[0]
        update["next_step"] = "tool"
    elif parsed.get("final_answer"):
        update["final_reply"] = parsed["final_answer"]
        update["next_step"] = "respond"
    else:
        # LLM 返回了 thought 但没明确结论 → 再试一次
        # 安全兜底：如果已经用了 ≥50% 步数还没结论，给一个兜底回复而非无限循环
        if state["step_count"] >= state["max_steps"] * 0.5:
            update["final_reply"] = (
                "我已经思考了多步但尚未找到确切答案，建议您提供更多上下文或联系人工同事。"
            )
            update["next_step"] = "respond"
            update["confidence"] = 0.20
        else:
            update["next_step"] = "reason"

    if usage is not None:
        update["token_usage"] = _merge_token_usage(state, usage)
    return update


# ── 节点 2：execute_tool ──

def _sanitize_arguments(
    state: AgentState,
    tool_name: str,
    tool_call: ToolCall,
) -> dict[str, Any]:
    """清洗模型给的工具入参。

    - 剔除 `confidence`（控制流字段，不是工具入参）
    - 受信任目标参数由代码从 AgentState 注入，覆盖模型提供的同名字段：
      模型无法据此向其他会话发消息 / 读他人记忆
    """
    arguments = dict(tool_call.get("arguments") or {})
    arguments.pop("confidence", None)

    if tool_name == "send_message":
        arguments.pop("chat_id", None)
        arguments.pop("current_chat_id", None)
        arguments["current_chat_id"] = state["chat_id"]
    elif tool_name == "knowledge_search":
        arguments.pop("user_id", None)
        arguments.pop("current_user_id", None)
        arguments["current_user_id"] = state["user_id"]
    return arguments


async def _request_approval(
    *,
    tool_call: ToolCall,
    arguments: dict[str, Any],
    required: int,
    state: AgentState,
) -> dict[str, Any]:
    """挂起等待人工审批。

    🔗 工程逻辑
        `interrupt()` 抛出的 GraphInterrupt 必须让它冒泡出去（图才会挂起），
        所以**不能**用 `except Exception` 包住调用本身 —— 只在"不在图内
        执行"（单测直接调节点）时降级为拒绝，避免测试环境假装通过审批。
    """
    from langgraph.errors import GraphInterrupt

    from lingxi.agent.approval import get_approval_store

    store = get_approval_store()
    request = store.create(
        tool_name=tool_call["tool_name"],
        arguments=arguments,
        required_level=required,
        requester_level=int(state.get("current_permission", 1)),
        chat_id=state["chat_id"],
        user_id=state["user_id"],
        channel=state["channel"],
    )
    payload = {
        "approval_id": request.approval_id,
        "tool_name": request.tool_name,
        "arguments": request.arguments,
        "required_level": required,
        "requester_level": request.requester_level,
        "chat_id": request.chat_id,
    }

    try:
        from langgraph.types import interrupt

        decision = interrupt(payload)
    except GraphInterrupt:
        raise
    except Exception as e:  # noqa: BLE001 — 不在图里跑（如单测）→ 拒绝
        log.warning("approval_outside_graph", tool=tool_call["tool_name"], error=str(e))
        return {"approved": False, "approver": "", "approval_id": request.approval_id}

    store.decide(
        request.approval_id,
        approved=bool(isinstance(decision, dict) and decision.get("approved")),
        approver=(decision or {}).get("approver", "") if isinstance(decision, dict) else "",
        note=(decision or {}).get("note", "") if isinstance(decision, dict) else "",
    )
    return {
        "approved": bool(isinstance(decision, dict) and decision.get("approved")),
        "approver": (decision or {}).get("approver", "") if isinstance(decision, dict) else "",
        "approval_id": request.approval_id,
    }


def _count_identical_run(state: AgentState, signatures: list[str]) -> int:
    """末尾连续相同工具调用的次数（含本次）。"""
    prior = list(state.get("tool_call_signatures") or [])
    if not signatures or not prior:
        return 1
    run = 1
    for prev in reversed(prior):
        if prev == signatures[-1]:
            run += 1
        else:
            break
    return run


async def execute_tool_node(
    state: AgentState,
    registry: ToolRegistry,
    current_permission: PermissionLevel = 1,
) -> dict[str, Any]:
    """执行 pending_tool_call 中的工具调用。

    流程：循环护栏 → 逐个定级/审批 → 执行并回填 tool 消息。
    - 一轮里模型可能返回多个 tool_calls：全部执行并逐个回填
      （漏回填会被服务端判为 tool_calls 未配对而报 400）
    - 权限取自 state.current_permission（按用户/会话解析），构造参数仅兜底
    - L2/L3 且权限不足 → interrupt 挂起等人工审批；L4 红线 → 直接拒绝
    """
    pending = list(state.get("pending_tool_calls") or [])
    single = state.get("pending_tool_call")
    if not pending and single:
        pending = [single]
    if not pending:
        return {"next_step": "reason", "step_count": state["step_count"] + 1}

    settings = get_settings()
    permission = int(state.get("current_permission", current_permission))
    signatures = [
        f"{tc['tool_name']}:{json.dumps(tc.get('arguments') or {}, sort_keys=True, ensure_ascii=False)}"
        for tc in pending
    ]

    # ── 1. 循环护栏（先于审批：别为失控循环反复打扰审批人）──
    identical_run = _count_identical_run(state, signatures)
    if identical_run >= settings.agent.max_identical_tool_calls:
        log.warning(
            "repeated_tool_call_short_circuit",
            tool=pending[0]["tool_name"],
            times=identical_run,
        )
        return {
            "pending_tool_call": None,
            "pending_tool_calls": [],
            "next_step": "escalate",
            "escalate_reason": (
                f"工具 '{pending[0]['tool_name']}' 被连续调用 "
                f"{identical_run} 次无进展，已停止并转人工"
            ),
            "step_count": state["step_count"] + 1,
        }

    # ── 2. 逐个定级 + 人工审批 ──
    prepared: list[tuple[ToolCall, dict[str, Any], int]] = []
    results: list[ToolResult] = []

    for tool_call in pending:
        tool_name = tool_call["tool_name"]
        arguments = _sanitize_arguments(state, tool_name, tool_call)
        required = resolve_required_level(registry, tool_name, arguments)

        if required >= PERM_FORBIDDEN:
            # 红线：无论谁批都不放行（BLUEPRINT「始终拒绝，仅代码层操作」）
            results.append({
                "tool_name": tool_name,
                "success": False,
                "observation": f"工具 '{tool_name}' 属红线操作，已拒绝执行。",
            })
            return _escalate_tool_result(
                state, results,
                f"工具 '{tool_name}' 属红线操作，已拒绝",
            )

        if needs_approval(required, permission):
            decision = await _request_approval(
                tool_call=tool_call,
                arguments=arguments,
                required=required,
                state=state,
            )
            if not decision["approved"]:
                approver = decision.get("approver") or "审批人"
                results.append({
                    "tool_name": tool_name,
                    "success": False,
                    "observation": f"操作被{approver}拒绝",
                })
                return _escalate_tool_result(
                    state, results,
                    f"工具 '{tool_name}' 的审批被{approver}拒绝",
                )
            # 审批通过 = 临时授予「本次调用」的权限，不是给用户提权
            permission = max(permission, required)

        prepared.append((tool_call, arguments, required))

    # ── 3. 执行 + 回填 ──
    new_msgs: list[dict[str, Any]] = []
    next_step = "reason"
    escalate_reason = ""

    for tool_call, arguments, _required in prepared:
        tool_name = tool_call["tool_name"]
        exec_result = await execute_tool(
            registry=registry,
            tool_name=tool_name,
            arguments=arguments,
            current_permission=permission,
        )
        results.append({
            "tool_name": tool_name,
            "success": exec_result["success"],
            "observation": exec_result["observation"],
        })

        _emit_stream_event({
            "type": "tool_call",
            "tool_name": tool_name,
            "arguments": arguments,
        })

        # 回填工具结果：带 id 的走标准 tool 角色；否则退化为 user 角色文本
        call_id = tool_call.get("id", "")
        if call_id:
            new_msgs.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": exec_result["observation"],
            })
        else:
            new_msgs.append({
                "role": "user",
                "content": f"[Tool: {tool_name}]\n{exec_result['observation']}",
            })

        _emit_stream_event({
            "type": "tool_result",
            "tool_name": tool_name,
            "observation": exec_result["observation"],
            "success": exec_result["success"],
        })

        # 权限不足 / 命令被拒 → 直接 escalate，不再让 LLM 反复重试
        observation = exec_result["observation"]
        if not exec_result["success"] and observation.startswith(_PERMISSION_DENIED_PREFIXES):
            next_step = "escalate"
            escalate_reason = f"工具 '{tool_name}' 被拒绝：{observation}"

    return {
        "pending_tool_call": None,
        "pending_tool_calls": [],
        "tool_results": [*state["tool_results"], *results],
        "messages": [*state["messages"], *new_msgs],
        "tool_call_signatures": [*list(state.get("tool_call_signatures") or []), *signatures][-10:],
        "next_step": next_step,
        "escalate_reason": escalate_reason,
        "step_count": state["step_count"] + 1,
    }


def _escalate_tool_result(
    state: AgentState,
    results: list[ToolResult],
    reason: str,
) -> dict[str, Any]:
    """工具层拒绝/审批失败的统一收敛：清空 pending 并转人工。"""
    return {
        "pending_tool_call": None,
        "pending_tool_calls": [],
        "tool_results": [*state["tool_results"], *results],
        "next_step": "escalate",
        "escalate_reason": reason,
        "step_count": state["step_count"] + 1,
    }


def _emit_stream_event(payload: dict[str, Any]) -> None:
    """向流式消费者推送事件；非流式上下文里静默跳过。

    `get_stream_writer()` 在没有 LangGraph 运行上下文时会抛
    RuntimeError/LookupError —— 那是正常路径（普通 run），不该影响执行。
    """
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(payload)
    except (RuntimeError, LookupError, ImportError):
        pass

# ── 节点 3：escalate ──

def escalate_node(state: AgentState) -> dict[str, Any]:
    """升级节点：暂停当前会话，交人工接管。

    🔗 工程逻辑
        这里只负责**产出结论**（最终回复 + 原因）；真正把工单入队、通知坐席、
        回写工单状态是 `flow/pipeline.py::_escalate_and_respond` 的职责。
        节点刻意不发消息、不写 DB —— 见文件头「节点函数不要有 IO 副作用」。
    """
    reason = state.get("escalate_reason") or "unknown"
    log.warning(
        "session_escalated",
        chat_id=state["chat_id"],
        reason=reason,
        steps=state.get("step_count", 0),
    )

    return {
        "next_step": "done",
        "final_reply": state.get("final_reply") or build_escalation_reply(reason),
        "escalate_reason": reason,
    }


# 升级原因 → 用户可读描述。
# 面向用户的话术不该暴露内部实现（"检测到 system 消息被篡改" 对用户无意义，
# 也不该把安全策略细节透给可能正在试探的攻击者）。
_REASON_ZH: dict[str, str] = {
    "max_steps_exceeded": "问题比较复杂",
    "权限不足": "权限受限",
    "injection_detected": "安全检测触发",
    "llm_unavailable": "服务暂时繁忙",
    "role_override_detected": "安全检测触发",
}


def _reason_zh(reason: str) -> str:
    """把 escalate_reason 转成中文用户友好描述。"""
    for key, text in _REASON_ZH.items():
        if key in reason:
            return text
    return reason


def build_escalation_reply(reason: str) -> str:
    """生成升级时给用户的兜底回复。"""
    return (
        f"这个问题我需要转交给同事处理（原因：{_reason_zh(reason)}）。"
        f"请稍等，他们会尽快联系您。"
    )


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
