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
import contextlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lingxi.agent.tools import register_tool_bridge
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
    from lingxi.memory.lorebook import Lorebook
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
        confidence_threshold: float = 0.6,
        max_agent_timeout: float = 45.0,
        memory_pipeline: Any = None,
        send_message_func: SendFunc | None = None,
        lorebook: Lorebook | None = None,
        knowledge: Any = None,
        rule_engine: Any = None,  # RuleEngine | None
        pii_guard: Any = None,  # PIIGuard | None
        audit_log: Any = None,  # AuditLog | None
    ) -> None:
        self._agent = agent
        self._tickets = ticket_store
        self._queue = escalation_queue
        self._confidence_threshold = confidence_threshold
        self._max_agent_timeout = max_agent_timeout
        self._memory = memory_pipeline
        self._lorebook = lorebook
        self._knowledge = knowledge
        self._rule_engine = rule_engine
        # Phase 2/3: PII 脱敏与审计
        self._pii = pii_guard
        self._audit = audit_log

        # 并发会话限流：防止过多并发 Agent 调用耗尽 LLM quota / 内存
        max_concurrent = get_settings().agent.max_concurrent_sessions
        self._semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent > 0 else None

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
        """从 chat_id 推断渠道（基于 Gewechat wxid 格式特征）。

        Gewechat wxid 格式：私聊以 "wxid_" 开头，群聊以 "@chatroom" 结尾。
        QQ 号为纯数字，不匹配以上模式 → 默认 QQ_PRIVATE。
        """
        cid = str(chat_id)
        if cid.endswith("@chatroom"):
            return ChannelType.WECHAT_GROUP
        if cid.startswith("wxid_"):
            return ChannelType.WECHAT_PRIVATE
        return ChannelType.QQ_PRIVATE

    async def handle_event(
        self,
        event: IMEvent,
        system_prompt: str,
        memory_block: str = "",
        keyword_escalate_reason: str = "",
        send_func: SendFunc | None = None,
        current_permission: PermissionLevel | None = None,
    ) -> PipelineResult:
        """处理一条 IM 事件（主入口）。

        current_permission 由调用方按 user/chat 解析（resolve_permission_level），
        传入后会写进 AgentState 供 execute_tool 节点鉴权；None 则用引擎默认值。
        """
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

        # ── 规则引擎评估: escalate 命中则短路升级 ──
        if self._rule_engine is not None:
            rule_event: dict[str, Any] = {
                "message": text,
                "confidence": 1.0,
                "ticket_status": "intake",
                "channel": getattr(event.channel, "value", str(event.channel)),
                "elapsed_seconds": 0,
            }
            rule_actions = self._rule_engine.evaluate(rule_event)
            if any(a.action == "escalate" for a in rule_actions):
                ticket = self._tickets.get_or_create(
                    chat_id=event.chat_id,
                    user_id=event.user_id,
                    channel=event.channel,
                )
                ticket.category = classify_intent(text)
                ticket.add_message("user", text)
                log.info("rule_engine_escalation", rule_names=[a.action for a in rule_actions])
                return await self._escalate_and_respond(
                    ticket=ticket,
                    reason="rule_engine_match",
                    reply_text="已为您优先接入人工坐席，请稍候…",
                    send_func=send_func,
                )

        # ── 关键词强制升级: 匹配则直接跳 agent, 短路升级 ──
        if keyword_escalate_reason:
            ticket = self._tickets.get_or_create(
                chat_id=event.chat_id,
                user_id=event.user_id,
                channel=event.channel,
            )
            ticket.category = classify_intent(text)
            ticket.add_message("user", text)
            log.info("keyword_forced_escalation", reason=keyword_escalate_reason)
            return await self._escalate_and_respond(
                ticket=ticket,
                reason=keyword_escalate_reason,
                reply_text="已为您优先接入人工坐席，请稍候…",
                send_func=send_func,
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
        # PII 脱敏: 写入 ticket 前先替换手机号/身份证/邮箱/银行卡
        stored_text = self._pii.sanitize_input(text) if self._pii is not None else text
        ticket.add_message("user", stored_text)

        # 记忆编码：从对话中提取结构化事实（异步，失败静默降级）
        if self._memory is not None and event.user_id and text:
            import asyncio as _asyncio
            _asyncio.create_task(
                self._memory.encode_and_store(
                    user_id=event.user_id,
                    text=text,
                    source=f"ticket:{ticket.ticket_id}",
                ),
            )

        # 2. 调用 Agent（带上完整对话历史 + 记忆块 + 世界观块）
        settings = get_settings()
        # PII 脱敏: system_prompt (含用户信息注入块) 送入 agent 前脱敏
        safe_prompt = self._pii.sanitize_input(system_prompt) if self._pii is not None else system_prompt
        history = self._build_conversation_messages(
            system_prompt=safe_prompt,
            ticket=ticket,
            budget_chars=settings.agent.context_char_budget,
        )
        # 关键词触发的世界观注入：Lorebook 按用户消息关键词匹配，
        # 命中的条目内容作为额外上下文供 Agent 参考（如退货政策、发货时效等）
        lorebook_block = ""
        if self._lorebook is not None and event.text:
            lorebook_block = self._lorebook.query(user_message=event.text)

        # RAG 知识库召回: 用户消息相关 chunks → 注入 system prompt (citation 溯源)
        knowledge_block = ""
        if self._knowledge is not None and text:
            try:
                knowledge_block = await asyncio.wait_for(
                    self._knowledge.build_context(text, top_k=5, max_chars=2000),
                    timeout=8.0,
                )
            except (TimeoutError, Exception) as e:
                log.warning("knowledge_recall_failed", error=str(e))

        # 合并系统提示: 脱敏后的 system_prompt + lorebook + RAG knowledge
        augmented_prompt = safe_prompt
        if lorebook_block:
            augmented_prompt += f"\n\n{lorebook_block}"
        if knowledge_block:
            augmented_prompt += f"\n\n{knowledge_block}"

        try:
            # 并发限流：超限时等待而非直接拒绝，避免用户消息丢失
            run_cm = self._semaphore if self._semaphore else contextlib.nullcontext()
            async with run_cm:
                agent_result = await asyncio.wait_for(
                    self._agent.run(
                        raw_message=text,
                        chat_id=event.chat_id,
                        user_id=event.user_id,
                        channel=event.channel,
                        system_prompt=augmented_prompt,
                        memory_block=memory_block,
                        lorebook_block=lorebook_block,
                        messages=history,
                        permission_level=current_permission,
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

        # PII 脱敏: Agent 回复送出前先脱敏
        raw_reply = agent_result.get("final_reply", "")
        final_reply = self._pii.sanitize_output(raw_reply) if self._pii is not None else raw_reply
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

        # 审计日志: 升级事件 (fire-and-forget, 失败仅告警)
        self._record_audit(
            "ticket.escalated",
            actor="agent",
            target=ticket.ticket_id,
            detail=reason,
        )

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


    def _record_audit(self, action: str, actor: str, target: str = "", detail: str = "") -> None:
        """fire-and-forget 写入审计日志 (失败仅 warning, 不阻塞主流程)。"""
        if self._audit is None:
            return
        try:
            self._audit.record(action=action, actor=actor, target=target, detail=detail)
        except Exception as e:
            log.warning("pipeline_audit_failed", action=action, error=str(e))


__all__ = ["CustomerServicePipeline", "PipelineResult"]
