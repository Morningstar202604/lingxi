"""业务指标埋点测试（对应"函数有定义、全仓零调用"的缺口）。

行为断言：走真实 pipeline 路径后，Prometheus 收集器里必须出现
Agent 调用/升级/业务 Gauge 数据。不 mock collector —— 那会重演
"测试全绿但生产是 0"的老问题。
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lingxi.agent.graph import ZhiXiAgent
from lingxi.core.protocol import (
    ChannelType,
    IMEvent,
    MessageSegment,
    MessageType,
    PostType,
)
from lingxi.flow.pipeline import CustomerServicePipeline
from lingxi.flow.queue import EscalationQueue
from lingxi.flow.ticket import TicketStatus, TicketStore


def _event(text: str = "你好") -> IMEvent:
    return IMEvent(
        post_type=PostType.MESSAGE,
        message_type=MessageType.PRIVATE,
        channel=ChannelType.QQ_PRIVATE,
        user_id="u1",
        raw_message=text,
        segments=[MessageSegment(type="text", data={"text": text})],
    )


def _agent_returning(**result: Any) -> MagicMock:
    agent = MagicMock(spec=ZhiXiAgent)

    async def _run(**kwargs: Any) -> dict[str, Any]:
        return result

    agent.run = _run
    return agent


def _sample(metric_name: str, **labels: str) -> float:
    """从 Prometheus 暴露文本里读一个样本值（0 = 不存在）。

    兼容带标签（`name{k="v"} 1.0`）与无标签（`name 1.0`）两种行：
    - 传了 labels：只认同时含有这些标签的行
    - 没传 labels：只认无标签行（避免误命中 `name{...}`）
    """
    from lingxi.metrics import get_metrics_collector

    text = get_metrics_collector().generate().decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(metric_name):
            continue
        rest = line[len(metric_name):]
        if not rest or rest[0] not in "{ ":
            continue  # 防止前缀撞名（queue_depth 命中 queue_depth_extra）
        has_labels = rest.startswith("{")
        if labels and not has_labels:
            continue
        if labels and not all(f'{k}="{v}"' in rest for k, v in labels.items()):
            continue
        if not labels and has_labels:
            continue
        try:
            return float(line.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            continue
    return 0.0


class TestAgentInvocationMetrics:
    @pytest.mark.asyncio
    async def test_successful_reply_counts_as_resolved(self) -> None:
        before = _sample("zhixi_agent_invocations_total", outcome="resolved")
        pipe = CustomerServicePipeline(
            agent=_agent_returning(final_reply="已为您处理", confidence=0.9),
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
        )
        await pipe.handle_event(_event(), system_prompt="sp")
        after = _sample("zhixi_agent_invocations_total", outcome="resolved")
        assert after == before + 1, "成功回复必须记入 outcome=resolved"

    @pytest.mark.asyncio
    async def test_low_confidence_escalation_is_counted(self) -> None:
        before_esc = _sample("zhixi_agent_escalations_total", reason_category="low_confidence")
        pipe = CustomerServicePipeline(
            agent=_agent_returning(final_reply="可能吧", confidence=0.1),
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
            confidence_threshold=0.6,
        )
        result = await pipe.handle_event(_event(), system_prompt="sp")
        assert result.escalated is True
        after_esc = _sample("zhixi_agent_escalations_total", reason_category="low_confidence")
        assert after_esc == before_esc + 1, "低置信度升级必须计入 low_confidence 分类"

    @pytest.mark.asyncio
    async def test_timeout_counts_as_timeout_outcome(self) -> None:
        agent = MagicMock(spec=ZhiXiAgent)

        async def _slow(**kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(5)
            return {"final_reply": "", "escalate_reason": ""}

        agent.run = _slow
        before = _sample("zhixi_agent_invocations_total", outcome="timeout")
        pipe = CustomerServicePipeline(
            agent=agent,
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
            max_agent_timeout=0.05,
        )
        await pipe.handle_event(_event(), system_prompt="sp")
        after = _sample("zhixi_agent_invocations_total", outcome="timeout")
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_agent_exception_counts_error_then_raises(self) -> None:
        agent = MagicMock(spec=ZhiXiAgent)
        agent.run = AsyncMock(side_effect=RuntimeError("boom"))
        before = _sample("zhixi_agent_invocations_total", outcome="error")
        pipe = CustomerServicePipeline(
            agent=agent,
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
        )
        with pytest.raises(RuntimeError):
            await pipe.handle_event(_event(), system_prompt="sp")
        after = _sample("zhixi_agent_invocations_total", outcome="error")
        assert after == before + 1, "异常也必须计数（失败面要可观测），且异常照常上抛"

    @pytest.mark.asyncio
    async def test_duration_histogram_observed(self) -> None:
        """延迟直方图要有样本（+Inf 桶计数增加即证明 observe 被调用）。"""
        before = _sample(
            "zhixi_agent_invocation_duration_seconds_bucket",
            channel="qq_private",
            le="+Inf",
        )
        pipe = CustomerServicePipeline(
            agent=_agent_returning(final_reply="ok", confidence=0.9),
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
        )
        await pipe.handle_event(_event(), system_prompt="sp")
        after = _sample(
            "zhixi_agent_invocation_duration_seconds_bucket",
            channel="qq_private",
            le="+Inf",
        )
        assert after == before + 1


class TestBusinessGauges:
    @pytest.mark.asyncio
    async def test_refresh_writes_ticket_distribution_and_queue_depth(self) -> None:
        """_refresh_business_metrics 必须把真实分布写进 Gauge。"""
        from lingxi.__main__ import _refresh_business_metrics

        store, queue = TicketStore(), EscalationQueue()
        t1 = store.get_or_create("c1", user_id="u1", channel="qq")
        t1.transition(TicketStatus.PROCESSING)
        t2 = store.get_or_create("c2", user_id="u2", channel="qq")
        queue.enqueue(t2, reason="test")

        task = asyncio.create_task(
            _refresh_business_metrics(store, queue, interval=0.05)
        )
        await asyncio.sleep(0.2)
        task.cancel()

        assert _sample("zhixi_tickets_active", status="processing") >= 1
        assert _sample("zhixi_tickets_active", status="intake") >= 1
        assert _sample("zhixi_queue_depth") >= 1

    @pytest.mark.asyncio
    async def test_refresh_survives_store_errors(self) -> None:
        """存储偶发异常不能杀死刷新协程（下一轮继续）。"""
        from lingxi.__main__ import _refresh_business_metrics

        calls = {"n": 0}

        class FlakyStore:
            def list_active(self) -> list[Any]:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient")
                return []

        class ZeroQueue:
            pending_count = 0

        task = asyncio.create_task(
            _refresh_business_metrics(FlakyStore(), ZeroQueue(), interval=0.05)
        )
        await asyncio.sleep(0.25)
        alive = not task.done()
        task.cancel()
        assert alive, "首次异常后协程必须继续存活"
        assert calls["n"] >= 2, "异常后应继续下一轮刷新"
