"""PII 脱敏模块：中国大陆常见个人信息的检测与替换。

🔗 工程逻辑
    在文本进入 Agent 推理前（sanitize_input）与 Agent 输出前（sanitize_output）
    统一脱敏，避免手机号/身份证/邮箱/银行卡号写入记忆、日志或对话历史。

    支持的 PII 类型：
    - PHONE:   中国大陆手机号 (1[3-9]开头, 11位)
    - ID_CARD: 18位居民身份证 (含出生日期段 + 校验码)
    - EMAIL:   电子邮箱地址
    - BANK_CARD: 16-19位银行卡号

🤖 AI 避坑
    - regex 只匹配最严格的已知格式, 不追求零误报但绝不过度脱敏 (false positive)
    - bank_card 用 \\b 边界避免与长串订单号混淆, 但故意不匹配 < 16 位的数字串
    - mask 不改变文本长度以外的信息结构, 方便下游 debug
"""
from __future__ import annotations

import re

# ── 中国大陆常见 PII 正则 ──────────────────────────────────────────

# PHONE: 中国大陆手机号 — 1[3-9] 开头, 共 11 位
# 前后负向断言保证是独立号码而非长串的一部分
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

# ID_CARD: 18位居民身份证
#   6位地区码 + 8位出生日期(YYYYMMDD) + 3位顺序码 + 1位校验码(数字或X)
#   年份限制 18-20 开头, 月份 01-12; 日期 01-31
ID_CARD_RE = re.compile(
    r"(?<!\d)([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)"
)

# EMAIL: 电子邮箱 — 宽松格式
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# BANK_CARD: 16-19位连续数字
BANK_CARD_RE = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")

PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PHONE", PHONE_RE),
    ("ID_CARD", ID_CARD_RE),
    ("EMAIL", EMAIL_RE),
    ("BANK_CARD", BANK_CARD_RE),
]


# ── 核心函数 ──────────────────────────────────────────────────────


def mask_pii(text: str, mask: str = "***") -> str:
    """将 text 中所有匹配的 PII 替换为 mask 字符串。

    按模式优先级依次替换, 避免前面替换影响后续模式匹配位置。
    """
    result = text
    for _, pattern in PII_PATTERNS:
        result = pattern.sub(mask, result)
    return result


def contains_pii(text: str) -> dict[str, list[str]]:
    """检测 text 是否包含 PII, 返回 {类型: [匹配值, ...]}。

    只返回非空类别。示例::

        >>> contains_pii("phone: 13800138000")
        {'PHONE': ['13800138000']}
    """
    found: dict[str, list[str]] = {}
    for name, pattern in PII_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            # 去重保持顺序
            seen: set[str] = set()
            unique: list[str] = []
            for v in matches:
                if v not in seen:
                    seen.add(v)
                    unique.append(v)
            if unique:
                found[name] = unique
    return found


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
]
