"""13 模块开关的行为接线测试（variance / translate / lorebook_injection）。

对应技术债：这三个开关此前是"配了也静默失效"的死配置
（AUDIT-2026-10-10 第四章 4.4）。
"""
from __future__ import annotations

import pydantic
import pytest

from lingxi.persona.injector import PersonaInjector
from lingxi.persona.schema import BehaviorRules, ContextModule, PersonaConfig


def _injector(**module_flags: bool) -> PersonaInjector:
    persona = PersonaConfig(
        identity={"name": "智犀"},
        modules=ContextModule(**module_flags),
    )
    return PersonaInjector(persona)


class TestVarianceModule:
    def test_style_line_injected_when_enabled(self) -> None:
        prompt = _injector(variance=True).build_system_prompt(user_message="你好")
        assert "[本轮措辞风格:" in prompt

    def test_no_style_line_when_disabled(self) -> None:
        prompt = _injector(variance=False).build_system_prompt(user_message="你好")
        assert "[本轮措辞风格" not in prompt

    def test_style_pick_is_idempotent(self) -> None:
        """同输入 → 同风格（注入幂等，模块头部是硬约束）。"""
        p1 = _injector().build_system_prompt(user_message="同一句话")
        p2 = _injector().build_system_prompt(user_message="同一句话")
        assert p1 == p2


class TestTranslateModule:
    def test_language_line_when_enabled(self) -> None:
        prompt = _injector(translate=True).build_system_prompt()
        assert "[语言]" in prompt

    def test_no_language_line_when_disabled(self) -> None:
        prompt = _injector(translate=False).build_system_prompt()
        assert "[语言]" not in prompt


class TestLorebookInjectionModule:
    def test_block_injected_when_enabled(self) -> None:
        prompt = _injector(lorebook_injection=True).build_system_prompt(
            lorebook_block="退货政策：7 天无理由",
        )
        assert "[世界观 / 业务规则]" in prompt

    def test_block_suppressed_when_disabled(self) -> None:
        prompt = _injector(lorebook_injection=False).build_system_prompt(
            lorebook_block="退货政策：7 天无理由",
        )
        assert "[世界观" not in prompt


class TestSchemaConstraints:
    def test_negative_max_reply_chars_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            BehaviorRules(max_reply_chars=-5)

    def test_zero_max_reply_chars_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            BehaviorRules(max_reply_chars=0)

    def test_version_accepts_yaml_int(self) -> None:
        """YAML 里 version: 1（未加引号）是 int，必须能解析。"""
        persona = PersonaConfig.model_validate({"version": 1, "identity": {"name": "X"}})
        assert persona.version == "1"
