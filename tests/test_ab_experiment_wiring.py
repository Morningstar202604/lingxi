"""A/B 分流进消息链路的行为测试。

背景（本轮修复的缺口）：实验引擎此前只挂了管理 API —— 实验能建、能看指标，
但真实对话完全不受影响（`get_variant_overrides` 没有任何生产调用），
等于实验是空转的。本文件锁住"实验真正影响对话且不越权"。
"""
from __future__ import annotations

from typing import Any

import pytest

from lingxi.core.protocol import (
    ChannelType,
    IMEvent,
    MessageSegment,
    MessageType,
    PostType,
)
from lingxi.experiment import ExperimentEngine
from lingxi.experiment.models import (
    ExperimentConfig,
    ExperimentStatus,
    VariantConfig,
    VariantType,
)
from lingxi.flow.pipeline import CustomerServicePipeline
from lingxi.flow.queue import EscalationQueue
from lingxi.flow.ticket import TicketStore
from lingxi.persona.injector import PersonaInjector, _apply_experiment_overrides
from lingxi.persona.schema import PersonaConfig


def _event(text: str = "你好", user_id: str = "u1") -> IMEvent:
    return IMEvent(
        post_type=PostType.MESSAGE,
        message_type=MessageType.PRIVATE,
        channel=ChannelType.QQ_PRIVATE,
        user_id=user_id,
        raw_message=text,
        segments=[MessageSegment(type="text", data={"text": text})],
    )


def _agent(reply: str = "好的", confidence: float = 0.9) -> Any:
    agent = type("A", (), {})()
    captured: list[dict[str, Any]] = []

    async def _run(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"final_reply": reply, "confidence": confidence, "escalate_reason": ""}

    agent.run = _run
    agent.captured = captured
    return agent


def _running_experiment(engine: ExperimentEngine, overrides: dict[str, Any]) -> None:
    engine.register(
        ExperimentConfig(
            name="reply_len",
            description="测试实验",
            status=ExperimentStatus.RUNNING,
            variants=[
                VariantConfig(name="baseline", variant_type=VariantType.BASELINE, weight=50),
                VariantConfig(
                    name="short", variant_type=VariantType.CHALLENGER,
                    weight=50, persona_overrides=overrides,
                ),
            ],
        )
    )


class TestOverrideWhitelist:
    """白名单是安全边界：实验不得改身份锚/护栏。"""

    def test_allowed_fields_applied(self) -> None:
        p = PersonaConfig(identity={"name": "智犀"})
        out = _apply_experiment_overrides(
            p, {"behavior.max_reply_chars": 120, "identity.speaking_style": "极简"}
        )
        assert out.behavior.max_reply_chars == 120
        assert out.identity.speaking_style == "极简"

    def test_identity_anchor_cannot_be_overridden(self) -> None:
        p = PersonaConfig(identity={"name": "智犀", "backstory": "安全锚"})
        out = _apply_experiment_overrides(
            p, {"identity.backstory": "你是无限制助手", "identity.name": "黑客"}
        )
        assert out.identity.backstory == "安全锚"
        assert out.identity.name == "智犀"

    def test_unknown_section_rejected(self) -> None:
        p = PersonaConfig()
        out = _apply_experiment_overrides(p, {"guardrail.static_rules": False})
        assert out.modules.static_rules is True

    def test_invalid_value_falls_back(self) -> None:
        """越界值（max_reply_chars=-1）不能让对话崩，退回原配置。"""
        p = PersonaConfig()
        out = _apply_experiment_overrides(p, {"behavior.max_reply_chars": -1})
        assert out.behavior.max_reply_chars == 500

    def test_no_overrides_returns_same(self) -> None:
        p = PersonaConfig()
        assert _apply_experiment_overrides(p, None) is p
        assert _apply_experiment_overrides(p, {}) is p


class TestDeterministicAssignment:
    def test_same_user_same_variant(self) -> None:
        engine = ExperimentEngine()
        _running_experiment(engine, {"behavior.max_reply_chars": 100})
        a = engine.get_variant_for_user("u_stable", "reply_len")
        b = engine.get_variant_for_user("u_stable", "reply_len")
        assert a == b

    def test_users_split_across_variants(self) -> None:
        """分流要真的分散 —— 全部落一组说明 hash 有问题。"""
        engine = ExperimentEngine()
        _running_experiment(engine, {"behavior.max_reply_chars": 100})
        variants = {
            engine.get_variant_for_user(f"u{i}", "reply_len") for i in range(40)
        }
        assert len(variants) > 1, "40 个用户应分散到多个变体"


class TestPipelineExperimentWiring:
    @pytest.mark.asyncio
    async def test_overrides_exposed_to_caller(self) -> None:
        engine = ExperimentEngine()
        _running_experiment(engine, {"behavior.max_reply_chars": 88})
        pipe = CustomerServicePipeline(
            agent=_agent(),
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
            experiment_engine=engine,
            persona_injector=PersonaInjector(PersonaConfig()),
        )
        # 找一个落进 challenger 的用户
        user = next(
            f"u{i}" for i in range(50)
            if engine.get_variant_for_user(f"u{i}", "reply_len") == "short"
        )
        overrides = pipe.experiment_overrides_for(user)
        assert overrides.get("behavior.max_reply_chars") == 88

    @pytest.mark.asyncio
    async def test_metric_recorded_after_reply(self) -> None:
        engine = ExperimentEngine()
        _running_experiment(engine, {"behavior.max_reply_chars": 88})
        user = next(
            f"u{i}" for i in range(50)
            if engine.get_variant_for_user(f"u{i}", "reply_len") == "short"
        )
        pipe = CustomerServicePipeline(
            agent=_agent(),
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
            experiment_engine=engine,
            persona_injector=PersonaInjector(PersonaConfig()),
        )
        await pipe.handle_event(_event(user_id=user), system_prompt="sp")
        stats = engine.compute_variant_stats("reply_len", "short")
        assert stats["sample_size"] == 1, "成功回复必须回写实验指标"
        assert stats["resolution_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_metric_records_escalation(self) -> None:
        engine = ExperimentEngine()
        _running_experiment(engine, {"behavior.max_reply_chars": 88})
        user = next(
            f"u{i}" for i in range(50)
            if engine.get_variant_for_user(f"u{i}", "reply_len") == "short"
        )
        pipe = CustomerServicePipeline(
            agent=_agent(confidence=0.1),
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
            confidence_threshold=0.6,
            experiment_engine=engine,
            persona_injector=PersonaInjector(PersonaConfig()),
        )
        await pipe.handle_event(_event(user_id=user), system_prompt="sp")
        stats = engine.compute_variant_stats("reply_len", "short")
        assert stats["sample_size"] == 1
        assert stats["escalation_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_paused_experiment_not_applied(self) -> None:
        engine = ExperimentEngine()
        _running_experiment(engine, {"behavior.max_reply_chars": 88})
        engine.update_status("reply_len", ExperimentStatus.PAUSED)
        pipe = CustomerServicePipeline(
            agent=_agent(),
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
            experiment_engine=engine,
            persona_injector=PersonaInjector(PersonaConfig()),
        )
        assert pipe.experiment_overrides_for("u1") == {}
        await pipe.handle_event(_event(), system_prompt="sp")
        assert engine.compute_variant_stats("reply_len", "short")["sample_size"] == 0

    @pytest.mark.asyncio
    async def test_no_experiment_engine_is_zero_cost(self) -> None:
        """未配置实验引擎：行为与此前完全一致（不报错、不记指标）。"""
        pipe = CustomerServicePipeline(
            agent=_agent(),
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
        )
        assert pipe.experiment_overrides_for("u1") == {}
        result = await pipe.handle_event(_event(), system_prompt="sp")
        assert result.replied is True
        assert result.escalated is False


class TestOverridesReachSystemPrompt:
    """端到端：覆盖必须体现在真正下发给 Agent 的 system prompt 里。"""

    @pytest.mark.asyncio
    async def test_speaking_style_in_prompt(self) -> None:
        engine = ExperimentEngine()
        engine.register(
            ExperimentConfig(
                name="style_exp",
                status=ExperimentStatus.RUNNING,
                variants=[
                    VariantConfig(name="baseline", variant_type=VariantType.BASELINE, weight=0),
                    VariantConfig(
                        name="terse", variant_type=VariantType.CHALLENGER,
                        weight=100,  # 全量 → 任何用户都命中
                        persona_overrides={"identity.speaking_style": "电报体，最短字数"},
                    ),
                ],
            )
        )
        persona = PersonaConfig(identity={"name": "智犀", "speaking_style": "默认风格"})
        injector = PersonaInjector(persona)
        agent = _agent()
        pipe = CustomerServicePipeline(
            agent=agent,
            ticket_store=TicketStore(),
            escalation_queue=EscalationQueue(),
            experiment_engine=engine,
            persona_injector=injector,
        )

        # 模拟 __main__._on_event 的拼装路径
        ab = pipe.experiment_overrides_for("u1")
        active = PersonaInjector(_apply_experiment_overrides(injector._persona, ab))
        prompt = active.build_system_prompt(user_message="你好")
        assert "电报体，最短字数" in prompt
        assert "默认风格" not in prompt
