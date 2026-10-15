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
import os
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

# 只监听这些地址时，服务在网络上不可达 → 未配置凭据时可按本地模式放行
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _is_loopback_host(host: str | None) -> bool:
    """监听地址是否只对本机可见。

    🔗 工程逻辑
        这决定 `security.allow_anonymous_local` 的适用范围：
        - True  → 未配 admin_token 时本机免密（默认部署，双击即用）
        - False → 未配 admin_token 即 503 fail-closed（0.0.0.0 等）

    🤖 AI 避坑
        不要用 `host.startswith("127.")` 糊弄：`127.0.0.1.evil.com` 不是本机，
        而 `127.0.0.2` 虽然也是 loopback 却不在此集合里 —— 白名单而非黑名单，
        未知取值一律按"已暴露"处理（fail-closed 一侧）。
    """
    if not host:
        return False
    return host.strip().lower() in _LOOPBACK_HOSTS


def _resolve_dashboard_port(host: str, preferred: int) -> int:
    """为首选端口冲突做动态规避：占用则向后顺延，全占则交给 OS 分配。

    🔗 工程逻辑
        Dashboard 的默认端口是 8080，本机经常被别的开发服务占用
        （实测：一个无关的 `python -m http.server 8080` 就能让整个
        Dashboard 起不来）。与其让用户自己去 `netstat` 找是谁占了端口，
        不如自动顺延：8080 → 8081 → … → 8080+19，仍失败则让 OS 分配。

    🤖 AI 避坑
        - 探测用**裸 bind**，不要设 SO_REUSEADDR：Windows 下该选项允许
          两个 socket 绑同一端口（端口劫持），会让探测彻底失真。
        - preferred=0 表示"随便给一个"，直接走 OS 分配。
        - 只探测 TCP（uvicorn 只监听 TCP）；不处理 IPv6 双栈细节，
          host 原样传给 bind，与 uvicorn 最终绑定的地址一致。
    """
    import socket

    def _try_bind(port: int) -> int | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
            actual: int = int(s.getsockname()[1])
            return actual
        except OSError:
            return None
        finally:
            s.close()

    if preferred == 0:
        got = _try_bind(0)
        return got if got is not None else 0

    for candidate in range(preferred, preferred + 20):
        got = _try_bind(candidate)
        if got is not None:
            return got
    # 20 个都占着：交给 OS 分配（几乎不可能失败）
    got = _try_bind(0)
    return got if got is not None else preferred


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

    def dashboard_app(self, host: str | None = None) -> Any:
        """构建与本引擎共享存储的 Dashboard FastAPI 应用。

        ⚠️ 此前这里只传了存储类，agent / experiment_engine / auth_enabled 全是
        None，而 dashboard_api.py 正是靠 `if xxx is not None` 决定是否注册路由 ——
        于是生产进程里 SSE 流式端点、A/B 实验端点、JWT 鉴权端点**一个都没挂上**
        （前端 6 处请求 /api/experiments 必然 404）。单元测试因为自己显式传参
        所以发现不了。现在把引擎持有的组件全部透传。

        Args:
            host: 实际监听地址。决定"未配置凭据时能否按本地模式免密"——
                只有 loopback 才放行，0.0.0.0 仍然 fail-closed。
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
            local_only=_is_loopback_host(host),
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
            self.dashboard_app(host),
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
        # 自动关单时释放坐席槽位（否则坐席 current_ticket_count 只增不减，
        # 满载后永远不再接单）。
        closer = getattr(self, "assigner", None)
        # 启动后台自动关闭协程 (每 60s 扫描 idle tickets)
        auto_close_task = asyncio.create_task(
            _auto_close_idle_tickets(self.tickets, interval=60.0, assigner=closer),
            name="auto_close",
        )
        # 业务 Gauge 刷新（活跃工单分布 / 队列深度）——
        # update_business_metrics 此前全仓零调用，Gauge 永远是 0。
        metrics_task = asyncio.create_task(
            _refresh_business_metrics(self.tickets, self.queue, interval=30.0),
            name="business_metrics",
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
            metrics_task.cancel()
            aclose = getattr(self.agent, "aclose", None)
            if callable(aclose):
                await aclose()
            # 关闭持久化后端（Postgres 连接池 / SQLite 句柄），三件套共享 writer 时幂等
            for obj in (self.tickets, self.queue, self.memory):
                close = getattr(obj, "close", None)
                if not callable(close):
                    continue
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.warning("store_close_failed", obj=type(obj).__name__)


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
        if settings.mem0.enabled:
            # Mem0 语义后端（自带向量 + 图谱），Ollama 不可达时构造即 fail-fast
            from lingxi.memory.mem0_backend import Mem0MemoryPipeline

            memory_pipeline = Mem0MemoryPipeline.create(
                settings, settings.data_dir
            )
        else:
            # 实体图谱：编码出的记忆会抽实体建图，否则 MemoryItem.entities 恒空、
            # graph_store 整块无人使用。按 settings 选 Neo4j / SQLite / 内存。
            from lingxi.flow.persistence_factory import create_graph_backend
            from lingxi.memory.graph_store import InMemoryGraphStore

            graph_store: Any = create_graph_backend(settings) or InMemoryGraphStore()
            memory_pipeline = MemoryPipeline(graph=graph_store)

    memory = memory_pipeline
    tickets = ticket_store
    queue = escalation_queue
    knowledge = knowledge_service or _build_knowledge_service()
    injector = PersonaInjector(_build_default_persona())

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

    # A/B 实验引擎：必须在 pipeline 之前构造 —— 分流要真正参与消息链路
    # （此前它只挂管理 API，实验能建、指标看得到，但对话不受影响 = 空转）。
    from lingxi.experiment import ExperimentEngine

    experiment_engine = ExperimentEngine()

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
        tool_registry=registry,
        experiment_engine=experiment_engine,
        persona_injector=injector,
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
        #
        # A/B 实验：命中的变体通过白名单覆盖 persona（措辞风格/回复长度上限等），
        # 不影响身份锚与护栏 —— 那些字段不在 injector 的 _OVERRIDABLE_FIELDS 里。
        active_injector = injector
        ab_overrides = pipeline.experiment_overrides_for(event.user_id)
        if ab_overrides:
            from lingxi.persona.injector import PersonaInjector, _apply_experiment_overrides

            active_injector = PersonaInjector(
                _apply_experiment_overrides(injector._persona, ab_overrides)
            )
        system_prompt = active_injector.build_system_prompt(
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
        result = await pipeline.handle_event(
            event,
            system_prompt=system_prompt,
            memory_block=memory_block,
            keyword_escalate_reason=keyword_escalate_reason,
            send_func=_send,
            current_permission=level,
        )
        # 升级即尝试自动分派坐席：TicketAssigner 此前只在 HTTP 端点/工作流
        # 节点里可达，消息链路的升级从不触发分派（席位永远闲着）。
        # 无空闲坐席时 try_assign 返回 (False, 原因)，只记日志不阻塞。
        if getattr(result, "escalated", False) and assigner is not None:
            try:
                _ticket = tickets.get(result.ticket_id)
                if _ticket is not None:
                    ok, info = assigner.try_assign(_ticket)
                    log.info(
                        "seat_auto_assign",
                        ticket_id=result.ticket_id,
                        assigned=ok,
                        seat_or_reason=info,
                    )
            except Exception:
                log.warning("seat_auto_assign_failed", ticket_id=result.ticket_id)

    # 所有适配器共享同一个事件处理入口
    for _adapter in adapters:
        _adapter._on_event = _on_event

    # 坐席管理：注册/状态/分配/利用率，供 dashboard 的 /api/seats* 使用。
    # 持久化模式接 SQLite（与 tickets/queue 同一个 DB 文件）：
    # 此前 SeatStore 是纯内存，进程重启后坐席与负载计数全部归零。
    from lingxi.seat.assignment import TicketAssigner
    from lingxi.seat.models import SeatStore

    if settings.memory.persistent:
        try:
            seat_store = SeatStore.with_sqlite(
                str(settings.data_dir / settings.memory.db_path),
            )
        except Exception as e:  # noqa: BLE001 — 坐席持久化失败不阻断引擎启动
            log.warning("seat_store_persistent_failed_fallback_memory", error=str(e))
            seat_store = SeatStore()
    else:
        seat_store = SeatStore()
    assigner = TicketAssigner(seat_store)

    log.info("engine_built", channels=im_channels, adapters=len(adapters),
             persistent=hasattr(tickets, '_writer') or hasattr(tickets, 'writer'),
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


def _build_default_persona() -> PersonaConfig:
    """构建更完善的默认人格配置：身份锚 + 行为约束 + Few-shot 工具使用示例。"""
    from lingxi.persona.schema import (
        BehaviorRules,
        ContextModule,
        Identity,
        KeywordRule,
    )

    return PersonaConfig(
        identity=Identity(
            name="智犀",
            role="全栈数字客服助理",
            backstory=(
                "你是智犀（ZhiXi），一个 7×24 小时在线的全栈数字员工。"
                "你擅长：订单查询、退换货政策解释、物流跟踪、账单核对、"
                "技术故障排查、账号问题处理。你的目标是在首轮对话中解决 80% 以上的问题，"
                "无法解决时毫不犹豫升级至人工。"
            ),
            speaking_style=(
                "简洁、专业、带一点温暖。"
                "避免客套话（'亲/亲爱的/很高兴为您服务'），直接切入问题。"
                "每轮最多 1 个追问；给选项时用 A/B/C 格式；关键信息加粗。"
            ),
            signature="—— 智犀",
            avatar_url=None,
        ),
        behavior=BehaviorRules(
            max_reply_chars=400,
            prefer_short=True,
            one_question_at_a_time=True,
            no_unsolicited_menu=True,
            language="zh",
            emoji_level="light",
        ),
        modules=ContextModule(
            time_awareness=True,
            turn_stage_awareness=True,
            keyword_triggers=True,
            emotion_vector=True,
            memory_injection=True,
            lorebook_injection=True,
            variance=True,
            fixed_signals=True,
            static_rules=True,
            translate=False,
            debug=False,
        ),
        custom_prompts={
            "reasoning_guide": (
                "[推理指引]\n"
                "遇到问题先思考：用户想解决什么？我已知什么信息？还缺什么？\n"
                "按步骤推理：\n"
                "1️⃣ 识别意图（查单/退货/投诉/改地址/技术支持/其他）\n"
                "2️⃣ 检查已知槽位（订单号/用户ID/商品名/时间/问题描述）\n"
                "3️⃣ 缺槽位 → 追问 1 个最关键的\n"
                "4️⃣ 槽位齐 → 调用工具（knowledge_search / send_message）\n"
                "5️⃣ 工具返回 → 组织自然语言回复，给结论或下一步\n\n"
                "工具选择规则：\n"
                "- 知识库查询（政策/规则/FAQ）→ knowledge_search\n"
                "- 主动发消息给用户（通知/确认/追问）→ send_message\n"
                "- 绝不编造订单号/物流单号/退款金额\n"
                "- 不确定时承认'我需要查一下'并调用工具\n\n"
                "置信度自评（在 JSON 的 confidence 字段给出 0.0-1.0）：\n"
                "- 0.9+：信息完整、工具返回明确、能给确定性答案\n"
                "- 0.6-0.8：信息基本够用、但有细节需用户确认\n"
                "- <0.6：关键槽位缺失、或涉及红线操作 → 升级人工"
            ),
            "tool_examples": (
                "[工具调用示例]\n\n"
                "用户：帮我查一下订单 20240115001 的物流\n"
                "→ {\"tool\": \"knowledge_search\", \"tool_args\": {\"query\": \"订单 20240115001 物流状态\"}, \"confidence\": 0.7}\n\n"
                "用户：我想退货，订单号 20240115002\n"
                "→ {\"tool\": \"knowledge_search\", \"tool_args\": {\"query\": \"退货政策 订单 20240115002\"}, \"confidence\": 0.7}\n\n"
                "用户：我的包裹显示签收了但我没收到\n"
                "→ {\"tool\": \"knowledge_search\", \"tool_args\": {\"query\": \"签收未收到 处理流程\"}, \"confidence\": 0.6}\n\n"
                "用户：确认退款了吗？\n"
                "→ {\"tool\": \"send_message\", \"tool_args\": {\"content\": \"您的退款已于今日发起，预计 1-3 个工作日原路返回，请留意账户变动。\"}, \"confidence\": 0.8}"
            ),
        },
        keyword_rules=[
            KeywordRule(
                keywords=["投诉", "人工", "找客服", "转人工", "举报", "不满意", "差评"],
                action="escalate",
                payload="用户明确要求人工介入或表达强烈不满",
                priority=100,
            ),
            KeywordRule(
                keywords=["退款没到账", "退款多久", "退款失败"],
                action="inject_prompt",
                payload="退款到账时效：原路返回 1-3 个工作日；超过 5 个工作日未到账请提供订单号和支付凭证，我帮您催查财务。",
                priority=50,
            ),
            KeywordRule(
                keywords=["改地址", "修改地址", "换地址", "地址写错"],
                action="inject_prompt",
                payload="订单发货前可改地址；已发货需联系快递拦截（可能产生费用）。请提供订单号和新地址，我帮您查询发货状态。",
                priority=50,
            ),
            KeywordRule(
                keywords=["发票", "开票", "电子发票", "纸质发票"],
                action="inject_prompt",
                payload="支持电子发票（默认）和纸质发票。下单时填写抬头/税号，发货后自动推送电子发票到微信/邮箱。补开发票请提供订单号和开票信息。",
                priority=30,
            ),
        ],
    )


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
        LorebookEntry(
            entry_id="refund_timeline",
            keywords=["退款到账", "退款多久", "退款原路返回"],
            content=(
                "退款到账：支付宝/微信支付 1-2 个工作日，银行卡 3-5 个工作日。"
                "超过 5 个工作日未到账请联系发卡行或提供支付凭证催查。"
            ),
            priority=7,
        ),
        LorebookEntry(
            entry_id="address_change",
            keywords=["改地址", "修改地址", "地址写错", "收货地址"],
            content=(
                "未发货：直接改地址。已发货：联系快递拦截改派（可能产生改派费）。"
                "已签收：无法改地址，只能发起退货重寄。"
            ),
            priority=6,
        ),
    ]
    return Lorebook(entries=entries)


async def _auto_close_idle_tickets(
    ticket_store: Any,
    interval: float = 60.0,
    idle_seconds: float = 1800.0,
    assigner: Any = None,
) -> None:
    """后台协程：定期扫描 idle tickets 并自动关闭。

    复用 Ticket.can_auto_close() 判断逻辑。每 interval 秒扫描一次。
    被取消 (Engine.run 退出) 时优雅退出。

    assigner 非空时，关单同时释放该工单占用的坐席槽位 —— 否则坐席
    current_ticket_count 只增不减，满载后永远不再接单
    （坐席分配此前只有"占用"没有"归还"的配对路径）。
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
                continue
            # 释放坐席容量（含应答时长结算），失败不影响关单结果
            if assigner is not None:
                try:
                    assigner.release(ticket)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "seat_release_on_auto_close_failed",
                        ticket_id=ticket.ticket_id,
                        error=str(e),
                    )


async def _refresh_business_metrics(
    ticket_store: Any,
    queue: Any,
    interval: float = 30.0,
) -> None:
    """后台协程：定期把工单状态分布与队列深度写进 Prometheus Gauge。

    🔗 工程逻辑
        `update_business_metrics` 此前只有定义、全仓零调用 —— 面板上
        "活跃工单 / 队列深度" 永远是 0，等于没有业务可观测性。
        与 auto_close 同一模式：低频扫描，被取消时优雅退出。

    🤖 AI 避坑
        指标刷新失败绝不能杀死协程（存储后端可能是 PG，偶发超时很正常），
        单次失败只记 warning，下一轮继续。
    """
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        try:
            from lingxi.metrics import get_metrics_collector

            collector = get_metrics_collector()
            active = ticket_store.list_active()
            by_status: dict[str, int] = {}
            for t in active:
                status = getattr(getattr(t, "status", None), "value", None)
                key = str(status or "unknown")
                by_status[key] = by_status.get(key, 0) + 1
            depth = queue.pending_count
            if callable(depth):
                depth = depth()
            collector.update_business_metrics(by_status, int(depth or 0))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 指标不允许打断引擎
            log.warning("business_metrics_refresh_failed", error=str(e))


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
        "--dashboard-port", type=int,
        default=int(os.environ.get("LINGXI_DASHBOARD_PORT", "8080") or "8080"),
        help="Dashboard 端口（被占用时自动向后顺延；0 = 交给 OS 分配）",
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

    # LLM 可观测性 + 缓存（litellm 官方 callback / Cache）：
    # 放在建引擎之前，保证第一条消息就带 token/成本指标。
    from lingxi.agent.llm_observability import setup_llm_observability

    setup_llm_observability(settings)

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

    # 端口冲突自动顺延（动态灵活：8080 被占 → 8081 → …）。
    dashboard_port = args.dashboard_port
    if dashboard_host:
        resolved = _resolve_dashboard_port(dashboard_host, dashboard_port)
        if resolved != dashboard_port:
            log.info(
                "dashboard_port_switched",
                preferred=dashboard_port,
                actual=resolved,
                reason="preferred port in use",
            )
            print(
                f"[zhixi] 端口 {dashboard_port} 被占用，Dashboard 改用 {resolved}",
                flush=True,
            )
        dashboard_port = resolved
        # 机器可读端口行：Tauri 壳解析它来发现真实端口（被占后自动换的值）。
        # 格式是跨进程契约，改了要同步 src-tauri/src/python_sidecar.rs。
        print(f"ZHIXI_DASHBOARD_PORT={dashboard_port}", flush=True)

    log.info(
        "zhixi_engine_starting",
        ws_url=settings.onebot.ws_url,
        model=settings.llm.model,
        dashboard=(f"{dashboard_host}:{dashboard_port}"
                  if dashboard_host else "disabled"),
        persistent=persistent,
    )
    try:
        asyncio.run(
            engine.run(
                dashboard_host=dashboard_host,
                dashboard_port=dashboard_port,
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
    from lingxi.flow.persistence_factory import (
        create_graph_backend,
        create_stores,
    )

    settings = get_settings()
    backend = settings.persistence.backend

    # 图谱：PostgreSQL 且未启用 Neo4j 时记忆管线不挂图谱（graph=None）；
    # 其余情况交给工厂（neo4j.enabled → Neo4jGraphStore，持久化 → SQLiteGraphStore）
    if backend == "postgresql" and not settings.neo4j.enabled:
        graph = None
    else:
        graph = create_graph_backend(settings)

    tickets, queue, mem_store = await create_stores(graph=graph)

    # Mem0 记忆后端独立于 tickets/queue 持久化后端，任何 backend 下都可启用
    if settings.mem0.enabled:
        from lingxi.memory.mem0_backend import Mem0MemoryPipeline

        memory = Mem0MemoryPipeline.create(settings, settings.data_dir)
    else:
        memory = mem_store

    pending = (
        queue.pending_count() if callable(getattr(queue, "pending_count", None))
        else (queue.pending_count if hasattr(queue, "pending_count") else 0)
    )
    log.info("persistent_stores_created",
             backend=backend,
             mem0=settings.mem0.enabled,
             queue=pending,
             memories=memory.total_memories if hasattr(memory, 'total_memories') else 0)
    return build_default_engine(
        ticket_store=tickets,
        escalation_queue=queue,
        memory_pipeline=memory,
    )


if __name__ == "__main__":
    main()
