"""Phase 2 测试 — SLA 监控 + 自动化规则引擎。

🔗 工程逻辑
    Phase 2 新增两个模块:
    - sla.py: SLA 超时检查 + format_sla_status
    - rules.py: 规则引擎 (条件匹配 → 动作执行)

    全部使用内存构造，不依赖数据库/网络。

🤖 AI 避坑
    - async 测试必须 @pytest.mark.asyncio
    - ticket 的 created_at 用 time 戳调小来模拟超时
"""
from __future__ import annotations

import time

import pytest

from lingxi.flow.rules import Rule, RuleAction, RuleCondition, RuleEngine
from lingxi.flow.sla import SLADefinition, SLAMonitor, format_sla_status
from lingxi.flow.ticket import Ticket, TicketStatus

# ── Fixtures ──

def _make_ticket(status=TicketStatus.INTAKE, created_seconds_ago=0) -> Ticket:
    """构造一个有指定状态的 Ticket, 可回溯 created_at。"""
    t = Ticket()
    t.status = status
    t.created_at = time.time() - created_seconds_ago
    return t


def _make_sla(
    first_response: int = 300,
    resolution: int = 3600,
    escalation: int = 600,
) -> SLAMonitor:
    """构造带自定义超时的 SLAMonitor。"""
    sla_def = SLADefinition(
        first_response_timeout=first_response,
        resolution_timeout=resolution,
        escalation_timeout=escalation,
    )
    return SLAMonitor(sla_def)


# ── SLA Tests ──

class TestSLAMonitorCheckTicket:
    """SLAMonitor.check_ticket 基本场景。"""

    def test_no_breach_new_ticket(self) -> None:
        """新建工单 (0秒) 不违规。"""
        monitor = _make_sla()
        ticket = _make_ticket(TicketStatus.INTAKE, created_seconds_ago=10)
        result = monitor.check_ticket(ticket)
        assert result["breached"] is False
        assert result["reasons"] == []
        assert result["remaining_seconds"] is not None
        assert result["remaining_seconds"] > 0

    def test_first_response_breach(self) -> None:
        """PROCESSING 超过 first_response_timeout → breach FIRST_RESPONSE。"""
        monitor = _make_sla(first_response=60)
        ticket = _make_ticket(TicketStatus.PROCESSING, created_seconds_ago=120)
        result = monitor.check_ticket(ticket)
        assert result["breached"] is True
        assert "FIRST_RESPONSE" in result["reasons"]

    def test_resolution_breach(self) -> None:
        """WAITING_USER 超过 resolution_timeout → breach RESOLUTION。"""
        monitor = _make_sla(resolution=300)
        ticket = _make_ticket(TicketStatus.WAITING_USER, created_seconds_ago=400)
        result = monitor.check_ticket(ticket)
        assert result["breached"] is True
        assert "RESOLUTION" in result["reasons"]

    def test_escalated_resolution_breach(self) -> None:
        """ESCALATED 超过 resolution_timeout → breach RESOLUTION (首响已处理不检查)。"""
        monitor = _make_sla(first_response=30, resolution=100)
        # 总耗时超过 resolution 但首响已处理 (不在 INTAKE/PROCESSING)
        ticket = _make_ticket(TicketStatus.ESCALATED, created_seconds_ago=200)
        result = monitor.check_ticket(ticket)
        assert result["breached"] is True
        assert "RESOLUTION" in result["reasons"]
        assert "FIRST_RESPONSE" not in result["reasons"]

    def test_no_breach_before_timeout(self) -> None:
        """距超时还差很远 → 不违规。"""
        monitor = _make_sla(first_response=300, resolution=3600)
        ticket = _make_ticket(TicketStatus.INTAKE, created_seconds_ago=10)
        result = monitor.check_ticket(ticket)
        assert result["breached"] is False
        assert result["remaining_seconds"] is not None
        assert result["remaining_seconds"] < 300

    def test_both_breach_when_very_old(self) -> None:
        """极老的 INTAKE 工单: 同时触发 FIST_RESPONSE + RESOLUTION。"""
        monitor = _make_sla(first_response=60, resolution=120)
        ticket = _make_ticket(TicketStatus.INTAKE, created_seconds_ago=200)
        result = monitor.check_ticket(ticket)
        assert result["breached"] is True
        assert "FIRST_RESPONSE" in result["reasons"]
        assert "RESOLUTION" in result["reasons"]


class TestFormatSlaStatus:
    """format_sla_status 汇总计算。"""

    def test_empty_list(self) -> None:
        result = format_sla_status([])
        assert result["total"] == 0
        assert result["breached"] == 0
        assert result["compliant"] == 0
        assert result["breach_rate"] == 0.0

    def test_mixed_results(self) -> None:
        results = [
            {"breached": True, "reasons": ["FIRST_RESPONSE"]},
            {"breached": False, "reasons": []},
            {"breached": True, "reasons": ["RESOLUTION"]},
            {"breached": False, "reasons": []},
        ]
        result = format_sla_status(results)
        assert result["total"] == 4
        assert result["breached"] == 2
        assert result["compliant"] == 2
        assert result["breach_rate"] == 0.5

    def test_all_compliant(self) -> None:
        results = [{"breached": False}, {"breached": False}]
        result = format_sla_status(results)
        assert result["breach_rate"] == 0.0
        assert result["compliant"] == 2

    def test_all_breached(self) -> None:
        results = [{"breached": True}, {"breached": True}]
        result = format_sla_status(results)
        assert result["breach_rate"] == 1.0
        assert result["compliant"] == 0


# ── RuleEngine Tests ──

class TestRuleEngineEvaluate:
    """RuleEngine.evaluate 条件匹配。"""

    def test_single_rule_match(self) -> None:
        """单条规则命中: message_contains 触发 escalate。"""
        engine = RuleEngine()
        rule = Rule(
            name="complaint_escalate",
            conditions=[
                RuleCondition(
                    field="message_contains",
                    operator="contains",
                    value="投诉",
                ),
            ],
            actions=[RuleAction(action="escalate")],
        )
        engine.add_rule(rule)

        event = {"message": "我要投诉这家店", "confidence": 0.5}
        actions = engine.evaluate(event)
        assert len(actions) == 1
        assert actions[0].action == "escalate"

    def test_single_rule_no_match(self) -> None:
        """消息不含关键词 → 不匹配。"""
        engine = RuleEngine()
        rule = Rule(
            name="complaint_escalate",
            conditions=[
                RuleCondition(
                    field="message_contains",
                    operator="contains",
                    value="投诉",
                ),
            ],
            actions=[RuleAction(action="escalate")],
        )
        engine.add_rule(rule)

        event = {"message": "请问退货怎么操作", "confidence": 0.5}
        actions = engine.evaluate(event)
        assert actions == []

    def test_and_conditions_all_match(self) -> None:
        """AND 条件全部满足 → 命中。"""
        engine = RuleEngine()
        rule = Rule(
            name="low_confidence_complaint",
            conditions=[
                RuleCondition(
                    field="message_contains",
                    operator="contains",
                    value="投诉",
                ),
                RuleCondition(
                    field="confidence_below",
                    operator="value",
                    value=0.3,
                ),
            ],
            actions=[RuleAction(action="escalate")],
        )
        engine.add_rule(rule)

        event = {"message": "投诉你们", "confidence": 0.2}
        actions = engine.evaluate(event)
        assert len(actions) == 1

    def test_and_conditions_partial_match(self) -> None:
        """AND 条件仅满足一个 → 不命中。"""
        engine = RuleEngine()
        rule = Rule(
            name="low_confidence_complaint",
            conditions=[
                RuleCondition(
                    field="message_contains",
                    operator="contains",
                    value="投诉",
                ),
                RuleCondition(
                    field="confidence_below",
                    operator="value",
                    value=0.3,
                ),
            ],
            actions=[RuleAction(action="escalate")],
        )
        engine.add_rule(rule)

        # confidence_below 不满足
        event = {"message": "投诉你们", "confidence": 0.9}
        actions = engine.evaluate(event)
        assert actions == []

    def test_priority_sorting(self) -> None:
        """priority 高的规则动作排在前面。"""
        engine = RuleEngine()
        low_rule = Rule(
            name="low_priority",
            priority=1,
            conditions=[
                RuleCondition(field="channel", operator="equals", value="qq"),
            ],
            actions=[RuleAction(action="tag", params={"tag": "low"})],
        )
        high_rule = Rule(
            name="high_priority",
            priority=10,
            conditions=[
                RuleCondition(field="channel", operator="equals", value="qq"),
            ],
            actions=[RuleAction(action="escalate")],
        )
        engine.add_rule(low_rule)
        engine.add_rule(high_rule)

        actions = engine.evaluate({"channel": "qq"})
        assert len(actions) == 2
        # 高优先级排在前
        assert actions[0].action == "escalate"
        assert actions[1].action == "tag"

    def test_disabled_rule_skipped(self) -> None:
        """enabled=False 的规则不会被评估。"""
        engine = RuleEngine()
        rule = Rule(
            name="disabled_rule",
            enabled=False,
            conditions=[
                RuleCondition(field="channel", operator="equals", value="qq"),
            ],
            actions=[RuleAction(action="escalate")],
        )
        engine.add_rule(rule)

        actions = engine.evaluate({"channel": "qq"})
        assert actions == []


class TestRuleEngineExecute:
    """RuleEngine.execute 实际执行动作。"""

    @pytest.mark.asyncio
    async def test_execute_escalate_action(self) -> None:
        """escalate action → ticket.transition(ESCALATED) + enqueue。"""
        engine = RuleEngine()
        rule = Rule(
            name="test_escalate",
            conditions=[RuleCondition(field="channel", operator="equals", value="qq")],
            actions=[RuleAction(action="escalate")],
        )
        engine.add_rule(rule)

        ticket = _make_ticket(TicketStatus.INTAKE)

        class FakeQueue:
            def __init__(self) -> None:
                self.enqueued: list = []

            def enqueue(self, t, **kwargs) -> None:
                self.enqueued.append((t.ticket_id, kwargs))

        fake_queue = FakeQueue()

        result = await engine.execute(
            {"channel": "qq"},
            ticket=ticket,
            escalate_to_queue=fake_queue,
        )

        assert "escalate" in result["executed"]
        assert ticket.status == TicketStatus.ESCALATED
        assert len(fake_queue.enqueued) == 1
        assert fake_queue.enqueued[0][0] == ticket.ticket_id

    @pytest.mark.asyncio
    async def test_execute_reply_template(self) -> None:
        """reply_template action → 正常执行无错误。"""
        engine = RuleEngine()
        rule = Rule(
            name="test_reply",
            conditions=[RuleCondition(field="channel", operator="equals", value="qq")],
            actions=[
                RuleAction(
                    action="reply_template",
                    params={"template": "您好，已收到您的消息"},
                ),
            ],
        )
        engine.add_rule(rule)

        result = await engine.execute(
            {"channel": "qq"},
            ticket=None,
        )

        assert "reply_template" in result["executed"]
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_execute_tag_action(self) -> None:
        """tag action → 添加 tag 到 ticket。"""
        engine = RuleEngine()
        rule = Rule(
            name="test_tag",
            conditions=[RuleCondition(field="channel", operator="equals", value="qq")],
            actions=[RuleAction(action="tag", params={"tag": "vip"})],
        )
        engine.add_rule(rule)

        ticket = _make_ticket()

        await engine.execute(
            {"channel": "qq"},
            ticket=ticket,
        )

        tags = getattr(ticket, "_tags", [])
        assert "vip" in tags


class TestRuleEngineYaml:
    """YAML 序列化/反序列化。"""

    def test_load_yaml_from_string(self) -> None:
        """从 YAML 字符串加载规则。"""
        import yaml

        yaml_str = yaml.dump([
            {
                "name": "complaint_rule",
                "conditions": [
                    {"field": "message_contains", "operator": "contains", "value": "投诉"},
                ],
                "actions": [{"action": "escalate"}],
            },
            {
                "name": "low_conf_rule",
                "conditions": [
                    {"field": "confidence_below", "operator": "value", "value": 0.3},
                ],
                "actions": [{"action": "escalate"}],
            },
        ])

        import os
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write(yaml_str)
            tmp_path = f.name

        try:
            engine = RuleEngine()
            count = engine.load_yaml(tmp_path)
            assert count == 2
            assert len(engine.list_rules()) == 2
        finally:
            os.unlink(tmp_path)


class TestRuleFullIntegration:
    """完整集成: 新建 Rule → engine.evaluate(event) 验证。"""

    def test_full_complaint_flow(self) -> None:
        """构造一条投诉升级规则并验证匹配。"""
        engine = RuleEngine()
        rule = Rule(
            name="complaint_auto_escalate",
            enabled=True,
            conditions=[
                RuleCondition(
                    field="message_contains",
                    operator="contains",
                    value="投诉",
                ),
            ],
            actions=[
                RuleAction(action="escalate"),
                RuleAction(action="tag", params={"tag": "complaint"}),
            ],
            priority=10,
        )
        engine.add_rule(rule)

        # 命中
        event = {
            "message": "我要投诉你们的物流",
            "confidence": 0.8,
            "ticket_status": "intake",
            "channel": "qq_private",
            "elapsed_seconds": 30,
        }
        actions = engine.evaluate(event)
        assert len(actions) == 2
        action_types = [a.action for a in actions]
        assert "escalate" in action_types
        assert "tag" in action_types

        # 不命中
        event2 = {"message": "退货流程", "confidence": 0.8}
        actions2 = engine.evaluate(event2)
        assert actions2 == []
