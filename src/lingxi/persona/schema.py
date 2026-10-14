"""Persona schema — 13 模块人格配置的定义层。

🔗 工程逻辑
    从 lingxi/persona/__init__.py 拆出，避免循环导入和过大的 __init__.py。
    YAML/JSON 配置直接 → PersonaConfig.model_validate(...) 解析。

🤖 AI 避坑
    - schema 层只定义数据，不包含注入或校验逻辑（Injector 管注入，Guardrail 管护栏）
    - 所有字段有默认值，用户只需填 identity.name 就能跑
    - custom_prompts 的 key 命名用 snake_case 避免 YAML 解析歧义
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class EmotionLevel(StrEnum):
    """情绪强度分级。"""

    CALM = "calm"
    PLEASED = "pleased"
    NEUTRAL = "neutral"
    IRRITATED = "irritated"
    APOLOGETIC = "apologetic"


class TurnStage(StrEnum):
    """对话阶段。决定回复策略和引导语。"""

    OPENING = "opening"
    GATHERING = "gathering"
    RESOLVING = "resolving"
    CLOSING = "closing"


class Identity(BaseModel):
    """身份定义 — 不可篡改的角色锚。"""

    name: str = "智犀"
    role: str = "全栈数字助理"
    backstory: str = ""
    avatar_url: str | None = None
    speaking_style: str = "简洁、专业、带一点温暖"
    signature: str = ""


class BehaviorRules(BaseModel):
    """行为规则 — 回复的形式约束。"""

    # ge=1：负数/0 会渲染出"单次回复不超过 -5 字"这种荒谬约束
    # （AUDIT-2026-10-10 第四章 4.6）
    max_reply_chars: int = Field(default=500, ge=1, le=10000)
    prefer_short: bool = True
    one_question_at_a_time: bool = True
    no_unsolicited_menu: bool = True
    language: Literal["zh", "en", "auto"] = "auto"
    emoji_level: Literal["none", "light", "full"] = "light"


class ContextModule(BaseModel):
    """13 模块开关 — 动态上下文注入。"""

    time_awareness: bool = True
    turn_stage_awareness: bool = True
    keyword_triggers: bool = True
    emotion_vector: bool = True
    memory_injection: bool = True
    lorebook_injection: bool = True
    variance: bool = True
    fixed_signals: bool = True
    static_rules: bool = True
    translate: bool = True
    debug: bool = False


class KeywordRule(BaseModel):
    """关键词触发规则。"""

    keywords: list[str]
    action: Literal["inject_prompt", "switch_emotion", "escalate"]
    payload: str = ""
    priority: int = 0


class PersonaConfig(BaseModel):
    """Persona 总配置 — JSON/YAML 直接加载。"""

    version: Literal["1"] = "1"
    identity: Identity = Field(default_factory=Identity)
    behavior: BehaviorRules = Field(default_factory=BehaviorRules)
    modules: ContextModule = Field(default_factory=ContextModule)
    custom_prompts: dict[str, str] = Field(default_factory=dict)
    keyword_rules: list[KeywordRule] = Field(default_factory=list)

    @field_validator("version", mode="before")
    @classmethod
    def _coerce_version(cls, v: Any) -> Any:
        """YAML 里 version: 1（未加引号）是 int，直接拒绝会吓到正常用户。

        （AUDIT-2026-10-10 第四章 4.6：`Literal["1"]` 拒收整数 1）
        """
        return str(v) if isinstance(v, int) else v


__all__ = [
    "EmotionLevel",
    "TurnStage",
    "Identity",
    "BehaviorRules",
    "ContextModule",
    "PersonaConfig",
    "KeywordRule",
]
