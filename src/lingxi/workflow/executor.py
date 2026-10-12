"""DAG 工作流执行引擎 —— 让 `workflow/` 从"能画图"变成"能跑"。

🔗 工程逻辑
    `workflow/` 此前只有模型、校验、Mermaid 渲染、YAML 往返 —— 整块是
    **纸面工作流**：能看能改，但没有一行代码真正按这张图走过。
    真实执行发生在 `pipeline.handle_event` 里，两者没有任何关联。

    这里把两者接起来：Executor 按拓扑 + 条件边驱动一个 DAG，
    每个 NodeType 由注册进来的 handler 实现，handler 调的正是
    pipeline 用的那批真实组件（RuleEngine / TicketStore / KnowledgeService /
    ZhiXiAgent / EscalationQueue / AuditLog / SeatAssigner）。

    为什么要有这层而不是"图只作可视化说明"：
    - 条件边（置信度 < 0.6 → 升级）本来就是**决策**，画在图上就该被执行
    - 改了 YAML 就能改行为，不需要改 Python；否则可视化是装饰

🤖 AI 避坑
    - **分支必须有兜底语义**：条件边一条都没命中且没有无条件边时，
      工作流必须**停下并记录原因**，而不是继续往下跑或静默结束。
    - 组件缺失要**优雅降级**：没注入 KnowledgeService 就把
      knowledge.hits 留空并在 trace 里标注 skipped，不能抛异常炸掉整条 DAG。
    - 必须有步数上限。validate_dag 保证无环，但 executor 是对外接口，
      不能假设调用方一定先校验过。
    - trace 要记录**每条边为何被选中/丢弃**，否则图出问题无从查起。
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from lingxi.core import get_logger
from lingxi.workflow.models import DAGEdge, EdgeCondition, NodeType, WorkflowDAG
from lingxi.workflow.validator import validate_dag

log = get_logger(__name__)

# 单次执行的步数上限。validate_dag 已保证无环，这里是给外部调用方的兜底。
MAX_EXECUTION_STEPS = 64


@dataclass
class WorkflowContext:
    """节点之间传递的可变状态。

    字段命名与 EdgeCondition.field 一一对应（`rule.match` / `keyword.matched`
    / `confidence` / `ticket.status`），新增条件字段时同步加属性。
    """

    raw_message: str = ""
    user_id: str = ""
    chat_id: str = ""
    channel: str = ""
    system_prompt: str = ""
    memory_block: str = ""

    ticket_id: str = ""
    ticket_status: str = ""
    knowledge_hits: list[dict[str, Any]] = field(default_factory=list)
    rule_match: str = "pass"
    keyword_matched: bool = False
    keyword_reason: str = ""
    llm_output: str = ""
    llm_escalate_reason: str = ""
    confidence: float = 0.0
    seat_id: str = ""
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    reply_text: str = ""

    def snapshot(self) -> dict[str, Any]:
        """导出可序列化的状态（供 trace / 调试用）。"""
        return {
            "ticket_id": self.ticket_id,
            "ticket_status": self.ticket_status,
            "rule_match": self.rule_match,
            "keyword_matched": self.keyword_matched,
            "confidence": self.confidence,
            "seat_id": self.seat_id,
            "knowledge_hits": len(self.knowledge_hits),
            "audit_events": list(self.audit_events),
            "reply_text": self.reply_text,
            "llm_output": self.llm_output,
        }


@dataclass
class NodeTrace:
    """单个节点的执行记录。"""

    node_id: str
    node_type: str
    status: str  # executed / skipped / failed / blocked
    detail: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class ExecutionResult:
    """一次 DAG 执行的完整结果。"""

    dag_name: str
    context: WorkflowContext
    traces: list[NodeTrace] = field(default_factory=list)
    edge_decisions: list[dict[str, Any]] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # 只有**异常中止**才非空。跑到终点节点是正常完成，不算 stopped。
    stopped_reason: str = ""
    ended_at: str = ""
    duration_ms: float = 0.0

    @property
    def executed(self) -> list[NodeTrace]:
        return [t for t in self.traces if t.status == "executed"]

    @property
    def succeeded(self) -> bool:
        return not any(t.status == "failed" for t in self.traces)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dag_name": self.dag_name,
            "succeeded": self.succeeded,
            "visited": self.visited,
            "skipped": self.skipped,
            "stopped_reason": self.stopped_reason,
            "ended_at": self.ended_at,
            "duration_ms": round(self.duration_ms, 2),
            "traces": [t.to_dict() for t in self.traces],
            "edge_decisions": self.edge_decisions,
            "context": self.context.snapshot(),
        }


@dataclass(frozen=True)
class Skip:
    """处理器主动声明「本次没跑」。

    🔗 为什么不用「detail 字符串以 'skipped:' 开头」来表示降级
        那样 status 仍是 executed，调用方必须去 match 字符串才能知道
        节点没真跑 —— 迟早有人漏掉，于是"图看着跑通了、其实全被跳过"
        这类问题就藏住了。降级必须体现在**结构化的 status** 上。
    """

    reason: str

    def __str__(self) -> str:
        return f"skipped: {self.reason}"


def skipped(reason: str) -> Skip:
    return Skip(reason)


NodeHandler = Callable[[WorkflowContext, Any], Awaitable[str | Skip]]


# ── 条件求值 ────────────────────────────────────────────────────────

def _lookup(context: WorkflowContext, path: str) -> tuple[bool, Any]:
    """按 `a.b` 路径取值。返回 (是否存在, 值)。

    ⚠️ 命名空间映射：DAG 里的条件字段写作 `rule.match` / `ticket.status`
    （点号表命名空间），而 ctx 上是扁平属性 `rule_match`。所以先按字面
    点号链取，取不到再退到 `path.replace(".", "_")`。
    不做这层映射的话，默认 DAG 的 `rule.match` 边**永远不成立** ——
    工作流会在第一个分叉点直接停死。

    "存在" 与 "值为 None" 区分开：`confidence` 为 0.0 是合法的，
    但 `rule.match` 缺失不是 —— `operator="exists"` 依赖这个区别。
    """
    obj: Any = context
    for part in path.split("."):
        if not hasattr(obj, part):
            break
        obj = getattr(obj, part)
    else:
        return True, obj

    flat = path.replace(".", "_")
    if hasattr(context, flat):
        return True, getattr(context, flat)
    return False, None


def evaluate_condition(
    condition: EdgeCondition | None,
    context: WorkflowContext,
) -> tuple[bool, str]:
    """求值一条边的条件，返回 (是否通过, 人类可读理由)。"""
    if condition is None or condition.is_empty():
        return True, "无条件边"

    present, actual = _lookup(context, condition.field)
    expected = condition.value
    op = (condition.operator or "equals").lower()

    if op == "exists":
        ok = present
        return ok, f"{condition.field} {'存在' if ok else '不存在'}"

    if not present:
        # 字段缺失时除了 not_equals / exists，其余一律不通过 ——
        # 宽松地"当作默认值通过"会让缺字段的分支被误激活。
        return False, f"{condition.field} 不存在（条件 {op} {expected!r}）"

    if op == "equals":
        ok = actual == expected
    elif op in ("not_equals", "ne"):
        ok = actual != expected
    elif op in ("lt", "lte", "gt", "gte"):
        # 布尔是 int 的子类：isinstance(True, int) 为真，不显式排除的话
        # `confidence gte 0` 会因为 confidence 是 True 而通过。
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False, f"{condition.field}={actual!r} 非数值，无法比较 {op}"
        ok = {
            "lt": actual < expected,
            "lte": actual <= expected,
            "gt": actual > expected,
            "gte": actual >= expected,
        }[op]
    elif op == "contains":
        ok = expected in actual if isinstance(actual, (str, list, tuple, dict)) else False
    else:
        # 未知算子必须不通过：拼错算子名不该让某条分支被激活。
        return False, f"未知算子 '{condition.operator}'"

    return ok, f"{condition.field}={actual!r} {op} {expected!r} → {'通过' if ok else '不通过'}"


# ── 执行器 ──────────────────────────────────────────────────────────

class WorkflowExecutor:
    """按 DAG 定义驱动节点执行。

    handler 按 NodeType 注册：未注册的节点类型不会崩，而是记为 skipped
    并在 stopped_reason 里说明 —— 这样新增节点类型是向后兼容的。
    """

    def __init__(self, components: dict[str, Any] | None = None) -> None:
        # components 里的每一项都是 pipeline 用的真实对象，键名见 docs
        self._c = components or {}
        self._handlers: dict[NodeType, NodeHandler] = {}
        self._register_default_handlers()

    @property
    def components(self) -> dict[str, Any]:
        return dict(self._c)

    def register(self, node_type: NodeType, handler: NodeHandler) -> None:
        """注册/覆盖某个节点类型的处理器。"""
        self._handlers[node_type] = handler

    async def run(
        self,
        dag: WorkflowDAG,
        context: WorkflowContext | None = None,
        *,
        validate: bool = True,
    ) -> ExecutionResult:
        started = time.perf_counter()
        ctx = context or WorkflowContext()

        errors = validate_dag(dag) if validate else []
        if errors:
            return ExecutionResult(
                dag_name=dag.name,
                context=ctx,
                stopped_reason=f"DAG 校验失败: {errors}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        result = ExecutionResult(dag_name=dag.name, context=ctx)
        pending: list[str] = dag.entry_nodes()
        if not pending:
            result.stopped_reason = "DAG 没有入口节点（全部节点都有入边）"
            return result

        node_by_id = {n.id: n for n in dag.nodes}
        steps = 0

        while pending:
            if steps >= MAX_EXECUTION_STEPS:
                result.stopped_reason = f"超出步数上限 {MAX_EXECUTION_STEPS}，已中止"
                break
            steps += 1

            node_id = pending.pop(0)
            if node_id in result.visited:
                continue  # DAG 已校验无环，这里是防御性去重
            node = node_by_id.get(node_id)
            if node is None:
                result.skipped.append(node_id)
                continue

            result.visited.append(node_id)
            handler = self._handlers.get(node.type)
            if handler is None:
                result.traces.append(NodeTrace(
                    node_id=node.id, node_type=str(node.type),
                    status="skipped", detail=f"未注册处理器: {node.type}",
                ))
                result.skipped.append(node_id)
                # 未处理的节点不产生边决策 —— 它「没有跑过」，不该决定走向
                continue

            node_started = time.perf_counter()
            try:
                outcome = await handler(ctx, node)
                if isinstance(outcome, Skip):
                    detail = outcome.reason
                    status = "skipped"
                else:
                    detail = outcome
                    status = "executed"
            except Exception as e:  # noqa: BLE001 — 单节点失败不应炸掉整条 DAG
                log.exception("workflow_node_failed", dag=dag.name, node=node.id)
                detail = f"{type(e).__name__}: {e}"
                status = "failed"
            result.traces.append(NodeTrace(
                node_id=node.id, node_type=str(node.type), status=status,
                detail=detail,
                duration_ms=(time.perf_counter() - node_started) * 1000,
            ))
            if status == "failed":
                result.stopped_reason = f"节点 '{node.id}' 执行失败: {detail}"
                break

            # ── 选边 ──
            outs = [e for e in dag.edges if e.source == node.id]
            conditional = [e for e in outs if e.is_conditional]
            unconditional = [e for e in outs if not e.is_conditional]

            chosen: list[DAGEdge] = list(unconditional)
            if conditional:
                matched = []
                for edge in conditional:
                    ok, why = evaluate_condition(edge.condition, ctx)
                    result.edge_decisions.append({
                        "from": edge.source, "to": edge.target,
                        "condition": edge.label, "taken": ok, "reason": why,
                    })
                    if ok:
                        matched.append(edge)
                chosen.extend(matched)

            if conditional and not matched and not unconditional:
                # 一条都没命中：停下并说明，而不是随便挑一条走下去
                result.stopped_reason = (
                    f"节点 '{node.id}' 的 "
                    f"{len(conditional)} 条条件边均未命中，工作流停止"
                )
                break

            if not chosen:
                # 正常终点：不算异常中止，只记 ended_at
                result.ended_at = node.id
                break

            pending.extend(e.target for e in chosen)

        for node in dag.nodes:
            if node.id not in result.visited:
                result.skipped.append(node.id)

        result.duration_ms = (time.perf_counter() - started) * 1000
        return result

    # ── 默认 handlers ──

    def _register_default_handlers(self) -> None:
        self._handlers.update({
            NodeType.REPLY: self._h_reply,
            NodeType.RULE_ENGINE: self._h_rule_engine,
            NodeType.TOOL_EXEC: self._h_tool_exec,
            NodeType.KNOWLEDGE_RECALL: self._h_knowledge,
            NodeType.LLM_INFERENCE: self._h_llm,
            NodeType.SEAT_ASSIGN: self._h_seat,
            NodeType.AUDIT_LOG: self._h_audit,
        })

    async def _h_reply(self, ctx: WorkflowContext, node: Any) -> str | Skip:
        """把 LLM 输出作为回复落到 ctx.reply_text。"""
        text = ctx.llm_output or "(无内容)"
        if node.params.get("pii_sanitize_output", True):
            text = _sanitize_output(text)
        ctx.reply_text = text
        return f"回复 {len(text)} 字"

    async def _h_rule_engine(self, ctx: WorkflowContext, node: Any) -> str | Skip:
        """规则引擎节点。

        `params.action_on_match` 决定命中后 rule_match 设成什么；
        缺省规则引擎（如"置信度判断"）只做取值判断，不改 rule_match。

        这是「一个 NodeType 承担两种语义」的地方 —— 默认 DAG 里
        rule_engine 同时表示"强制升级规则"和"置信度门限"。用
        `params.mode` 区分，缺省 mode="rules"。
        """
        mode = node.params.get("mode", "rules")
        if mode == "threshold":
            # 「置信度判断」节点：只做一次门限标注，供下游 confidence 边求值。
            # 它不改 rule_match —— 否则会把"规则未命中"和"置信度不足"混成一件事。
            threshold = float(node.params.get("threshold", 0.6))
            return f"置信度 {ctx.confidence:.2f} vs 门限 {threshold}"

        if mode == "keywords":
            # 「关键词升级」节点：读规则引擎阶段算好的关键词信号。
            # 刻意**不**在这里跑 RuleEngine —— 那会重跑一遍并覆盖
            # keyword.matched，让这条条件边的 True 分支永远走不到。
            return f"keyword.matched={ctx.keyword_matched}"

        engine = self._c.get("rule_engine")
        if engine is None:
            return skipped("未注入 rule_engine")
        actions = engine.evaluate({
            "text": ctx.raw_message,
            "user_id": ctx.user_id,
            "chat_id": ctx.chat_id,
        })
        matched = [a for a in actions if str(getattr(a, "action", a)) != "pass"]
        if matched:
            ctx.rule_match = "escalate"
            ctx.keyword_reason = "; ".join(
                str(getattr(a, "reason", a)) for a in matched
            )
            return f"命中 {len(matched)} 条规则 → escalate"
        ctx.rule_match = "pass"
        return "未命中规则 → pass"

    async def _h_tool_exec(self, ctx: WorkflowContext, node: Any) -> str | Skip:
        """工单节点：用 TicketStore 建/取工单。"""
        store = self._c.get("ticket_store")
        if store is None:
            return skipped("未注入 ticket_store")
        ticket = store.get_or_create(
            chat_id=ctx.chat_id,
            user_id=ctx.user_id,
            channel=ctx.channel,
        )
        ctx.ticket_id = ticket.ticket_id
        ctx.ticket_status = ticket.status
        return f"工单 {ticket.ticket_id} ({ticket.status})"

    async def _h_knowledge(self, ctx: WorkflowContext, node: Any) -> str | Skip:
        """知识召回节点。"""
        knowledge = self._c.get("knowledge")
        if knowledge is None:
            return skipped("未注入 knowledge")
        top_k = int(node.params.get("rag_top_k", 5))
        try:
            hits = await knowledge.recall(ctx.raw_message, top_k=top_k)
        except Exception as e:  # noqa: BLE001 — 召回失败不该让整条 DAG 失败
            log.warning("workflow_knowledge_failed", error=str(e))
            return skipped(f"召回失败 {type(e).__name__}")

        max_chars = int(node.params.get("max_chars", 2000))
        ctx.knowledge_hits = _truncate_hits(hits, max_chars)
        ctx.memory_block = _hits_to_text(ctx.knowledge_hits)
        return f"召回 {len(ctx.knowledge_hits)} 段"

    async def _h_llm(self, ctx: WorkflowContext, node: Any) -> str | Skip:
        """LLM 推理节点。走的是 agent.run（与 pipeline 同一个 ZhiXiAgent）。"""
        agent = self._c.get("agent")
        if agent is None:
            return skipped("未注入 agent")

        threshold = float(node.params.get("confidence_threshold", 0.6))
        out = await agent.run(
            raw_message=ctx.raw_message,
            chat_id=ctx.chat_id,
            user_id=ctx.user_id,
            channel=ctx.channel or "qq_private",
            system_prompt=ctx.system_prompt,
            memory_block=ctx.memory_block,
        )
        ctx.llm_output = out.get("final_reply", "")
        ctx.confidence = float(out.get("confidence", threshold))
        escalated = out.get("escalate_reason") or ""
        if escalated:
            ctx.llm_escalate_reason = escalated
            ctx.keyword_reason = ctx.keyword_reason or escalated
        return f"输出 {len(ctx.llm_output)} 字，置信度 {ctx.confidence:.2f}"

    async def _h_seat(self, ctx: WorkflowContext, node: Any) -> str | Skip:
        """坐席分配节点。

        🔗 真实 API 对齐：`TicketAssigner` 的方法是
        `try_assign(ticket)`（返回 `(ok, seat_or_reason)`）——
        此前调的是不存在的 `assigner.assign(...)`，真实装配
        （dashboard_api 注入 assigner）下节点直接 AttributeError。
        分配失败（无空闲坐席 / 工单已分配）是**业务常态**，
        落到入队分支而不是 failed。

        注意：分配策略在 TicketAssigner 构造时固定，节点级
        `params.strategy` 不生效（TicketAssigner 无运行时
        切策略接口），不在此假装支持。
        """
        assigner = self._c.get("assigner")
        queue = self._c.get("queue")
        tickets = self._c.get("ticket_store")

        reason = ctx.keyword_reason or ctx.llm_escalate_reason or "低置信度"

        ticket = None
        if tickets is not None and ctx.ticket_id:
            ticket = tickets.get(ctx.ticket_id)

        if assigner is not None and ticket is not None:
            ok, seat_or_reason = assigner.try_assign(ticket)
            if ok:
                ctx.seat_id = str(seat_or_reason)
                return f"分配坐席 {ctx.seat_id}"
            # 分配失败 → 落到入队分支（无空闲坐席不该让整条 DAG 失败）
            log.info(
                "workflow_seat_assign_fallback",
                reason=seat_or_reason,
            )

        if queue is not None and ticket is not None:
            queue.enqueue(ticket, reason)
            ctx.seat_id = "queued"
            return f"无可用坐席，已入升级队列（{reason}）"

        return skipped("未注入 assigner/queue 或无工单")

    async def _h_audit(self, ctx: WorkflowContext, node: Any) -> str | Skip:
        """审计节点：按 params.events 记录已发生的事件。"""
        audit = self._c.get("audit_log")
        wanted = node.params.get("events") or []
        recorded: list[str] = []

        fired = {
            "ticket.escalated": bool(ctx.seat_id),
            "ticket.replied": bool(ctx.reply_text),
        }
        for event in wanted:
            if not fired.get(str(event), False):
                continue
            entry = {
                "action": str(event),
                "actor": "workflow",
                "target": ctx.ticket_id or ctx.chat_id,
                "detail": ctx.keyword_reason or ctx.reply_text[:120],
            }
            ctx.audit_events.append(entry)
            recorded.append(str(event))
            if audit is not None:
                try:
                    audit.record(
                        action=str(event),
                        actor="workflow",
                        target=entry["target"],
                        detail=entry["detail"],
                    )
                except Exception as e:  # noqa: BLE001 — 审计失败不该阻塞主流程
                    log.warning("workflow_audit_failed", action=event, error=str(e))

        return f"记录 {len(recorded)}/{len(wanted)} 条: {recorded}"


# ── 辅助 ────────────────────────────────────────────────────────────

def _mask(value: str, keep_head: int, keep_tail: int) -> str:
    hidden = max(0, len(value) - keep_head - keep_tail)
    return f"{value[:keep_head]}{'*' * hidden}{value[-keep_tail:] if keep_tail else ''}"


def _mask_email(local: str) -> str:
    """邮箱本地部分遮蔽：保留前 2 字符 + 固定 3 星。

    刻意不按长度等比遮蔽 —— 那会让「1 字符」和「20 字符」的邮箱
    呈现完全不同的形态，反倒泄露长度信息。
    """
    return f"{local[:2]}***"


def _sanitize_output(text: str) -> str:
    """输出侧 PII 收敛：手机号 / 身份证 / 邮箱。

    🔗 关键：必须**遮蔽**数字本身。旧实现写成 `r"\\1[已隐藏]"` ——
    正则只加了个后缀，原号一个字符没少，等于没打码。

    只做保守的数字段遮蔽，不做姓名识别 —— 姓名遮蔽需要 NER，误伤成本
    高于收益（把人名当手机号打码会让客服回复没法读）。
    """
    import re

    text = re.sub(
        r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)",
        lambda m: f"{m[1]}{'*' * 4}{m[3]}",
        text,
    )
    text = re.sub(
        r"(?<!\d)(\d{6})(\d{4})(\d{2}[\dXx])(?!\d)",
        lambda m: f"{m[1]}{'*' * 4}{m[3]}",
        text,
    )
    text = re.sub(
        r"[\w.+-]+@[\w-]+\.[\w.]+",
        lambda m: _mask_email(m[0].split("@")[0]) + "@" + m[0].split("@")[1],
        text,
    )
    return text


def _truncate_hits(hits: Any, max_chars: int) -> list[dict[str, Any]]:
    """按总字数截断召回结果，避免把超长 chunk 全量灌进上下文。"""
    if not isinstance(hits, list):
        return []
    out: list[dict[str, Any]] = []
    total = 0
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        text = str(hit.get("text", ""))
        remaining = max_chars - total
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining] + "…"
        out.append({**hit, "text": text})
        total += len(text)
    return out


def _hits_to_text(hits: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{h.get('source', h.get('doc_id', ''))}] {h.get('text', '')}"
        for h in hits
    )


def build_default_executor(components: dict[str, Any] | None = None) -> WorkflowExecutor:
    """工厂函数。components 键：rule_engine / ticket_store / knowledge /
    agent / queue / assigner / audit_log。
    """
    return WorkflowExecutor(components)


__all__ = [
    "ExecutionResult",
    "NodeTrace",
    "WorkflowContext",
    "WorkflowExecutor",
    "build_default_executor",
    "evaluate_condition",
    "Skip",
    "skipped",
    "MAX_EXECUTION_STEPS",
]
