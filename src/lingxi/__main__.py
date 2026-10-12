"""智犀 (ZhiXi) 引擎入口 — `python -m lingxi`。

🔗 工程逻辑
     开发模式启动序列（README 快速开始承诺）：
         configure_logging → build_default_engine → NapCat adapter.run()
     引擎本体只是 Layer 0-5 组件的装配线，不新增任何编排逻辑：
         Agent(L4) + Pipeline(L5) + Memory(L3) + Persona(L2) + IM(L1)

     支持双通道: NapCat (QQ) + Gewechat (微信 HTTP 回调)。
     Django Channels 模式: 多条 adapter 并行绑到同一个 pipeline。

🤖 AI 避坑
     - build_default_engine() 绝不打开网络连接（连接只发生在 adapter.run()）
     - 事件处理异常由 IMAdapter._dispatch 兜底捕获，不会杀死收发循环
     - Ctrl+C → asyncio.run 取消主任务 → adapter 的 CancelledError 分支优雅退出
     - Gewechat 的 HTTP 回调需注册到 Dashboard FastAPI 路由（/configure/gewechat/callback）
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from lingxi import __version__
from lingxi.agent.graph import get_agent
from lingxi.agent.tools import _create_default_registry
from lingxi.config import get_settings
from lingxi.core import IMEvent, OutboundMessage, configure_logging, get_logger
from lingxi.flow.dashboard_api import create_dashboard_app
from lingxi.flow.pipeline import CustomerServicePipeline
from lingxi.flow.queue import EscalationQueue
from lingxi.flow.ticket import TicketStore
from lingxi.im.gewechat import GewechatAdapter
from lingxi.im.napcat import NapCatAdapter
from lingxi.memory import MemoryPipeline
from lingxi.memory.graph_store import InMemoryGraphStore, SQLiteGraphStore
from lingxi.memory.lorebook import Lorebook, LorebookEntry
from lingxi.persona.guardrails import resolve_permission_level
from lingxi.persona.injector import PersonaInjector
from lingxi.persona.schema import PersonaConfig

# ── 品牌 ASCII 横幅 ──
_ZHIXI_BANNER = rf"""
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║        ███████╗██╗  ██╗██╗██╗  ██╗██╗                            ║
║        ╚══███╔╝██║  ██║██║╚██╗██╔╝██║                            ║
║          ███╔╝ ███████║██║ ╚███╔╝ ██║                            ║
║         ███╔╝  ██╔══██║██║ ██╔██╗ ██║                            ║
║        ███████╗██║  ██║██║██╔╝ ██╗██║                            ║
║        ╚══════╝╚═╝  ╚═╝╚═╝╚═╝  ╚═╝                               ║
║                                                                  ║
║        智 犀  Z h i X i   v{__version__}                            ║
║        智辨已知 · 犀照善为                                         ║
║        7×24 全栈数字员工引擎                                       ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

if TYPE_CHECKING:
    from lingxi.persona.guardrails import PermissionLevel

log = get_logger(__name__)


@dataclass
class Engine:
    """智犀运行时：全部组件装配完成、尚未连网的状态。

    支持多通道: NapCat (QQ, WebSocket) + Gewechat (微信, HTTP 回调)。
    多个 adapter 共享同一 pipeline/tickets/memory。
    """

    agent: Any
    pipeline: CustomerServicePipeline
    adapters: list[Any]  # NapCatAdapter / GewechatAdapter / ...
    injector: PersonaInjector
    tickets: Any  # TicketStore | PersistentTicketStore
    queue: Any  # EscalationQueue | PersistentEscalationQueue
    memory: Any  # MemoryPipeline | PersistentMemoryPipeline
    lorebook: Lorebook
    knowledge: Any = None  # KnowledgeService | None
    _sla_monitor: Any = None  # SLAMonitor | None
    _rule_engine: Any = None  # RuleEngine | None
    audit_log: Any = None  # AuditLog | None
    summarizer: Any = None  # SessionSummarizer | None
    pii_guard: Any = None  # PIIGuard | None
    experiment_engine: Any = None  # ExperimentEngine | None
    seat_store: Any = None  # SeatStore | None
    assigner: Any = None  # TicketAssigner | None
    permission_level: PermissionLevel = 1  # 未命中 permission_rules 时的基线等级

    def dashboard_app(self) -> Any:
        """构建与本引擎共享存储的 Dashboard FastAPI 应用。

        ⚠️ 此前这里只传了存储类，agent / experiment_engine / auth_enabled 全是
        None，而 dashboard_api.py 正是靠 `if xxx is not None` 决定是否注册路由 ——
        于是生产进程里 SSE 流式端点、A/B 实验端点、JWT 鉴权端点**一个都没挂上**
        （前端 6 处请求 /api/experiments 必然 404）。单元测试因为自己显式传参
        所以发现不了。现在把引擎持有的组件全部透传。
        """
        sla_monitor = getattr(self, "_sla_monitor", None)
        rule_engine = getattr(self, "_rule_engine", None)
        settings = get_settings()
        app = create_dashboard_app(
            self.tickets, self.queue, self.knowledge,
            sla_monitor=sla_monitor,
            rule_engine=rule_engine,
            audit_log=getattr(self, "audit_log", None),
            summarizer=getattr(self, "summarizer", None),
            agent=self.agent,
            experiment_engine=getattr(self, "experiment_engine", None),
            auth_enabled=settings.auth.enabled,
            seat_store=getattr(self, "seat_store", None),
            assigner=getattr(self, "assigner", None),
            persona_config=self.injector._persona,
        )
        # 注册 Gewechat 回调路由 (HTTP POST from 微信服务器)
        for adapter in self.adapters:
            gw_callback = getattr(adapter, "get_callback_route", None)
            if gw_callback is not None:
                route_info = gw_callback()
                app.add_api_route(
                    route_info["path"],
                    route_info["handler"],
                    methods=["POST"],
                    include_in_schema=False,
                )
                log.info("gewechat_callback_registered", path=route_info["path"])
        return app

    async def serve_dashboard(self, host: str, port: int) -> None:
        """把 Dashboard 以协程形式挂进当前事件循环（非阻塞）。

        用 uvicorn.Server.serve() 而非 uvicorn.run()：后者会自建事件循环并
        阻塞调用，无法与 IM 收发循环共存于同一进程（ledger objection #1）。
        """
        import uvicorn

        config = uvicorn.Config(
            self.dashboard_app(),
            host=host,
            port=port,
            log_config=None,  # 交给 structlog 统一接管，避免双通道日志
            access_log=True,
        )
        server = uvicorn.Server(config)
        await server.serve()

    async def run(self, dashboard_host: str | None = None,
                  dashboard_port: int = 8080) -> None:
        """并发运行 IM 适配器与 Dashboard HTTP 服务，直到 Ctrl+C / stop。"""
        # 启动后台自动关闭协程 (每 60s 扫描 idle tickets)
        auto_close_task = asyncio.create_task(
            _auto_close_idle_tickets(self.tickets, interval=60.0),
            name="auto_close",
        )
        try:
            # 收集所有 adapter.run() 协程
            adapter_runs = []
            for adapter in self.adapters:
                run_method = getattr(adapter, "run", None)
                if run_method is not None and callable(run_method):
                    adapter_runs.append(run_method())

            if dashboard_host is None:
                if adapter_runs:
                    await asyncio.gather(*adapter_runs)
                else:
                    await asyncio.Event().wait()
                return

            log.info("dashboard_serving", host=dashboard_host, port=dashboard_port)
            await asyncio.gather(*adapter_runs, self.serve_dashboard(dashboard_host, dashboard_port))
        finally:
            auto_close_task.cancel()
            aclose = getattr(self.agent, "aclose", None)
            if callable(aclose):
                await aclose()


def build_default_engine(
    ticket_store: Any = None,
    escalation_queue: Any = None,
    memory_pipeline: Any = None,
    knowledge_service: Any = None,
    audit_log: Any = None,
    summarizer: Any = None,
    pii_guard: Any = None,
) -> Engine:
    """无网络装配引擎（可测试、可复用）。

    自动按 settings.*.enabled 装配一个或多个 IM 适配器：
      - NapCatAdapter (QQ) 始终装配（默认）
      - GewechatAdapter (微信) 按 settings.gewechat.enabled 装配

    参数:
      ticket_store / escalation_queue / memory_pipeline / knowledge_service:
        外部传入存储实例 (main() 传入 Persistent* 版本, testing 时 None)。
      audit_log / summarizer / pii_guard: 外部传入时覆盖默认实例,
        None 时由本函数默认构造。
    """
    settings = get_settings()

    # ── Phase 2/3: 默认构造 (不传时) ──
    if audit_log is None:
        from lingxi.flow.audit import AuditLog
        audit_log = AuditLog(db_path=str(settings.data_dir / "zhixi_audit.db"))
    if summarizer is None:
        from lingxi.memory.summarizer import SessionSummarizer
        summarizer = SessionSummarizer()
    if pii_guard is None:
        from lingxi.security.pii import PIIGuard
        pii_guard = PIIGuard()

    # 未传入存储时: 有 persistent 标记且 main() 异步创建后传入, 否则走内存
    if ticket_store is None:
        ticket_store = TicketStore()
    if escalation_queue is None:
        escalation_queue = EscalationQueue()
    if memory_pipeline is None:
        # 传入实体图谱：编码出的记忆会抽出实体并建图，
        # 否则 MemoryItem.entities 恒为空、graph_store 整块无人使用。
        # 生产用 SQLite（重启不丢）；不持久化时用内存实现。
        if settings.memory.persistent:
            graph_store: Any = SQLiteGraphStore(
                str(settings.data_dir / settings.memory.graph_db_path),
            )
        else:
            graph_store = InMemoryGraphStore()
        memory_pipeline = MemoryPipeline(graph=graph_store)

    memory = memory_pipeline
    tickets = ticket_store
    queue = escalation_queue
    knowledge = knowledge_service or _build_knowledge_service()
    injector = PersonaInjector(PersonaConfig())

    # 构建默认世界观知识库（客服场景常用规则）
    lorebook = _build_default_lorebook()

    # 从 YAML 文件加载补充的 lorebook 条目（如配置了 lorebook_paths）
    yaml_lorebook = _load_yaml_lorebooks()
    if yaml_lorebook.size > 0:
        for entry in yaml_lorebook._entries:
            lorebook.add_entry(entry)
        log.info("lorebook_yaml_loaded", entries=yaml_lorebook.size)

    # ── 按配置装配多通道适配器 ──
    adapters: list[Any] = [NapCatAdapter(config=settings.onebot)]
    im_channels: list[str] = ["qq"]

    gc = settings.gewechat
    if gc.enabled and gc.api_base and gc.app_id:
        gw_adapter = GewechatAdapter(
            api_base=gc.api_base,
            token=gc.token,
            app_id=gc.app_id,
            callback_path=gc.callback_path,
            health_check_interval=30.0,
        )
        adapters.append(gw_adapter)
        im_channels.append("wechat")
        log.info("gewechat_adapter_added", api_base=gc.api_base)

    registry = _create_default_registry(
        blocked_commands=settings.security.blocked_commands,
    )
    # 权限基线来自配置（SecurityConfig.default_permission_level），
    # 每条消息的实际等级由 resolve_permission_level 按 user/chat 解析。
    permission_level = cast(
        "PermissionLevel", settings.security.default_permission_level
    )
    agent = get_agent(
        registry=registry,
        permission_level=permission_level,
        max_steps=settings.agent.react_steps,
    )

    # MCP 服务端：把同一份 ToolRegistry 暴露为 MCP / OpenAI 兼容协议，
    # 供外部客户端（Cursor / Claude Desktop 等）复用引擎能力。
    # resources/prompts 两个原语接的是真实数据源：知识库、人格配置、实体图谱。
    from lingxi.mcp.primitives import build_default_registry as build_mcp_registry
    from lingxi.mcp.primitives import set_resource_registry
    from lingxi.mcp.server import init_mcp_server

    init_mcp_server(registry, min_permission=permission_level)
    set_resource_registry(build_mcp_registry(
        knowledge=knowledge,
        persona=injector._persona,
        graph=getattr(memory, "graph", None),
    ))

    async def _send(msg: OutboundMessage) -> None:
        """多通道智能路由：根据 msg.channel 选适配器发送。"""
        for _adapter in adapters:
            # 根据目标渠道选择适配器
            channel = getattr(msg, "channel", None)
            if channel is None:
                # 无 channel 字段 → 走第一个可用适配器
                ok = await _adapter.send(msg)
                if ok:
                    return
                continue
            # NapCat 处理 QQ 消息
            if channel.value.startswith("qq") and isinstance(_adapter, NapCatAdapter):
                ok = await _adapter.send(msg)
                if not ok:
                    log.warning("outbound_send_failed", target=msg.target_id, channel="qq")
                return
            # Gewechat 处理微信消息
            if channel.value.startswith("wechat") and isinstance(_adapter, GewechatAdapter):
                ok = await _adapter.send(msg)
                if not ok:
                    log.warning("outbound_send_failed", target=msg.target_id, channel="wechat")
                return
        log.warning("no_adapter_for_channel", channel=getattr(msg, "channel", None))

    from lingxi.flow.rules import RuleEngine
    from lingxi.flow.sla import SLADefinition, SLAMonitor

    sla_monitor = SLAMonitor(SLADefinition())
    rule_engine = RuleEngine()

    pipeline = CustomerServicePipeline(
        agent=agent,
        ticket_store=tickets,
        escalation_queue=queue,
        confidence_threshold=settings.agent.confidence_threshold,
        memory_pipeline=memory,
        send_message_func=_send,
        lorebook=lorebook,
        knowledge=knowledge,
        rule_engine=rule_engine,
        pii_guard=pii_guard,
        audit_log=audit_log,
    )

    async def _on_event(event: IMEvent) -> None:
        """IM 事件 → persona system prompt + 记忆召回 + 世界观 → 客服管线。"""
        memory_block = ""
        lorebook_block = ""
        keyword_escalate_reason = ""
        if event.text:
            # 混合召回：字面 n-gram + 实体图谱（共享实体但措辞不同的记忆也能命中）
            memory_block = memory.recall_hybrid(
                user_id=event.user_id, query=event.text,
            ).render_for_prompt()
            if lorebook is not None:
                lorebook_block = lorebook.query(user_message=event.text) if lorebook is not None else ""
            # 关键词升级检测 (KeywordRule action=escalate)
            kw_result = injector._evaluate_keywords(event.text, injector._persona.keyword_rules)
            if kw_result and kw_result[0] == "escalate":
                keyword_escalate_reason = kw_result[1]
        # 记忆与世界观必须由 injector 拼进 system prompt：reason 节点不读 state 中的块，
        # 只写进 state 等于凭空消失（见 docs/AUDIT-2026-10-10.md #17）
        system_prompt = injector.build_system_prompt(
            memory_block=memory_block,
            lorebook_block=lorebook_block,
            user_message=event.text,
        )
        # 分级权限按 user/chat 解析（SecurityConfig.permission_rules），
        # 不再是写死的全局常量（见 docs/AUDIT-2026-10-10.md #4）
        level = resolve_permission_level(
            user_id=event.user_id,
            chat_id=event.chat_id,
            default_level=permission_level,
        )
        await pipeline.handle_event(
            event,
            system_prompt=system_prompt,
            memory_block=memory_block,
            keyword_escalate_reason=keyword_escalate_reason,
            send_func=_send,
            current_permission=level,
        )

    # 所有适配器共享同一个事件处理入口
    for _adapter in adapters:
        _adapter._on_event = _on_event

    # A/B 实验引擎：挂到 Dashboard 后 /api/experiments* 才存在（此前从未构造 → 404）
    from lingxi.experiment import ExperimentEngine

    experiment_engine = ExperimentEngine()

    # 坐席管理：注册/状态/分配/利用率，供 dashboard 的 /api/seats* 使用
    from lingxi.seat.assignment import TicketAssigner
    from lingxi.seat.models import SeatStore

    seat_store = SeatStore()
    assigner = TicketAssigner(seat_store)

    log.info("engine_built", channels=im_channels, adapters=len(adapters),
             persistent=hasattr(tickets, '_writer'),
             permission_level=permission_level)
    return Engine(
        agent=agent,
        pipeline=pipeline,
        adapters=adapters,
        injector=injector,
        tickets=tickets,
        queue=queue,
        memory=memory,
        lorebook=lorebook,
        knowledge=knowledge,
        _sla_monitor=sla_monitor,
        _rule_engine=rule_engine,
        audit_log=audit_log,
        summarizer=summarizer,
        pii_guard=pii_guard,
        experiment_engine=experiment_engine,
        seat_store=seat_store,
        assigner=assigner,
        permission_level=permission_level,
    )


def _build_knowledge_service() -> Any:
    """异步构建 KnowledgeService (仅在需要时调用)。"""
    from lingxi.knowledge.service import KnowledgeService
    return KnowledgeService()


def _build_default_lorebook() -> Lorebook:
    """构建客服场景默认世界观知识库。"""
    entries = [
        LorebookEntry(
            entry_id="return_policy",
            keywords=["退货", "退款", "换货", "退钱", "不想要"],
            content=(
                "退货政策：商品签收后 7 天内可无理由退货，需保持商品完好、"
                "包装齐全。退款在仓库签收后 3 个工作日内原路返回。"
            ),
            priority=10,
        ),
        LorebookEntry(
            entry_id="shipping_time",
            keywords=["物流", "发货", "多久", "什么时候到", "快递"],
            content=(
                "发货时效：订单付款后 48 小时内发出，偏远地区（新疆、西藏）"
                "额外加 2-3 天。快递默认顺丰/京东，可备注改发。"
            ),
            priority=8,
        ),
        LorebookEntry(
            entry_id="contact_human",
            keywords=["投诉", "人工", "找客服", "转人工", "举报"],
            content=(
                "用户要求转人工时，确认用户问题类型并安抚情绪。"
                "投诉类工单自动升级至人工队列，处理时效 30 分钟内首次响应。"
            ),
            priority=9,
        ),
    ]
    return Lorebook(entries=entries)


async def _auto_close_idle_tickets(
    ticket_store: Any,
    interval: float = 60.0,
    idle_seconds: float = 1800.0,
) -> None:
    """后台协程：定期扫描 idle tickets 并自动关闭。

    复用 Ticket.can_auto_close() 判断逻辑。每 interval 秒扫描一次。
    被取消 (Engine.run 退出) 时优雅退出。
    """
    from lingxi.flow.ticket import TicketStatus

    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        idle_tickets = ticket_store.list_idle(idle_seconds)
        for ticket in idle_tickets:
            try:
                ticket.transition(TicketStatus.CLOSED)
                log.info("ticket_auto_closed", ticket_id=ticket.ticket_id, idle=idle_seconds)
            except ValueError:
                pass


def _load_yaml_lorebooks() -> Lorebook:
    """从 settings.memory.lorebook_paths 加载所有 YAML Lorebook 文件。

    每个文件独立加载后合并到同一 Lorebook 实例。
    文件不存在或解析失败时不阻塞启动（只记录 warning）。
    """
    from lingxi.memory.lorebook import Lorebook
    settings = get_settings()
    paths = settings.memory.lorebook_paths or []
    if not paths:
        return Lorebook()
    result = Lorebook()
    for p in paths:
        import os
        if not os.path.isfile(p):
            log.warning("lorebook_yaml_missing", path=p)
            continue
        try:
            lb = Lorebook.from_yaml(p)
            for entry in lb._entries:
                result.add_entry(entry)
            log.info("lorebook_yaml_loaded", path=p, entries=lb.size)
        except Exception as e:
            log.warning("lorebook_yaml_error", path=p, error=str(e))
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 CLI 参数（--dashboard-host / --dashboard-port / --no-dashboard / --persistent / --db-path）。"""
    parser = argparse.ArgumentParser(
        prog="lingxi",
        description="智犀 (ZhiXi) — 二十四小时在线的全栈数字员工引擎",
    )
    parser.add_argument(
        "--version", action="version", version=f"lingxi {_version()}",
    )
    parser.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="Dashboard 监听地址（默认仅本机，前端经 Next.js 代理访问）",
    )
    parser.add_argument(
        "--dashboard-port", type=int, default=8080, help="Dashboard 端口",
    )
    parser.add_argument(
        "--no-dashboard", action="store_true", help="不启动 Dashboard HTTP 服务",
    )
    parser.add_argument(
        "--persistent", action="store_true", default=None,
        help="启用 SQLite 持久化 (默认按 settings.memory.persistent)",
    )
    parser.add_argument(
        "--no-persistent", action="store_true",
        help="禁用持久化 (纯内存模式, 覆盖 settings)",
    )
    parser.add_argument(
        "--db-path", type=str, default=None,
        help="SQLite 数据库路径 (默认使用 settings.memory.db_path)",
    )
    return parser.parse_args(argv)


def _version() -> str:
    from lingxi import __version__

    return __version__


def main(argv: list[str] | None = None) -> None:
    """CLI 入口。"""
    # 最先打印横幅（configure_logging 之前，确保任何模式下都可见）
    print(_ZHIXI_BANNER)

    args = _parse_args(argv)
    settings = get_settings()
    configure_logging("DEBUG" if settings.debug else settings.log_level)

    # ── 持久化判断: CLI 参数 > settings 默认值 ──
    persistent = settings.memory.persistent
    if args.persistent is True:
        persistent = True
    if args.no_persistent:
        persistent = False

    db_path = args.db_path or str(settings.data_dir / settings.memory.db_path)

    if persistent:
        # 创建文件父目录
        from pathlib import Path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        log.info("persistent_storage_enabled", db_path=db_path)

    engine: Engine | None = None
    try:
        if persistent:
            engine = asyncio.run(_build_persistent_engine(db_path, args))
        else:
            engine = build_default_engine()
    except KeyboardInterrupt:
        log.info("zhixi_engine_stopped_by_user")
        return
    except Exception:
        log.exception("engine_build_failed")
        return

    dashboard_host = None if args.no_dashboard else args.dashboard_host
    log.info(
        "zhixi_engine_starting",
        ws_url=settings.onebot.ws_url,
        model=settings.llm.model,
        dashboard=(f"{dashboard_host}:{args.dashboard_port}"
                  if dashboard_host else "disabled"),
        persistent=persistent,
    )
    try:
        asyncio.run(
            engine.run(
                dashboard_host=dashboard_host,
                dashboard_port=args.dashboard_port,
            )
        )
    except KeyboardInterrupt:
        log.info("zhixi_engine_stopped_by_user")
    finally:
        log.info("zhixi_engine_exited")


async def _build_persistent_engine(
    db_path: str,
    args: argparse.Namespace,
) -> Engine:
    """异步创建持久化存储 + 构建引擎。"""
    from lingxi.flow.persistence import create_persistent_stores
    from lingxi.memory.graph_store import SQLiteGraphStore

    # 图谱随记忆一起持久化：否则持久化模式下实体图谱整体失效
    # （编码不建边、recall_hybrid 图谱分支恒空、MCP graph://stats 不注册）
    settings = get_settings()
    graph = SQLiteGraphStore(
        str(settings.data_dir / settings.memory.graph_db_path),
    )
    tickets, queue, memory = await create_persistent_stores(db_path, graph=graph)
    log.info("persistent_stores_created",
             tickets=0,
             queue=queue.pending_count if hasattr(queue, 'pending_count') else 0,
             memories=memory.total_memories if hasattr(memory, 'total_memories') else 0)
    return build_default_engine(
        ticket_store=tickets,
        escalation_queue=queue,
        memory_pipeline=memory,
    )


if __name__ == "__main__":
    main()
