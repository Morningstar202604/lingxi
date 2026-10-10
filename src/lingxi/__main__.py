"""智犀 (ZhiXi) 引擎入口 — `python -m lingxi`。

🔗 工程逻辑
     开发模式启动序列（README 快速开始承诺）：
         configure_logging → build_default_engine → NapCat adapter.run()
     引擎本体只是 Layer 0-5 组件的装配线，不新增任何编排逻辑：
         Agent(L4) + Pipeline(L5) + Memory(L3) + Persona(L2) + IM(L1)

     Gewechat（微信）走 HTTP 回调模型，需要额外的 FastAPI 回调服务，
     当前切片只装配 NapCat（QQ）通道；微信回调服务随 HTTP server 切片补齐。

🤖 AI 避坑
     - build_default_engine() 绝不打开网络连接（连接只发生在 adapter.run()）
     - 事件处理异常由 IMAdapter._dispatch 兜底捕获，不会杀死收发循环
     - Ctrl+C → asyncio.run 取消主任务 → adapter 的 CancelledError 分支优雅退出
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lingxi.agent.graph import get_agent
from lingxi.agent.tools import _create_default_registry
from lingxi.config import get_settings
from lingxi.core import IMEvent, OutboundMessage, configure_logging, get_logger
from lingxi.flow.pipeline import CustomerServicePipeline
from lingxi.flow.queue import EscalationQueue
from lingxi.flow.ticket import TicketStore
from lingxi.im.napcat import NapCatAdapter
from lingxi.memory import MemoryPipeline
from lingxi.persona.injector import PersonaInjector
from lingxi.persona.schema import PersonaConfig

if TYPE_CHECKING:
    from lingxi.persona.guardrails import PermissionLevel

log = get_logger(__name__)

# L1：读 + 低风险写（发消息/建工单），与 BLUEPRINT 权限分级对齐
_PERMISSION_LEVEL: PermissionLevel = 1


@dataclass
class Engine:
    """智犀运行时：全部组件装配完成、尚未连网的状态。"""

    agent: Any
    pipeline: CustomerServicePipeline
    adapter: NapCatAdapter
    injector: PersonaInjector
    tickets: TicketStore
    queue: EscalationQueue
    memory: MemoryPipeline

    async def run(self) -> None:
        """阻塞运行直到 Ctrl+C / stop。"""
        await self.adapter.run()


def build_default_engine() -> Engine:
    """无网络装配引擎（可测试、可复用）。"""
    settings = get_settings()

    memory = MemoryPipeline()
    tickets = TicketStore()
    queue = EscalationQueue()
    injector = PersonaInjector(PersonaConfig())
    registry = _create_default_registry(
        blocked_commands=settings.security.blocked_commands,
    )
    agent = get_agent(
        registry=registry,
        permission_level=_PERMISSION_LEVEL,
        max_steps=settings.agent.react_steps,
    )

    adapter = NapCatAdapter(config=settings.onebot)

    async def _send(msg: OutboundMessage) -> None:
        """统一发送出口：走 IM 适配器，失败只记日志不抛（不打断主循环）。"""
        ok = await adapter.send(msg)
        if not ok:
            log.warning("outbound_send_failed", target=msg.target_id)

    pipeline = CustomerServicePipeline(
        agent=agent,
        ticket_store=tickets,
        escalation_queue=queue,
        registry=registry,
        confidence_threshold=settings.agent.confidence_threshold,
        memory_pipeline=memory,
        send_message_func=_send,
    )

    async def _on_event(event: IMEvent) -> None:
        """IM 事件 → persona system prompt + 记忆召回 → 客服管线。"""
        memory_block = ""
        if event.text:
            memory_block = memory.recall(
                user_id=event.user_id, query=event.text,
            ).render_for_prompt()
        # 记忆必须由 injector 拼进 system prompt：reason 节点不读 state.memory_block，
        # 只把它写进 state 等于凭空消失（见 docs/AUDIT-2026-10-10.md #17）
        system_prompt = injector.build_system_prompt(memory_block=memory_block)
        await pipeline.handle_event(
            event,
            system_prompt=system_prompt,
            memory_block=memory_block,
            send_func=_send,
            current_permission=_PERMISSION_LEVEL,
        )

    adapter._on_event = _on_event

    return Engine(
        agent=agent,
        pipeline=pipeline,
        adapter=adapter,
        injector=injector,
        tickets=tickets,
        queue=queue,
        memory=memory,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lingxi",
        description="智犀 (ZhiXi) — 二十四小时在线的全栈数字员工引擎",
    )
    parser.add_argument(
        "--version", action="version", version=f"lingxi {_version()}",
    )
    return parser.parse_args(argv)


def _version() -> str:
    from lingxi import __version__

    return __version__


def main(argv: list[str] | None = None) -> None:
    """CLI 入口。"""
    _parse_args(argv)
    settings = get_settings()
    configure_logging("DEBUG" if settings.debug else settings.log_level)

    engine = build_default_engine()
    log.info(
        "zhixi_engine_starting",
        ws_url=settings.onebot.ws_url,
        model=settings.llm.model,
    )
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        log.info("zhixi_engine_stopped_by_user")
    finally:
        log.info("zhixi_engine_exited")


if __name__ == "__main__":
    main()
