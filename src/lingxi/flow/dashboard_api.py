"""Node 5.4 — Dashboard API: FastAPI 后端接口，供 Next.js 控制台消费。

🔗 工程逻辑
提供给监控面板的 RESTful API：
- /api/health — 健康检查
- /api/dashboard/summary — 实时概览（活跃工单数、队列长度、解决率）
- /api/tickets — 活跃工单列表
- /api/tickets/{id} — 单个工单详情（含消息历史）
- /api/queue — 升级队列状态
- /api/queue/claim — 坐席认领工单
- /api/queue/complete — 坐席完成工单
- /api/sessions — 当前活跃会话列表
- /api/logs — 最近结构化日志（内存环）
- /api/events — SSE 实时事件流 (升级/认领/关闭)
- /api/sessions/{chat_id}/stream — SSE: 流式推送给定会话的 Agent 处理结果
- /api/stream/chat — SSE: 发送消息并实时接收 Agent 回复流

🤖 AI 避坑
- API 必须有认证（admin_token），绝不能暴露到公网无需鉴权
- 不要在 API 里做耗时操作，IO 请求用 async
- 坐席操作必须是原子的（避免多人同时认领同一工单）
- SSE 使用 sse_starlette（battle-tested, 纯 asyncio 无额外依赖）
- Agent streaming 端点返回 EventSourceResponse 时必须由 Starlette 原生处理，
  不能用 fastapi.responses.Response 包装（会破坏 SSE 长连接 chunked 传输）
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from sse_starlette.sse import EventSourceResponse

from lingxi.config import get_settings
from lingxi.core import get_logger
from lingxi.core.admin_auth import require_admin, require_scrape, set_local_only
from lingxi.core.logging import memory_handler
from lingxi.flow.sla import format_sla_status
from lingxi.flow.ticket import TicketStatus
from lingxi.persona.guardrails import detect_injection
from lingxi.persona.injector import PersonaInjector
from lingxi.persona.schema import PersonaConfig

log = get_logger(__name__)

# 只允许 loopback 来源的 origin 正则：host 必须是 localhost / 127.0.0.1 / [::1]，
# 端口任意（dev server 端口会变，写死端口等于给"换个端口启动"埋雷）。
# 不用 `allow_origins=["*"]`：那等于对全网开放管理面。
_LOOPBACK_ORIGIN_RE = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"




def _install_cors(app: FastAPI) -> None:
    """让浏览器里的前端直连后端。

    🔗 工程逻辑
        前端不再经过 Next 的 rewrites 代理 —— 代理会缓冲 SSE 响应体，
        /api/events 于是"200 但一帧不发"。直连 + CORS 才能让 dev 与
        Tauri 两条路径行为一致（SSE、错误语义、状态码都相同）。

    🤖 AI 避坑
        - 正则只放行 loopback 来源；Dashboard 默认也只监听 loopback，两侧对齐
        - allow_credentials=False：凭据走 x-admin-token 头而非 Cookie，
          所以不需要 credentials 模式（那会强制禁止 `*`，且引入 CSRF 面）
        - SSE 与普通 GET 共用一套 CORS，不要为 /api/events 单独开一套
    """
    from fastapi.middleware.cors import CORSMiddleware

    settings = get_settings()
    origins = list(settings.security.cors_origins)
    if not origins and settings.security.cors_allow_loopback:
        # 显式 origin 留空时启用 loopback 正则；两者都给，兼容性更好
        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=_LOOPBACK_ORIGIN_RE,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        return
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
    else:
        log.info("cors_disabled")


def create_dashboard_app(
    ticket_store: Any,  # TicketStore | PersistentTicketStore
    queue: Any,  # EscalationQueue | PersistentEscalationQueue
    knowledge_service: Any = None,  # KnowledgeService | None
    sla_monitor: Any = None,  # SLAMonitor | None
    rule_engine: Any = None,  # RuleEngine | None
    audit_log: Any = None,  # AuditLog | None
    summarizer: Any = None,  # SessionSummarizer | None
    agent: Any = None,  # ZhiXiAgent | None — 用于 SSE 流式推理端点
    auth_enabled: bool = False,  # 是否启用 JWT RBAC 鉴权
    experiment_engine: Any = None,  # ExperimentEngine | None
    seat_store: Any = None,  # SeatStore | None — 坐席管理端点
    assigner: Any = None,  # TicketAssigner | None
    persona_config: Any = None,  # PersonaConfig | None — /api/persona 用
    local_only: bool = False,  # 是否仅监听 loopback（决定未配 token 时能否免密）
) -> FastAPI:
    """创建 Dashboard FastAPI 应用。"""
    app = FastAPI(title="智犀 (ZhiXi) Dashboard v0.2.0", version="0.2.0")

    # 暴露面登记一次即可：MCP / seat / workflow 三个子路由与 Dashboard 共用
    # 同一套凭据策略，登记点也必须唯一（见 core/admin_auth.py）。
    set_local_only(local_only)

    _install_cors(app)

    # ── Prometheus 中间件 (自动记录所有 HTTP 请求) ──
    settings = get_settings()
    if settings.metrics.enabled:
        from lingxi.metrics.middleware import PrometheusMiddleware
        app.add_middleware(PrometheusMiddleware)

    # ── Prometheus /metrics 端点 ──
    if settings.metrics.enabled:
        from lingxi.metrics import get_metrics_collector
        metrics_path = settings.metrics.path

        @app.get(metrics_path)
        async def prometheus_metrics(x_admin_token: str | None = Header(None)) -> Response:
            """Prometheus 指标抓取端点。

            需凭据：metrics.scrape_token 优先，未配置则回退 security.admin_token。
            匿名可读会把内部端点名与状态码分布（含 404 探测痕迹）直接送出去
            （见 docs/AUDIT-2026-10-10.md #7）。抓取侧示例::

                scrape_configs:
                  - job_name: zhixi
                    metrics_path: /metrics
                    authorization:
                      type: Token
                      credentials: <scrape_token>
            """
            _check_scrape(x_admin_token)
            collector = get_metrics_collector()
            data = collector.generate()
            return Response(content=data, media_type="text/plain; version=0.0.4; charset=utf-8")

    # ── 实验管理路由 (A/B Testing) ──
    if experiment_engine is not None:
        from lingxi.experiment import create_experiment_router

        # prefix="/api" 不是可选的：router 自身只声明 "/experiments"，
        # 漏掉前缀时端点会挂在 "/experiments"（根路径），而前端请求的是
        # "/api/experiments" → 整个 A/B 实验页 404。
        # 单测自己 include 时带了前缀，所以一直没人发现（与
        # AUDIT-2026-10-10 记录的"组件没透传"同一类问题）。
        app.include_router(create_experiment_router(experiment_engine), prefix="/api")

    # ── 鉴权路由 (JWT RBAC) ──
    if auth_enabled:
        from lingxi.auth import create_auth_router
        app.include_router(create_auth_router())

    # ── 工作流 DAG 路由 (可视化 / 校验 / Mermaid 预览 / 执行) ──
    # 此前 create_workflow_router 从未被任何地方 include，等于 700+ 行死代码
    # components 传入引擎持有的真实组件，让 /api/workflow/execute 能真跑
    # 一遍 DAG；缺失的组件在 executor 里被优雅降级为 skipped。
    from lingxi.workflow.router import create_workflow_router

    app.include_router(create_workflow_router({
        "rule_engine": rule_engine,
        "ticket_store": ticket_store,
        "knowledge": knowledge_service,
        "agent": agent,
        "queue": queue,
        "assigner": assigner,
        "audit_log": audit_log,
    }))

    # ── 坐席管理路由 ──
    if seat_store is not None:
        from lingxi.seat.router import create_seat_router

        app.include_router(
            create_seat_router(seat_store, assigner=assigner, ticket_store=ticket_store),
            prefix="/api",
        )

    # ── MCP / OpenAI 兼容工具路由（内部需 admin token）──
    from lingxi.mcp.router import create_mcp_router, create_openai_tools_router

    app.include_router(create_mcp_router())
    app.include_router(create_openai_tools_router())

    def _check_admin(x_admin_token: str | None = Header(None)) -> None:
        require_admin(
            x_admin_token,
            detail=(
                "Dashboard 未配置管理凭据：请设置 "
                "LINGXI_SECURITY__ADMIN_TOKEN 环境变量"
            ),
        )

    def _check_scrape(x_admin_token: str | None) -> None:
        require_scrape(x_admin_token)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "zhixi-dashboard"}

    @app.get("/api/dashboard/summary")
    async def summary(x_admin_token: str | None = Header(None)) -> dict[str, Any]:
        _check_admin(x_admin_token)
        active = ticket_store.list_active()
        escalated = [t for t in active if t.status.value == "escalated"]
        processing = [t for t in active if t.status.value == "processing"]
        waiting = [t for t in active if t.status.value == "waiting_user"]
        queue_snapshot = queue.snapshot()

        # SLA 统计
        sla_results = sla_monitor.check_all(active) if sla_monitor else []
        sla_summary = format_sla_status(sla_results) if sla_results else {
            "total": 0, "breached": 0, "compliant": 0, "breach_rate": 0.0
        }

        # 坐席统计
        seat_metrics = {}
        if seat_store:
            all_seats = seat_store.list_all()
            online_seats = [s for s in all_seats if s.status.value in ("idle", "busy")]
            busy_seats = [s for s in online_seats if s.status.value == "busy"]
            total_capacity = sum(s.max_concurrent for s in online_seats)
            used_capacity = sum(s.current_ticket_count for s in busy_seats)
            total_handled = sum(s.total_handled for s in all_seats)
            avg_response = sum(s.avg_response_seconds for s in all_seats) / max(1, len(all_seats))
            seat_metrics = {
                "total_seats": len(all_seats),
                "online_seats": len(online_seats),
                "busy_seats": len(busy_seats),
                "idle_seats": len([s for s in online_seats if s.status.value == "idle"]),
                "total_capacity": total_capacity,
                "used_capacity": used_capacity,
                "utilization_rate": used_capacity / max(1, total_capacity),
                "total_handled": total_handled,
                "avg_response_seconds": round(avg_response, 1),
            }

        # 解决率（本轮会话内）
        resolved = [t for t in active if t.status.value in ("resolved", "closed")]
        resolution_rate = len(resolved) / max(1, len(active))

        # 平均等待时长（升级队列）
        avg_wait = queue_snapshot.get("longest_wait", 0)
        pending = queue_snapshot.get("pending_count", 0)
        if pending > 0:
            top5 = queue_snapshot.get("top_5_pending", [])
            if top5:
                avg_wait = sum(t.get("wait_seconds", 0) for t in top5) / len(top5)

        return {
            "active_tickets": len(active),
            "escalated": len(escalated),
            "processing": len(processing),
            "waiting_user": len(waiting),
            "resolved": len(resolved),
            "resolution_rate": round(resolution_rate, 3),
            "queue": queue_snapshot,
            "sla": sla_summary,
            "seats": seat_metrics,
            "avg_wait_seconds": round(avg_wait, 1),
        }

    @app.get("/api/tickets")
    async def list_tickets(
        status: str | None = None,
        x_admin_token: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        _check_admin(x_admin_token)
        tickets = ticket_store.list_active()
        if status:
            tickets = [t for t in tickets if t.status.value == status]
        return [t.to_dict() for t in tickets]

    @app.get("/api/tickets/{ticket_id}")
    async def get_ticket(
        ticket_id: str,
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_admin(x_admin_token)
        ticket = ticket_store.get(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        d: dict[str, Any] = ticket.to_dict()
        d["messages"] = [
            {
                "role": m.role,
                "content": m.content[:500],
                "timestamp": m.timestamp,
                "confidence": m.confidence,
            }
            for m in ticket.messages
        ]
        return d

    @app.get("/api/queue")
    async def queue_status(
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_admin(x_admin_token)
        snapshot: dict[str, Any] = queue.snapshot()
        return snapshot

    @app.post("/api/queue/claim")
    async def claim_ticket(
        agent_id: str,
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_admin(x_admin_token)
        entry = await queue.dequeue(agent_id)
        if not entry:
            raise HTTPException(status_code=404, detail="No pending tickets")
        return {
            "ticket_id": entry.ticket_id,
            "chat_id": entry.chat_id,
            "category": entry.category.value,
            "reason": entry.reason,
        }

    @app.post("/api/queue/complete")
    async def complete_ticket(
        ticket_id: str,
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_admin(x_admin_token)
        ticket = ticket_store.get(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        queue.complete(ticket_id)
        # 同步更新 ticket 状态为 CLOSED
        with contextlib.suppress(ValueError):
            ticket.transition(TicketStatus.CLOSED)
        return {"ticket_id": ticket_id, "status": "completed"}

    # ── 入站 Webhook（HMAC-SHA256 验签）──
    # 出站签名用同一套 header（security/hmac.py 的 sign_webhook_payload），
    # 接收方验签用 verify_webhook_signature：时间戳容差 + 常量时间比较。
    # 未配置 inbound_hmac_secret 时端点整体不挂载（404），不产生半开面。
    _inbound_secret = (
        settings.webhook.inbound_hmac_secret.get_secret_value()
        if settings.webhook.inbound_hmac_secret
        else None
    )
    # SecretStr 实例本身恒为真值（repr 掩码串），空 secret 必须按未配置处理，
    # 否则会出现"配了空密钥的验签端点"——任何请求都能算出正确签名。
    if _inbound_secret:

        @app.post("/api/webhook/inbound")
        async def webhook_inbound(
            request: Request,
            x_zhixi_signature: str | None = Header(default=None, alias="X-ZhiXi-Signature"),
            x_zhixi_timestamp: str | None = Header(default=None, alias="X-ZhiXi-Timestamp"),
        ) -> dict[str, Any]:
            """验签入站 Webhook。payload 原样返回，由调用方（规则引擎/外部系统）消费。"""
            from lingxi.security.hmac import verify_webhook_signature

            body = await request.body()
            if x_zhixi_signature is None or x_zhixi_timestamp is None:
                raise HTTPException(status_code=401, detail="Missing X-ZhiXi-Signature / X-ZhiXi-Timestamp")
            ok = verify_webhook_signature(
                body,
                _inbound_secret,
                x_zhixi_signature,
                x_zhixi_timestamp,
                max_age_seconds=settings.webhook.inbound_max_age_seconds,
            )
            if not ok:
                raise HTTPException(status_code=401, detail="Signature verification failed")
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {"raw": body.decode("utf-8", errors="replace")}
            return {"received": True, "payload": payload}

        log.info("inbound_webhook_endpoint_mounted")

    # ── 模型接入状态（独立子模块：flow/setup_api.py）──
    from lingxi.flow.setup_api import register_setup_routes

    register_setup_routes(app, _check_admin)

    @app.get("/api/sessions")
    async def list_sessions(
        x_admin_token: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        """当前活跃会话（含最近消息概要）。"""
        _check_admin(x_admin_token)
        tickets = ticket_store.list_active()
        sessions: list[dict[str, Any]] = []
        for t in tickets:
            last_msg = t.messages[-1] if t.messages else None
            sessions.append(
                {
                    "session_id": t.ticket_id,
                    "chat_id": t.chat_id,
                    "user_id": t.user_id,
                    "channel": t.channel,
                    "status": t.status.value,
                    "message_count": t.message_count,
                    "last_activity": t.updated_at,
                    "last_message": (last_msg.content[:120] if last_msg else ""),
                    "last_role": (last_msg.role if last_msg else ""),
                }
            )
        # 最近活跃排前
        sessions.sort(key=lambda s: s["last_activity"], reverse=True)
        return sessions

    # ── Knowledge 知识库 RAG (Phase 1) ──

    if knowledge_service is not None:

        @app.get("/api/knowledge/stats")
        async def knowledge_stats(
            x_admin_token: str | None = Header(None),
        ) -> dict[str, Any]:
            _check_admin(x_admin_token)
            stats: dict[str, Any] = await knowledge_service.stats()
            return stats

        @app.get("/api/knowledge/docs")
        async def list_knowledge_docs(
            x_admin_token: str | None = Header(None),
        ) -> list[dict[str, Any]]:
            _check_admin(x_admin_token)
            docs: list[dict[str, Any]] = await knowledge_service.list_documents()
            return docs

        @app.post("/api/knowledge/upload")
        async def upload_knowledge(
            body: dict[str, Any],
            x_admin_token: str | None = Header(None),
        ) -> dict[str, Any]:
            _check_admin(x_admin_token)
            content = body.get("content", "")
            doc_id = body.get("doc_id", "")
            source = body.get("source", "")
            if not content:
                raise HTTPException(status_code=400, detail="content is required")
            result: dict[str, Any] = await knowledge_service.upload_document(
                content, doc_id=doc_id, source=source,
            )
            return result

        @app.post("/api/knowledge/recall")
        async def recall_knowledge(
            body: dict[str, Any],
            x_admin_token: str | None = Header(None),
        ) -> list[dict[str, Any]]:
            _check_admin(x_admin_token)
            query = body.get("query", "")
            top_k = body.get("top_k", 5)
            hits: list[dict[str, Any]] = await knowledge_service.recall(query, top_k=top_k)
            return hits

        @app.delete("/api/knowledge/docs/{doc_id}")
        async def delete_knowledge_doc(
            doc_id: str,
            x_admin_token: str | None = Header(None),
        ) -> dict[str, Any]:
            _check_admin(x_admin_token)
            count = await knowledge_service.delete_document(doc_id)
            return {"deleted_chunks": count, "doc_id": doc_id}

        @app.post("/api/knowledge/reset")
        async def reset_knowledge(
            x_admin_token: str | None = Header(None),
        ) -> dict[str, str]:
            _check_admin(x_admin_token)
            await knowledge_service.reset()
            return {"status": "ok"}

    # ── 人工审批（L2/L3 高风险操作）──
    # Agent 在 L2/L3 调用点 interrupt 挂起 → 坐席在这里决策 →
    # approve/reject 端点调用 agent.resume() 让图从挂起点继续。
    @app.get("/api/approvals")
    async def list_approvals(
        status: str = "pending",
        x_admin_token: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        """待审批 / 历史审批清单。"""
        _check_admin(x_admin_token)
        from lingxi.agent.approval import get_approval_store

        store = get_approval_store()
        items = store.list_pending() if status == "pending" else store.list_all()
        return [i.to_dict() for i in items]

    @app.post("/api/approvals/{approval_id}/{action}")
    async def decide_approval(
        approval_id: str,
        action: str,
        body: dict[str, Any],
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        """审批决策：action ∈ {approve, reject}。

        决策后会立刻 resume 挂起的图 —— 不恢复的话用户那边会一直等。
        """
        _check_admin(x_admin_token)
        if action not in ("approve", "reject"):
            raise HTTPException(status_code=400, detail="action 必须是 approve 或 reject")

        from lingxi.agent.approval import get_approval_store

        store = get_approval_store()
        item = store.decide(
            approval_id,
            approved=action == "approve",
            approver=str(body.get("approver", "")),
            note=str(body.get("note", "")),
        )
        if item is None:
            raise HTTPException(status_code=404, detail="审批项不存在或已决策")

        resumed: dict[str, Any] = {}
        if agent is not None and hasattr(agent, "resume"):
            resumed = await agent.resume(
                channel=item.channel,
                chat_id=item.chat_id,
                decision={"approved": action == "approve", "approver": item.approver},
            )
        store.purge()
        return {
            "approval_id": approval_id,
            "status": item.status,
            "approver": item.approver,
            "resumed": bool(resumed),
            "final_reply": resumed.get("final_reply", ""),
            "escalate_reason": resumed.get("escalate_reason", ""),
        }

    @app.get("/api/logs")
    async def list_logs(
        limit: int = 200,
        x_admin_token: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        """最近结构化日志（内存环，最近 N 条）。"""
        _check_admin(x_admin_token)
        return memory_handler.recent(min(limit, 1000))

    # ── 人格配置 (13 模块) ──
    # 此前 /persona 页只能在前端硬编码一份假数据（后端无端点），
    # 运维改 PersonaConfig 后控制台看不到，等于没有可观测性。
    @app.get("/api/persona")
    async def get_persona(
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        """返回运行时生效的人格配置（身份 / 行为 / 13 模块开关 / 关键词规则）。"""
        _check_admin(x_admin_token)
        cfg = persona_config or PersonaConfig()
        modules = cfg.modules.model_dump()
        return {
            "identity": cfg.identity.model_dump(),
            "behavior": cfg.behavior.model_dump(),
            "modules": modules,
            "active_modules": [k for k, v in modules.items() if v and k != "debug"],
            "keyword_rules": [r.model_dump() for r in cfg.keyword_rules],
            "custom_prompt_keys": sorted(cfg.custom_prompts),
        }

    @app.post("/api/persona/preview")
    async def preview_persona(
        body: dict[str, Any],
        x_admin_token: str | None = Header(None),
    ) -> dict[str, Any]:
        """注入预览：用给定消息实时拼出真正会送给 LLM 的 system prompt。

        body: {"message": "...", "memory_block": "...", "lorebook_block": "..."}
        """
        _check_admin(x_admin_token)
        message = str(body.get("message", "") or "")
        injector = PersonaInjector(persona_config or PersonaConfig())
        prompt = injector.build_system_prompt(
            memory_block=str(body.get("memory_block", "") or ""),
            lorebook_block=str(body.get("lorebook_block", "") or ""),
            user_message=message,
        )
        verdict, pattern = detect_injection(message)
        return {
            "system_prompt": prompt,
            "chars": len(prompt),
            "guardrail": {"verdict": verdict.value, "pattern": pattern},
        }

    @app.get("/api/events")
    async def event_stream(
        x_admin_token: str | None = Header(None),
    ) -> EventSourceResponse:
        """SSE 实时事件流: 升级/认领/关闭。

        前端消费（前端直连后端，不走 Next 代理——代理会缓冲 SSE 响应体）::

            const res = await fetch(`${API_BASE}/api/events`, {
                headers: {"x-admin-token": TOKEN}
            });
            // 自己解 SSE 帧，见 web/lib/api.ts::subscribeSSE

        sse_starlette 是 asyncio 原生 SSE 实现, 不依赖 Redis/broker,
        适用于单节点多订阅场景。往 EventSourceResponse 传入一个 async
        generator, 框架负责编码符合 W3C EventSource 规范的 SSE wire format。
        """
        _check_admin(x_admin_token)

        async def _stream() -> Any:
            q = queue.subscribe()
            try:
                # 先发送一条注释确认连接已建立 (代理/Nginx 会缓存, 注释会立刻刷出)
                yield {"event": "connected", "data": json.dumps({"ts": asyncio.get_event_loop().time()})}
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=30.0)
                        yield {"event": msg["event"], "data": json.dumps(msg["data"])}
                    except TimeoutError:
                        # 30s 无事件: 发心跳注释防 Nginx 代理断开
                        yield {"event": "ping", "data": ""}
            except asyncio.CancelledError:
                pass
            finally:
                queue.unsubscribe(q)

        return EventSourceResponse(_stream())

    # ── SLA 监控 (Phase 2) ──

    if sla_monitor is not None:

        @app.get("/api/sla/status")
        async def sla_status(
            x_admin_token: str | None = Header(None),
        ) -> dict[str, Any]:
            """SLA 状态：活跃工单超时检查。"""
            _check_admin(x_admin_token)
            active = ticket_store.list_active()
            results = sla_monitor.check_all(active)
            from lingxi.flow.sla import format_sla_status
            return {
                "summary": format_sla_status(results),
                "breaches": results,
                "sla_config": sla_monitor.sla.model_dump(),
            }

    # ── 自动化规则引擎 (Phase 2) ──

    if rule_engine is not None:

        @app.get("/api/rules")
        async def list_rules(
            x_admin_token: str | None = Header(None),
        ) -> list[dict[str, Any]]:
            """列出所有规则。"""
            _check_admin(x_admin_token)
            rules: list[dict[str, Any]] = rule_engine.list_rules()
            return rules

        @app.post("/api/rules")
        async def add_rule(
            body: dict[str, Any],
            x_admin_token: str | None = Header(None),
        ) -> dict[str, Any]:
            """添加新规则。"""
            _check_admin(x_admin_token)
            from lingxi.flow.rules import Rule
            rule = Rule(**body)
            rule_engine.add_rule(rule)
            return {"status": "ok", "rule": rule.model_dump()}

        @app.post("/api/rules/evaluate")
        async def evaluate_rules(
            body: dict[str, Any],
            x_admin_token: str | None = Header(None),
        ) -> dict[str, Any]:
            """评估事件，返回匹配动作（不执行）。"""
            _check_admin(x_admin_token)
            event = body.get("event", {})
            actions = rule_engine.evaluate(event)
            return {"matched_actions": [a.model_dump() for a in actions]}

        @app.delete("/api/rules/{name}")
        async def delete_rule(
            name: str,
            x_admin_token: str | None = Header(None),
        ) -> dict[str, Any]:
            """按名称删除规则。"""
            _check_admin(x_admin_token)
            removed = rule_engine.remove_rule(name)
            if not removed:
                raise HTTPException(status_code=404, detail=f"Rule '{name}' not found")
            return {"status": "deleted", "name": name}

    # ── 审计日志 (Phase 3) ──

    if audit_log is not None:

        @app.get("/api/audit")
        async def list_audit(
            action: str = "",
            actor: str = "",
            limit: int = 100,
            x_admin_token: str | None = Header(None),
        ) -> list[dict[str, Any]]:
            _check_admin(x_admin_token)
            entries: list[dict[str, Any]] = audit_log.query(
                action=action, actor=actor, limit=limit
            )
            return entries

        @app.get("/api/audit/export")
        async def export_audit(
            x_admin_token: str | None = Header(None),
        ) -> Response:
            """导出审计日志为 JSON 文件。"""
            _check_admin(x_admin_token)
            entries = audit_log.query(limit=100000)
            body = audit_log.export_json(entries)
            return Response(
                content=body,
                media_type="application/json",
                headers={
                    "Content-Disposition": "attachment; filename=audit_log.json",
                },
            )

    # ── 会话摘要 (Phase 2) ──

    if summarizer is not None:

        @app.post("/api/sessions/summarize")
        async def summarize_session(
            body: dict[str, Any],
            x_admin_token: str | None = Header(None),
        ) -> dict[str, Any]:
            """对一组消息生成摘要。body: {"messages": [...]}"""
            _check_admin(x_admin_token)
            messages = body.get("messages", [])
            if not isinstance(messages, list):
                raise HTTPException(status_code=400, detail="messages must be a list")
            if len(messages) < 2:
                raise HTTPException(
                    status_code=400,
                    detail="messages too few to summarize (need at least 2)",
                )
            summary = await summarizer.summarize(messages)
            return {"summary": summary}

    # ── Agent SSE 流式推理 ──

    if agent is not None and hasattr(agent, "astream"):
        from lingxi.streaming import stream_agent_events

        @app.post("/api/stream/chat", response_class=EventSourceResponse)
        async def stream_chat(
            request: Request,
            x_admin_token: str | None = Header(None),
        ) -> EventSourceResponse:
            """SSE: 发送消息并实时接收 Agent 回复流。

            前端消费（直连后端）::

                const res = await fetch(`${API_BASE}/api/stream/chat`, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "x-admin-token": TOKEN
                    },
                    body: JSON.stringify({
                        message: "我的订单到哪了",
                        chat_id: "user_123",
                        user_id: "user_123",
                        channel: "qq_private",
                        system_prompt: "你是客服助手..."
                    })
                });

            注意: 此端点返回 SSE 流 (text/event-stream)，不能使用 fetch().json()，
            应使用 EventSource 或 ReadableStream 逐行读取。
            """
            _check_admin(x_admin_token)
            data = await request.json()
            message = data.get("message", "").strip()
            if not message:
                raise HTTPException(status_code=400, detail="message is required")

            chat_id = data.get("chat_id", "anonymous")
            user_id = data.get("user_id", chat_id)
            channel = data.get("channel", "qq_private")
            system_prompt = data.get("system_prompt", "你是智犀 (ZhiXi) 客服助手。请回答用户问题。")

            log.info("stream_chat_request", chat_id=chat_id, message_len=len(message))

            return stream_agent_events(
                agent=agent,
                raw_message=message,
                chat_id=chat_id,
                user_id=user_id,
                channel=channel,
                system_prompt=system_prompt,
            )

        @app.get("/api/sessions/{chat_id}/stream")
        async def stream_session(
            chat_id: str,
            x_admin_token: str | None = Header(None),
            message: str = "",
            user_id: str = "",
            channel: str = "qq_private",
            system_prompt: str = "",
        ) -> EventSourceResponse:
            """SSE: 实时流式推送给定 chat_id 的 Agent 处理结果。

            与 POST /api/stream/chat 功能相同, 但以 GET 暴露 (方便 EventSource 直连)。

            Query params:
                message: 用户消息 (必填)
                user_id: 用户 ID (默认 = chat_id)
                channel: 渠道 (默认 qq_private)
                system_prompt: 系统提示 (默认使用内置模板)
            """
            _check_admin(x_admin_token)
            if not message:
                raise HTTPException(status_code=400, detail="message query param is required")

            effective_user_id = user_id or chat_id
            effective_prompt = system_prompt or "你是智犀 (ZhiXi) 客服助手。请回答用户问题。"

            log.info("stream_session_request", chat_id=chat_id, message_len=len(message))

            return stream_agent_events(
                agent=agent,
                raw_message=message,
                chat_id=chat_id,
                user_id=effective_user_id,
                channel=channel,
                system_prompt=effective_prompt,
            )

    return app


__all__ = ["create_dashboard_app"]
