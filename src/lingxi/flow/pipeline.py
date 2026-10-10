"""Node 5.3 — 客服流水线：将 IM 事件 → Agent 推理 → 回复/升级 串起来。

🔗 工程逻辑
    这是智犀 (ZhiXi) 的"主循环"：
    1. IM 事件进来（来自 NapCat/Gewechat 适配器）
    2. 创建/查找 Ticket
    3. 调用 LangGraph Agent（Layer 4）
    4. 根据置信度决定：直接回复 / 升级人工
    5. 回写 Ticket 状态 + 发送回复

🤖 AI 避坑
    - 不要在 IM 事件回调里直接 await 长耗时操作
    - Agent 调用必须有超时（否则用户等半天无回复 → 投诉）
    - 升级通知要异步推送（SSE/Webhook），不阻塞 Agent 响应
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lingxi.agent.tools import ToolRegistry, register_tool_bridge
from lingxi.config import get_settings
from lingxi.core import get_logger
from lingxi.core.protocol import ChannelType, IMEvent, OutboundMessage
from lingxi.flow.ticket import (
    Ticket,
    TicketCategory,
    TicketStatus,
    TicketStore,
    classify_intent,
)

if TYPE_CHECKING:
    from lingxi.agent.graph import ZhiXiAgent
    from lingxi.flow.queue import EscalationQueue
    from lingxi.persona.guardrails import PermissionLevel

log = get_logger(__name__)

# 类型别名
SendFunc = Callable[[OutboundMessage], Coroutine[Any, Any, None]]


@dataclass
class PipelineResult:
    """流水线处理结果。"""

    ticket_id: str
    replied: bool
    escalated: bool
    reply_text: str = ""
    escalation_reason: str = ""


class CustomerServicePipeline:
    """智犀 (ZhiXi) 客服流水线。

    将 IM 事件 → Agent 推理 → 回复/升级 完整串联。
    """

    def __init__(
        self,
        agent: ZhiXiAgent,
        ticket_store: TicketStore,
        escalation_queue: EscalationQueue,
        registry: ToolRegistry,
        confidence_threshold: float = 0.6,
        max_agent_timeout: float = 45.0,
        memory_pipeline: Any = None,
        send_message_func: SendFunc | None = None,
    ) -> None:
        self._agent = agent
        self._tickets = ticket_store
        self._queue = escalation_queue
        self._registry = registry
        self._confidence_threshold = confidence_threshold
        self._max_agent_timeout = max_agent_timeout
        self._memory = memory_pipeline
        self._send_func = send_message_func

        # 注册工具桥梁（让 Agent 内的 send_message / knowledge_search 真正生效）
        if send_message_func is not None:
            async def _bridge_send(chat_id: str, content: str) -> bool:
                try:
                    msg = OutboundMessage.text(
                        channel=self._detect_channel(chat_id),
                        target_id=chat_id, content=content,
                    )
                    await send_message_func(msg)
                    return True
                except Exception:
                    return False
            register_tool_bridge("send_message", _bridge_send)

        if memory_pipeline is not None:
            async def _bridge_search(user_id: str, query: str) -> str:
                result = memory_pipeline.recall(user_id=user_id, query=query, top_k=5)
                return str(result.render_for_prompt())
            register_tool_bridge("knowledge_search", _bridge_search)

    @staticmethod
    def _build_conversation_messages(
        system_prompt: str,
        ticket: Ticket,
        budget_chars: int,
    ) -> list[dict[str, Any]]:
        """把工单消息历史渲染成 LLM 的 messages 列表。

        Ticket.messages 是会话历史的业务真源（pipeline 每轮都在调 agent 前
        `ticket.add_message("user", text)`，故末条即当前轮用户消息）。
        若不传历史，agent 只能看到 system + 当前这条，用户一句"那它到哪了"
        会因为没有指代对象而答非所问（见 docs/AUDIT-2026-10-10.md #1）。

        超预算时从最新一轮往前裁剪，system prompt 永不丢弃（它是唯一的角色锚）。
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        history = ticket.messages
        kept: list[dict[str, Any]] = []
        used = len(system_prompt)
        for msg in reversed(history):
            role = "assistant" if msg.role in ("agent", "agent_escalated") else "user"
            entry = {"role": role, "content": msg.content}
            if used + len(msg.content) > budget_chars:
                break
            kept.append(entry)
            used += len(msg.content)
        messages.extend(reversed(kept))
        if len(kept) < len(history):
            log.info(
                "conversation_history_truncated",
                ticket_id=ticket.ticket_id,
                kept=len(kept),
                total=len(history),
            )
        return messages

    @staticmethod
    def _detect_channel(chat_id: str) -> ChannelType:
        """从 chat_id 前缀推断渠道。"""
        if "wxid" in str(chat_id) or "@chatroom" in str(chat_id):
            return ChannelType.WECHAT_GROUP if "@chatroom" in str(chat_id) else ChannelType.WECHAT_PRIVATE
        return ChannelType.QQ_PRIVATE  # qq 默认私聊

    async def handle_event(
        self,
        event: IMEvent,
        system_prompt: str,
        memory_block: str = "",
        send_func: SendFunc | None = None,
        current_permission: PermissionLevel = 1,
    ) -> PipelineResult:
        """处理一条 IM 事件（主入口）。"""
        text = event.text.strip()
        if not text:
            # 处理非文本消息（图片/语音/文件）：放行到后续 Agent 处理
            # 而不是静默丢弃。图片 URL 会在 system_prompt / context 中传递。
            text = event.text  # 尝试取 raw
            if not text:
                # 完全无文字：创建 ticket 并标记为 [非文本消息]
                ticket = self._tickets.get_or_create(
                    chat_id=event.chat_id,
                    user_id=event.user_id,
                    channel=event.channel,
                )
                if ticket.status == TicketStatus.INTAKE:
                    ticket.category = TicketCategory.OTHER
                    ticket.transition(TicketStatus.PROCESSING)
                image_urls = event.image_urls
                if image_urls:
                    ticket.add_message("user", f"[图片] {image_urls[0]}")
                else:
                    ticket.add_message("user", "[非文本消息]")
                return PipelineResult(
                    ticket_id=ticket.ticket_id,
                    replied=True,
                    escalated=False,
                    reply_text="收到您的消息，我需要看一下这个内容。",
                )

        # 1. 获取或创建票
        ticket = self._tickets.get_or_create(
            chat_id=event.chat_id,
            user_id=event.user_id,
            channel=event.channel,
        )

        # 分类（仅 intake 状态时分类一次）
        if ticket.status == TicketStatus.INTAKE:
            ticket.category = classify_intent(text)
            try:
                ticket.transition(TicketStatus.PROCESSING)
            except ValueError as e:
                log.debug("ticket_transition_skipped", error=str(e))

        # 记录用户消息（此后 ticket.messages 末条即当前轮，见 _build_conversation_messages）
        ticket.add_message("user", text)

        # 2. 调用 Agent（带上完整对话历史，否则多轮指代无从解析）
        settings = get_settings()
        history = self._build_conversation_messages(
            system_prompt=system_prompt,
            ticket=ticket,
            budget_chars=settings.agent.context_char_budget,
        )
        try:
            agent_result = await asyncio.wait_for(
                self._agent.run(
                    raw_message=text,
                    chat_id=event.chat_id,
                    user_id=event.user_id,
                    channel=event.channel,
                    system_prompt=system_prompt,
                    memory_block=memory_block,
                    messages=history,
                ),
                timeout=self._max_agent_timeout,
            )
        except TimeoutError:
            log.warning("agent_timeout", chat_id=event.chat_id)
            return await self._escalate_and_respond(
                ticket=ticket,
                reason="agent_timeout",
                reply_text="处理超时，为您转接人工。",
                send_func=send_func,
            )

        final_reply = agent_result.get("final_reply", "")
        escalate_reason = agent_result.get("escalate_reason", "")
        confidence = float(agent_result.get("confidence", 0.0))

        # 3. 判断：直接回复 or 升级
        # 升级条件：Agent 主动要求 / 无回复 / 置信度低于阈值
        if escalate_reason or not final_reply.strip():
            return await self._escalate_and_respond(
                ticket=ticket,
                reason=escalate_reason or "no_reply",
                reply_text=final_reply or "这个问题我需要转交同事处理。",
                send_func=send_func,
            )

        if confidence > 0 and confidence < self._confidence_threshold:
            return await self._escalate_and_respond(
                ticket=ticket,
                reason=f"low_confidence_{confidence:.2f}",
                reply_text=final_reply,
                send_func=send_func,
            )

        # 4. 直接回复
        ticket.add_message("agent", final_reply, confidence=confidence)
        try:
            ticket.transition(TicketStatus.WAITING_USER)
        except ValueError as e:
            log.debug("ticket_transition_skipped", error=str(e))

        if send_func:
            msg = OutboundMessage.text(
                channel=event.channel, target_id=event.chat_id, content=final_reply,
            )
            try:
                await send_func(msg)
            except Exception:
                log.exception("send_failed", chat_id=event.chat_id)

        return PipelineResult(
            ticket_id=ticket.ticket_id,
            replied=True,
            escalated=False,
            reply_text=final_reply,
        )

    async def _escalate_and_respond(
        self,
        ticket: Ticket,
        reason: str,
        reply_text: str,
        send_func: SendFunc | None = None,
    ) -> PipelineResult:
        """升级工单并发兜底回复。"""
        try:
            ticket.transition(TicketStatus.ESCALATED)
        except ValueError:
            if ticket.status == TicketStatus.ESCALATED:
                pass  # 已被升级过
            else:
                log.warning(
                    "escalate_failed",
                    ticket_id=ticket.ticket_id,
                    current=ticket.status.value,
                )

        ticket.escalation_reason = reason
        ticket.add_message("agent_escalated", reply_text)

        self._queue.enqueue(ticket, reason=reason)

        log.info("ticket_escalated", ticket_id=ticket.ticket_id, reason=reason)

        if send_func:
            # channel 字符串 → ChannelType 枚举
            ch_str = getattr(ticket, "channel", None) or "qq_private"
            from lingxi.core.protocol import ChannelType

            try:
                ch = ChannelType(ch_str)
            except ValueError:
                ch = ChannelType.QQ_PRIVATE
            msg = OutboundMessage.text(channel=ch, target_id=ticket.chat_id, content=reply_text)
            try:
                await send_func(msg)
            except Exception:
                log.exception("send_failed", chat_id=ticket.chat_id)

        return PipelineResult(
            ticket_id=ticket.ticket_id,
            replied=True,
            escalated=True,
            reply_text=reply_text,
            escalation_reason=reason,
        )


__all__ = ["CustomerServicePipeline", "PipelineResult"]
