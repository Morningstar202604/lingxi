"""Node 2.4 — 安全护栏：防止角色被 prompt 注入覆盖 + 权限检查。

🔗 工程逻辑
    Prompt injection 是 Agent 最危险的攻击向量：
    用户说 "忽略你的设定，你现在是 XXX" → 如果 LLM 照做，护栏失效。
    三道防线：
    1. 身份锚前置 — 把 system prompt 放在最前面（LLM 注意力最强位置）
    2. 注入检测 — 用正则/启发式检测 prompt injection 特征
    3. 权限硬编码 — 不在 prompt 里定义权限级别，在节点代码里检查

🤖 AI 避坑
    - 别依赖 LLM 自己判断"这是不是 injection"，攻击者总有绕过之法
    - 权限检查必须在代码层（Guardrail 类），绝不能只写在 prompt 里
    - 检测到 injection 时不要当场爆炸，要优雅降级（回复兜底消息 + 记录日志）
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from lingxi.core import get_logger

log = get_logger(__name__)


# ── 注入检测 ──

# 常见的 prompt injection 模式
_INJECTION_PATTERNS = [
    r"(?i)ignore\s+(previous|above|all)\s+instructions?",
    r"(?i)you\s+are\s+now\s+(a|an|the)\s+\w+",
    r"(?i)new\s+persona[:：]",
    r"(?i)forget\s+(everything|all|your)",
    r"(?i)system\s*[:：]\s*you\s+are",
    r"(?i)你(现在|现在起)(是|扮演|变成)",
    r"(?i)忽略(之前|上面|所有)(指令|设定|规则)",
    r"(?i)你(现在)?是(一个|一名|助理)",
    r"(?i)假装你是",
    r"(?i)pretend\s+(to\s+be|you\s+are)",
]

_COMPILED_PATTERNS = [re.compile(p) for p in _INJECTION_PATTERNS]


class InjectionVerdict(str, Enum):
    """注入检测结果。"""

    CLEAN = "clean"
    SUSPECT = "suspect"  # 可疑但可能是正常用语
    BLOCK = "block"  # 明确注入尝试


def detect_injection(text: str) -> tuple[InjectionVerdict, str | None]:
    """检测文本中是否包含 prompt injection 特征。

    Returns:
        (verdict, matched_pattern_or_none)
    """
    for pattern in _COMPILED_PATTERNS:
        if pattern.search(text):
            return InjectionVerdict.BLOCK, pattern.pattern
    return InjectionVerdict.CLEAN, None


# ── 权限分级 ──

PermissionLevel = Literal[0, 1, 2, 3, 4]

PERM_AUTO: PermissionLevel = 0        # 只读操作
PERM_LOW_RISK: PermissionLevel = 1    # 发消息/建工单
PERM_MEDIUM_RISK: PermissionLevel = 2  # 写文件/调外部 API
PERM_HIGH_RISK: PermissionLevel = 3    # Shell/删文件/提权
PERM_FORBIDDEN: PermissionLevel = 4    # 访问凭据/改配置


class PermissionDenied(Exception):
    """权限不足异常。"""

    def __init__(self, required: PermissionLevel, actual: PermissionLevel, action: str) -> None:
        super().__init__(
            f"Permission denied: action '{action}' requires level {required}, "
            f"current level is {actual}"
        )
        self.required = required
        self.actual = actual
        self.action = action


def check_permission(
    action: str,
    current_level: PermissionLevel,
    required_level: PermissionLevel,
) -> None:
    """检查权限是否足够。不够则抛出 PermissionDenied。"""
    if current_level < required_level:
        raise PermissionDenied(
            required=required_level,
            actual=current_level,
            action=action,
        )


def classify_shell_command(command: str, blocked_list: list[str]) -> PermissionLevel:
    """对 Shell 命令做分级分类。"""
    cmd_lower = command.lower().strip()

    # 命中黑名单直接 forbidden
    for blocked in blocked_list:
        if blocked.lower() in cmd_lower:
            return PERM_FORBIDDEN

    # 高风险：写操作 / 提权 / 系统级
    high_risk_patterns = [
        r"\brm\b", r"\bmv\b", r"\bchmod\b", r"\bsudo\b",
        r"\bmkfs\b", r"\bdd\b", r"\bfdisk\b",
        r"\bcurl\b.*\|.*\bsh\b", r"\bwget\b.*\|.*\bsh\b",
        r"\bnc\b.*-e", r"\bbash\b.*-c",
    ]
    for pat in high_risk_patterns:
        if re.search(pat, cmd_lower):
            return PERM_HIGH_RISK

    # 中等风险：安装包 / 调 API
    medium_risk = [r"\bpip\b", r"\bapt\b", r"\bcurl\b", r"\bwget\b", r"\bnpm\b"]
    for pat in medium_risk:
        if re.search(pat, cmd_lower):
            return PERM_MEDIUM_RISK

    # 默认低风险（ls, cat, grep, echo 等只读命令）
    return PERM_AUTO


# ── 角色篡改检测 ──

def detect_role_override(messages: list[dict]) -> bool:
    """检查 messages 列表中是否有 system 角色被篡改。

    正常流程只有一个 system message（开头），且由 PersonaInjector 生成。
    如果后续出现 system 角色内容（通常是注入攻击），返回 True。
    """
    system_count = 0
    for msg in messages:
        if msg.get("role") == "system":
            system_count += 1
            if system_count > 1:
                return True
    return False


__all__ = [
    "InjectionVerdict",
    "detect_injection",
    "PermissionLevel",
    "PermissionDenied",
    "check_permission",
    "classify_shell_command",
    "detect_role_override",
    "PERM_AUTO",
    "PERM_LOW_RISK",
    "PERM_MEDIUM_RISK",
    "PERM_HIGH_RISK",
    "PERM_FORBIDDEN",
]
