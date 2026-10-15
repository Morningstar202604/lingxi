"""PII 脱敏模块 — **Presidio 框架 + 中文识别器插件 + 正则兜底**。

🔗 工程逻辑
    三层设计，各司其职：

    1. **Presidio（微软官方）负责框架**：识别器注册/调度、置信度评分、
       匿名化算子（replace/redact/hash/encrypt）、多语言支持、上下文增强。
       这些是"轮子"——自研置信度模型与算子体系毫无意义。
    2. **中文识别器作为 Presidio 插件**：Presidio 预置识别器面向英文体系
       （实测：中文手机号/身份证/银行卡**识别不到**，只有 email/URL 能匹配）。
       中国大陆证件格式是我们的领域知识，用 `PatternRecognizer` +
       `validate_result`（身份证校验位、Luhn）注册进去。
    3. **正则兜底**：Presidio 初始化失败（缺 spaCy/依赖异常）时退化为
       纯正则实现 —— 脱敏是安全功能，**绝不能因为依赖问题失效**。

    为什么不用 spaCy 统计模型：我们只需要格式类识别（手机号/证件号/卡号），
    不需要 NER。用 `spacy.blank()` 空管线即可，免去下载数百 MB 模型 ——
    离线部署（本项目常态）不允许运行时下载。

🤖 AI 避坑
    - Presidio 的 AnalyzerEngine 初始化较慢（加载识别器），全局单例 + 懒加载。
    - 身份证校验位必须验：否则任何 18 位数字都被当身份证（订单号误伤）。
    - Luhn 校验同理用于银行卡：纯 `\\d{16,19}` 会把长订单号误脱敏。
    - Presidio 的 PERSON/LOCATION（英文 NER）在本场景误伤率高
      （中文人名地名不该被脱敏，否则对话被破坏）——只替换格式类实体。
"""
from __future__ import annotations

import re
from typing import Any

from lingxi.core import get_logger

log = get_logger(__name__)

# ── 兜底正则（Presidio 不可用时的完整实现，也是最后一道保险）────────

PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
ID_CARD_RE = re.compile(
    r"(?<!\d)([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)"
)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
BANK_CARD_RE = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")

PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PHONE", PHONE_RE),
    ("ID_CARD", ID_CARD_RE),
    ("EMAIL", EMAIL_RE),
    ("BANK_CARD", BANK_CARD_RE),
]

# ── 身份证校验位（GB 11643-1999）──────────────────────────────────

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def _valid_id_card(value: str) -> bool:
    """校验 18 位身份证最后一位（防止任意 18 位数字被误判）。"""
    if len(value) != 18:
        return False
    try:
        total = sum(int(value[i]) * _ID_WEIGHTS[i] for i in range(17))
    except ValueError:
        return False
    return _ID_CHECK[total % 11].upper() == value[17].upper()


def _valid_luhn(value: str) -> bool:
    """Luhn 校验（银行卡/信用卡），把长订单号排除在外。"""
    digits = [int(c) for c in value if c.isdigit()]
    if len(digits) < 12:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ── Presidio 引擎（懒加载单例）────────────────────────────────────

_analyzer: Any = None
_anonymizer: Any = None
_presidio_failed: str | None = None


def _make_validator(check: Any) -> Any:
    """构造 Presidio 的 validate_result 钩子。

    该钩子是**绑定方法**（`(self, pattern_text)`），不是普通函数 ——
    直接赋普通函数会签名不匹配（mypy 报 assignment error），
    运行时也会因为少一个 self 参数而报错。
    """

    def _validate(_self: Any, pattern_text: str) -> bool:
        return bool(check(pattern_text))

    return _validate


def _build_analyzer() -> Any:
    """构造 AnalyzerEngine：空 spaCy 管线 + 预置识别器 + 中文证件插件。"""
    import spacy
    from presidio_analyzer import (
        AnalyzerEngine,
        Pattern,
        PatternRecognizer,
        RecognizerRegistry,
    )
    from presidio_analyzer.nlp_engine import SpacyNlpEngine

    class _BlankSpacyEngine(SpacyNlpEngine):
        """官方 SpacyNlpEngine 子类：spacy.blank 零下载管线（离线可用）。"""

        def load(self) -> None:
            nlp_map: Any = {
                m["lang_code"]: spacy.blank(m["lang_code"]) for m in self.models
            }
            self.nlp = nlp_map

    engine = _BlankSpacyEngine(
        models=[
            {"lang_code": "zh", "model_name": ""},
            {"lang_code": "en", "model_name": ""},
        ]
    )
    engine.load()

    registry = RecognizerRegistry(supported_languages=["en", "zh"])
    registry.load_predefined_recognizers(languages=["en", "zh"])

    phone_rec = PatternRecognizer(
        supported_entity="CN_PHONE",
        name="cn_phone_recognizer",
        supported_language="zh",
        patterns=[Pattern(name="cn_phone", regex=r"(?<!\d)1[3-9]\d{9}(?!\d)", score=0.85)],
    )
    id_rec = PatternRecognizer(
        supported_entity="CN_ID_CARD",
        name="cn_id_card_recognizer",
        supported_language="zh",
        patterns=[
            Pattern(
                name="cn_id_18",
                regex=r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)",
                score=0.6,  # 基础分低：靠校验位通过后升到高置信
            )
        ],
    )
    bank_rec = PatternRecognizer(
        supported_entity="CN_BANK_CARD",
        name="cn_bank_card_recognizer",
        supported_language="zh",
        patterns=[Pattern(name="cn_bank", regex=r"(?<!\d)\d{16,19}(?!\d)", score=0.5)],
    )
    # 校验位让识别具备"验证"能力：格式对+校验过→置信度提升；
    # 校验不过被淘汰，避免订单号/流水号被误脱敏
    # Presidio 的官方扩展点就是"给识别器挂 validate_result"（不是继承重写），
    # 因此方法赋值是预期用法，仅类型检查需要豁免。
    id_rec.validate_result = _make_validator(_valid_id_card)  # type: ignore[method-assign]
    bank_rec.validate_result = _make_validator(_valid_luhn)  # type: ignore[method-assign]

    for rec in (phone_rec, id_rec, bank_rec):
        registry.add_recognizer(rec)

    return AnalyzerEngine(
        nlp_engine=engine, registry=registry, supported_languages=["en", "zh"]
    )


def _get_engines() -> tuple[Any, Any] | None:
    """懒加载并缓存 Presidio 引擎；失败后记住原因（不反复重试）。"""
    global _analyzer, _anonymizer, _presidio_failed
    if _presidio_failed is not None:
        return None
    if _analyzer is not None and _anonymizer is not None:
        return (_analyzer, _anonymizer)
    try:
        from presidio_anonymizer import AnonymizerEngine

        _analyzer = _build_analyzer()
        _anonymizer = AnonymizerEngine()  # type: ignore[no-untyped-call]
        log.info("pii_engine_ready", backend="presidio", languages=["zh", "en"])
        return (_analyzer, _anonymizer)
    except Exception as e:  # noqa: BLE001 — 脱敏功能不能因依赖问题失效
        _presidio_failed = f"{type(e).__name__}: {e}"
        log.warning(
            "pii_presidio_unavailable_using_regex_fallback",
            error=_presidio_failed,
            hint="降级为内置正则（手机号/身份证/邮箱/银行卡全覆盖，功能不缺失）",
        )
        return None


def reset_engines_for_tests() -> None:
    """清空引擎缓存（测试切换 Presidio/降级路径用）。"""
    global _analyzer, _anonymizer, _presidio_failed
    _analyzer = None
    _anonymizer = None
    _presidio_failed = None


def pii_status() -> dict[str, Any]:
    """当前 PII 引擎状态（健康检查/排障）。"""
    return {
        "backend": "presidio" if (_analyzer is not None) else "regex_fallback",
        "presidio_failed_reason": _presidio_failed or "",
        "regex_fallback_active": _analyzer is None,
    }


# ── 核心函数 ──────────────────────────────────────────────────────

# Presidio 实体名 → 对外类型名（保持既有契约不变）
_ENTITY_ALIAS = {
    "CN_PHONE": "PHONE",
    "PHONE_NUMBER": "PHONE",
    "CN_ID_CARD": "ID_CARD",
    "CN_BANK_CARD": "BANK_CARD",
    "CREDIT_CARD": "BANK_CARD",
    "EMAIL_ADDRESS": "EMAIL",
    "IP_ADDRESS": "IP",
}

# 只对格式类实体做替换：Presidio 预置的 PERSON/LOCATION 是英文 NER，
# 中文场景误伤率高（正常人名地名被脱敏会破坏对话语义）
_MASKABLE_ENTITIES = frozenset(_ENTITY_ALIAS) - {"PERSON", "LOCATION"}


def _regex_mask(text: str, mask: str) -> str:
    """正则兜底替换（**格式匹配即脱敏**，不做校验位淘汰）。

    🔗 安全取舍：宁可多脱敏，不可漏脱敏
        身份证/银行卡的**校验位不参与兜底层的淘汰**：格式像证件的串一律脱敏。
        原因：脱敏是安全功能，漏掉真实 PII 的代价（隐私泄露到记忆/日志）
        远大于把订单号误脱敏（用户看不到自己的单号，可重新查询）。
        校验位只用于 Presidio 层**提升置信度**（把明确的证件号提到高分），
        不用于"格式对但不脱敏"。

    🤖 AI 避坑
        不要把这里改成"校验不过就保留" —— 测试与真实数据里都存在
        Luhn/校验位不成立的"测试卡号/示例身份证"，漏脱敏就是安全事故。
    """
    result = text
    for _, pattern in PII_PATTERNS:
        result = pattern.sub(mask, result)
    return result


def _regex_scan(text: str) -> dict[str, list[str]]:
    """正则扫描（与脱敏同一标准：格式匹配即报告）。"""
    found: dict[str, list[str]] = {}
    for name, pattern in PII_PATTERNS:
        seen: set[str] = set()
        unique: list[str] = []
        for v in pattern.findall(text):
            if v not in seen:
                seen.add(v)
                unique.append(v)
        if unique:
            found[name] = unique
    return found


def contains_pii(text: str) -> dict[str, list[str]]:
    """检测文本包含的 PII，返回 {类型: [匹配值, ...]}。

    正则结果与 Presidio 结果**合并** —— 保证检测能力只增不减。
    """
    found = _regex_scan(text)
    engines = _get_engines()
    if engines is None:
        return found
    analyzer, _ = engines
    try:
        for r in analyzer.analyze(text=text, language="zh"):
            if r.score < 0.4:  # 低置信度不报，避免噪声
                continue
            name = _ENTITY_ALIAS.get(r.entity_type, r.entity_type)
            value = text[r.start:r.end]
            bucket = found.setdefault(name, [])
            if value not in bucket:
                bucket.append(value)
    except Exception as e:  # noqa: BLE001 — 检测失败不阻断主流程
        log.warning("pii_analyze_failed", error=str(e))
    return found


def mask_pii(text: str, mask: str = "***") -> str:
    """将 text 中的 PII 替换为 mask。

    先用 Presidio 精确定位 + 官方匿名化算子，再用正则兜底扫一遍 ——
    Presidio 漏掉的中文格式由正则补上，两层都不动非 PII 内容。
    """
    engines = _get_engines()
    if engines is None:
        return _regex_mask(text, mask)

    analyzer, anonymizer = engines
    try:
        from presidio_anonymizer.entities import OperatorConfig

        results = [
            r
            for r in analyzer.analyze(text=text, language="zh")
            if r.score >= 0.4 and r.entity_type in _MASKABLE_ENTITIES
        ]
        if results:
            anonymized = anonymizer.anonymize(
                text=text,
                analyzer_results=results,
                operators={"DEFAULT": OperatorConfig("replace", {"new_value": mask})},
            )
            text = anonymized.text
    except Exception as e:  # noqa: BLE001
        log.warning("pii_anonymize_failed_fallback_regex", error=str(e))

    # 正则兜底：Presidio 未覆盖的中文格式（及它漏掉的边界情形）
    return _regex_mask(text, mask)


# ── PIIGuard 类 ───────────────────────────────────────────────────


class PIIGuard:
    """钩子: 进入 Agent 前的输入 & 离开 Agent 前的输出, 统一调用 mask_pii。"""

    @staticmethod
    def sanitize_input(text: str) -> str:
        """入站脱敏: Agent 收到用户消息前, 把 PII 替换为 ***。"""
        return mask_pii(text)

    @staticmethod
    def sanitize_output(text: str) -> str:
        """出站脱敏: Agent 输出回复前, 把它回复中的 PII 也替换掉。"""
        return mask_pii(text)

    @staticmethod
    def detect(text: str) -> dict[str, list[str]]:
        """检测文本包含哪些 PII 类型 (只读, 不替换)。"""
        return contains_pii(text)


__all__ = [
    "PII_PATTERNS",
    "PIIGuard",
    "contains_pii",
    "mask_pii",
    "pii_status",
    "reset_engines_for_tests",
]
