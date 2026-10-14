"""Prometheus 指标收集器。

🔗 工程逻辑
    使用 prometheus_client 库（Prometheus 官方 Python SDK），
    定义计数器/直方图/仪表盘三类指标，
    暴露 /metrics 端点供 Prometheus scraper 抓取。

    关键指标：
    - http_requests_total: Counter — 按 method/endpoint/status 分类
    - http_request_duration_seconds: Histogram — 按 endpoint 分类
    - agent_invocations_total: Counter — Agent 调用的次数
    - agent_invocation_duration_seconds: Histogram — Agent 耗时
    - agent_escalations_total: Counter — 升级次数
    - tickets_active: Gauge — 当前活跃工单数
    - queue_depth: Gauge — 升级队列深度

🤖 AI 避坑
    - prometheus_client 在多进程模式下需要 multiprocess 模块，单进程不需要
    - Histogram buckets 要根据实际业务场景预设，不能太粗不能太细
    - Counter 只增不减，Gauge 可增可减，Histogram 只记录分布
    - 不要在 hot path 里创建新的 label 值（label 值组合数爆炸）
"""
from __future__ import annotations

import time
from functools import wraps
from typing import TYPE_CHECKING, Any

from lingxi.core import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False

log = get_logger(__name__)


class MetricsCollector:
    """Prometheus 指标收集器（可独立使用 / 可注入 FastAPI）。"""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        if not HAS_PROMETHEUS:
            # 降级：所有操作 no-op
            self._noop = True
            log.info("prometheus_client not installed, metrics disabled (noop mode)")
            return
        self._noop = False
        self._registry = registry or CollectorRegistry()

        # HTTP 指标
        self.http_requests_total = Counter(
            "zhixi_http_requests_total",
            "Total HTTP requests",
            ["method", "endpoint", "status"],
            registry=self._registry,
        )
        self.http_request_duration = Histogram(
            "zhixi_http_request_duration_seconds",
            "HTTP request latency",
            ["endpoint"],
            buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
            registry=self._registry,
        )

        # Agent 指标
        self.agent_invocations_total = Counter(
            "zhixi_agent_invocations_total",
            "Total agent invocations",
            ["channel", "outcome"],  # outcome: success/timeout/error/escalated
            registry=self._registry,
        )
        self.agent_invocation_duration = Histogram(
            "zhixi_agent_invocation_duration_seconds",
            "Agent invocation duration",
            ["channel"],
            buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0],
            registry=self._registry,
        )
        self.agent_escalations_total = Counter(
            "zhixi_agent_escalations_total",
            "Total agent escalations",
            ["reason_category"],  # low_confidence/timeout/injection/keyword/none
            registry=self._registry,
        )

        # 业务指标 (Gauge — 周期性更新)
        self.tickets_active = Gauge(
            "zhixi_tickets_active",
            "Current active tickets",
            ["status"],  # processing/waiting_user/escalated
            registry=self._registry,
        )
        self.queue_depth = Gauge(
            "zhixi_queue_depth",
            "Current escalation queue depth",
            registry=self._registry,
        )

        # 知识库指标
        self.knowledge_operations_total = Counter(
            "zhixi_knowledge_operations_total",
            "Knowledge base operations",
            ["operation"],  # upload/recall/delete/reset
            registry=self._registry,
        )
        self.knowledge_recall_duration = Histogram(
            "zhixi_knowledge_recall_duration_seconds",
            "Knowledge recall latency",
            buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 8.0],
            registry=self._registry,
        )

        # ── LLM 调用指标（由 litellm 官方 callback 驱动，见 agent/llm_observability.py）──
        self.llm_tokens_total = Counter(
            "zhixi_llm_tokens_total",
            "LLM token 消耗（按 provider/model/类型分类）",
            ["provider", "model", "kind"],  # kind: prompt/completion
            registry=self._registry,
        )
        self.llm_cost_usd_total = Counter(
            "zhixi_llm_cost_usd_total",
            "LLM 累计成本（美元，litellm 内置价格表推算）",
            ["provider", "model"],
            registry=self._registry,
        )
        self.llm_requests_total = Counter(
            "zhixi_llm_requests_total",
            "LLM 调用次数（success/failure）",
            ["provider", "model", "status"],
            registry=self._registry,
        )
        self.llm_latency_seconds = Histogram(
            "zhixi_llm_latency_seconds",
            "LLM 端到端延迟",
            ["provider", "model"],
            buckets=[0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0],
            registry=self._registry,
        )
        self.llm_cache_hits_total = Counter(
            "zhixi_llm_cache_hits_total",
            "LLM 缓存命中次数（litellm 官方 cache）",
            ["provider", "model"],
            registry=self._registry,
        )

    # ── LLM 记录入口（litellm callback / call_llm 共用）──

    def record_llm_success(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
        duration: float = 0.0,
        cache_hit: bool = False,
    ) -> None:
        if self._noop:
            return
        self.llm_requests_total.labels(provider=provider, model=model, status="success").inc()
        if prompt_tokens:
            self.llm_tokens_total.labels(provider=provider, model=model, kind="prompt").inc(prompt_tokens)
        if completion_tokens:
            self.llm_tokens_total.labels(provider=provider, model=model, kind="completion").inc(completion_tokens)
        if cost_usd:
            self.llm_cost_usd_total.labels(provider=provider, model=model).inc(cost_usd)
        if duration:
            self.llm_latency_seconds.labels(provider=provider, model=model).observe(duration)
        if cache_hit:
            self.llm_cache_hits_total.labels(provider=provider, model=model).inc()

    def record_llm_failure(self, *, provider: str, model: str) -> None:
        if self._noop:
            return
        self.llm_requests_total.labels(provider=provider, model=model, status="failure").inc()

    def record_http_request(self, method: str, endpoint: str, status: int, duration: float) -> None:
        """记录 HTTP 请求。"""
        if self._noop:
            return
        self.http_requests_total.labels(method=method, endpoint=endpoint, status=str(status)).inc()
        self.http_request_duration.labels(endpoint=endpoint).observe(duration)

    def record_agent_invocation(self, channel: str, outcome: str, duration: float) -> None:
        """记录 Agent 调用。"""
        if self._noop:
            return
        self.agent_invocations_total.labels(channel=channel, outcome=outcome).inc()
        self.agent_invocation_duration.labels(channel=channel).observe(duration)

    def record_escalation(self, reason: str = "") -> None:
        """记录一次升级事件。"""
        if self._noop:
            return
        category = self._categorize_reason(reason)
        self.agent_escalations_total.labels(reason_category=category).inc()

    @staticmethod
    def _categorize_reason(reason: str) -> str:
        """将升级原因映射到预设的分类标签。"""
        if not reason:
            return "none"
        lowered = reason.lower()
        if "confidence" in lowered:
            return "low_confidence"
        if "timeout" in lowered:
            return "timeout"
        if "injection" in lowered:
            return "injection"
        if "keyword" in lowered or "rule" in lowered:
            return "keyword"
        return "other"

    def update_business_metrics(self, tickets_by_status: dict[str, int], queue_depth: int) -> None:
        """周期性更新 Gauge 指标。"""
        if self._noop:
            return
        for status, count in tickets_by_status.items():
            self.tickets_active.labels(status=status).set(count)
        self.queue_depth.set(queue_depth)

    def generate(self) -> bytes:
        """生成 Prometheus exposition 格式数据。"""
        if self._noop:
            return b"# prometheus_client not installed\n"
        return generate_latest(self._registry)

    def track(
        self, endpoint: str
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        """装饰器：自动记录 HTTP method/duration/status。

        用于手动包装的 endpoint 上。
        """

        def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            @wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                if self._noop:
                    return await func(*args, **kwargs)
                start = time.monotonic()
                try:
                    result = await func(*args, **kwargs)
                    status = 200
                    return result
                except Exception as e:
                    status = getattr(e, "status_code", 500)
                    raise
                finally:
                    duration = time.monotonic() - start
                    self.record_http_request("GET", endpoint, status, duration)

            return wrapper

        return decorator


# 全局单例
_metrics_collector: MetricsCollector | None = None


def get_metrics_collector() -> MetricsCollector:
    """获取全局单例 MetricsCollector。"""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector
