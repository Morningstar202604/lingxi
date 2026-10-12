"""Node 4.1 — LangGraph Agent State 定义。

🔗 工程逻辑
    LangGraph 用 TypedDict 定义状态 graph（节点修改它，边驱动流转）。
    这个 state 是 Agent 单次 LLM 推理的完整上下文：
    - incoming: 从 IM 进来的原始消息
    - persona: PersonaInjector 生成的 system prompt
    - memory: 从记忆管线召回的 block
    - messages: LLM 消息列表（system + history + tool calls）
    - current_step: 当前 ReAct 步（from / reason / tool / escalate）
    - tool_calls: 本轮 LLM 想要调用的工具
    - tool_results: 工具返回的观测
    - final回复: 最终发给用户的文本

🤖 AI 避坑
    - 不要用 dataclass 做 LangGraph state，必须用 TypedDict（序列化/检查点持久化）
    - 不要放大型不可序列化对象（LLM client、DB connection）进 state
    - total_tokens 这类累计值放在 state graph 里也有问题（覆盖而非累加），用 Annotated + add
"""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class ToolCall(TypedDict):
    """一次工具调用请求。

    id: 模型侧返回的 tool_call_id。走 OpenAI function-calling 协议时必须原样回填，
    否则下一次请求里的 assistant.tool_calls 找不到配对的 tool 消息，
    服务端会直接报 400（OpenAI / DeepSeek / Qwen 等都会校验配对）。
    """

    tool_name: str
    arguments: dict[str, Any]
    id: NotRequired[str]


class ToolResult(TypedDict):
    """一次工具调用结果。"""

    tool_name: str
    success: bool
    observation: str


class AgentState(TypedDict):
    """LangGraph ReAct Agent 的完整状态。"""

    # ── 输入（entry 节点写入，全程只读）──
    user_id: str
    chat_id: str
    channel: str
    raw_message: str
    system_prompt: str  # PersonaInjector 生成的完整 system prompt

    # ── 上下文（reason 节点消费）──
    memory_block: str  # 记忆块（由 PersonaInjector 拼进 system prompt，不由 reason 节点直读）
    lorebook_block: str  # 世界观块（同上，注入 system prompt）

    # ── 消息历史（reason 节点消费 & 修改）──
    messages: list[dict[str, Any]]  # [{role, content, ...}, ...]

    # ── step 控制 ──
    # 刻意不用 Annotated 累加器：LangGraph 的 add reducer 会跨 ainvoke 调用持续累加，
    # 使第 N 轮的 step_count 叠加前 N-1 轮残留，会话很快触顶 max_steps 而永久降级
    # （见 docs/AUDIT-2026-10-10.md #1）。改为 LastValue：每轮由调用方传 0 归零，
    # 节点返回 state["step_count"] + 1 自增。
    step_count: int  # 本轮 ReAct 步数（每轮归零）
    max_steps: int  # 步数上限
    stage: str  # opening / gathering / resolving / closing
    emotion: str  # calm / pleased / neutral / irritated / apologetic

    # ── 工具调用 ──
    pending_tool_call: ToolCall | None
    # 模型一轮可能返回多个 tool_calls，全部执行并逐个回填 tool 消息
    pending_tool_calls: list[ToolCall]
    tool_results: list[ToolResult]
    # "工具名 + 参数" 的指纹序列，用于检测模型陷入重复调用死循环
    tool_call_signatures: list[str]

    # ── 成本 ──
    # 跨轮累加的 token 用量（prompt/completion/total/calls）。
    # 没有它就无法回答"这轮对话花了多少钱"，成本不可观测等于不可控。
    token_usage: dict[str, int]

    # ── 分级权限 ──
    # 由 pipeline 按 user/chat 解析后写入（见 SecurityConfig.permission_rules），
    # execute_tool 节点读它做鉴权 —— 不再是进程级全局常量。
    current_permission: int

    # ── 控制流 ──
    next_step: Literal["reason", "tool", "escalate", "respond", "done"]

    # ── 输出 ──
    final_reply: str
    escalate_reason: str
    confidence: float


__all__ = ["AgentState", "ToolCall", "ToolResult"]
