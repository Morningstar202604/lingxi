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

import os
import re
import shlex
import unicodedata
from enum import StrEnum
from typing import Any, Literal

from lingxi.core import get_logger

log = get_logger(__name__)


# ── 注入检测 ──


def normalize_for_detection(text: str) -> str:
    """生成用于匹配的规范化副本（原文透传给 LLM，本函数不改原串）。

    对付 Unicode 等价字符绕过：全角/兼容字符（ＮＦＫＣ 归一）+ 大小写 + 空白折叠。

    注意：这里**不删除**分隔符。"ignore previous" 的词间空格是有意义的，
    删掉会得到 "ignoreprevious" 从而破坏 \\b 词边界（这正是旧实现漏判的原因）。
    分隔符插入型绕过（"I G N O R E"）改由正则侧的 _separator_tolerant 容忍。
    """
    normalized = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s\u3000]+", " ", normalized)


# 字母串（长度>=2）→ 展开为允许插入分隔符的形式：ignore → i[\s\-_]*g[\s\-_]*n...
# 这样 "I G N O R E" / "i-g-n-o-r-e" / "i\ng\nnore" 都会命中同一模式，
# 而词间空格仍由 \\b 正确定位。
_LETTER_RUN = re.compile(r"[a-z]{2,}")


def _separator_tolerant(pattern: str) -> str:
    """把正则中的字母串展开为容忍分隔符插入的形式。"""
    return _LETTER_RUN.sub(lambda m: r"[\s\-_]*".join(m.group(0)), pattern)



# 明确注入模式：指令性要求模型覆盖/遗忘/篡改系统设定 → 直接拦截
# 注：匹配统一跑在 normalize_for_detection 的规范化副本上，故此处
# 无需再写 (?i)（规范化已 lower）且可用宽松的 \s*（分隔符已被折叠）。
_BLOCK_PATTERNS = [
    # 覆盖 ignore/disregard 后的任意修饰语 + previous/above/all/earlier 指令
    r"(?:ignore|disregard|forget|override)\b[^.!?\n]{0,24}?\b(?:previous|prior|earlier|above|all|original|initial)\b[^.!?\n]{0,16}?\b(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions)\b",
    r"\bforget\s+(?:everything|all)\b",
    r"\bdisregard\s+(?:your|the|all|any)\s+(?:instruction|instructions|rule|rules|guideline|guidelines|persona|settings|configuration|prompt)\b",
    r"\bnew\s+persona\s*[:：]",
    r"\bdeveloper\s+mode\b",
    r"\bjailbreak\b",
    r"\bpretend\s+(?:to\s+be|you\s+are|that\s+you)\b",
    r"\byou\s+are\s+now\s+(?:a|an|the)\s+(?!helpful|friendly|assistant\b)",
    r"\bsystem\s*[:：]\s*you\s+are\b",
    # 中文变体：忽略/无视/忘记 + 之前/上面/以上/所有 + 指令/设定/规则/提示词
    r"(?:忽略|无视|忘记|覆盖|不要理会)(?:掉)?[^。！？\n]{0,6}?(?:之前|前面|上面|以上|先前|所有|原先|原来)[^。！？\n]{0,4}?(?:指令|命令|设定|规则|配置|提示词|人设)",
    r"假装(?:你是|你叫|你现在是)",
    r"(?:你的|你原来的|你之前的)(?:身份|角色|设定|人设)(?:被)?(?:改变|替换|重置)",
    r"开发者模式",
]

# 可疑模式：可能是角色扮演/测试等合法场景，需调用方自行决策
_SUSPECT_PATTERNS = [
    r"(?i)你(现在|现在起|来)(是|扮演|变成|当)",
    r"(?i)(assume|act)\s+(the\s+role|as)",
]

_COMPILED_BLOCK = [
    re.compile(_separator_tolerant(p)) for p in _BLOCK_PATTERNS
]
_COMPILED_SUSPECT = [
    re.compile(_separator_tolerant(p)) for p in _SUSPECT_PATTERNS
]


class InjectionVerdict(StrEnum):
    """注入检测结果。"""

    CLEAN = "clean"
    SUSPECT = "suspect"  # 可疑但可能是正常用语（调用方决定）
    BLOCK = "block"  # 明确注入尝试（硬拦截）


def detect_injection(text: str) -> tuple[InjectionVerdict, str | None]:
    """检测文本中是否包含 prompt injection 特征。

    二阶段判断：
    1. 命中 BLOCK 模式（指令性覆盖/遗忘/注入）→ 硬拦截
    2. 命中 SUSPECT 模式（假设性角色扮演）→ 由调用方决定是否放行
    3. 全部未命中 → CLEAN

    Returns:
        (verdict, matched_pattern_or_none)
    """
    haystack = normalize_for_detection(text)
    for pattern in _COMPILED_BLOCK:
        if pattern.search(haystack):
            return InjectionVerdict.BLOCK, pattern.pattern
    for pattern in _COMPILED_SUSPECT:
        if pattern.search(haystack):
            return InjectionVerdict.SUSPECT, pattern.pattern
    return InjectionVerdict.CLEAN, None


# ── 权限分级 ──

PermissionLevel = Literal[0, 1, 2, 3, 4]

PERM_AUTO: PermissionLevel = 0        # 只读操作
PERM_LOW_RISK: PermissionLevel = 1    # 发消息/建工单
PERM_MEDIUM_RISK: PermissionLevel = 2  # 写文件/调外部 API
PERM_HIGH_RISK: PermissionLevel = 3    # Shell/删文件/提权
PERM_FORBIDDEN: PermissionLevel = 4    # 访问凭据/改配置


class PermissionDeniedError(Exception):
    """权限不足异常。"""

    def __init__(self, required: int, actual: int, action: str) -> None:
        super().__init__(
            f"Permission denied: action '{action}' requires level {required}, "
            f"current level is {actual}"
        )
        self.required = required
        self.actual = actual
        self.action = action


def check_permission(
    action: str,
    current_level: int,
    required_level: int,
) -> None:
    """检查权限是否足够。不够则抛出 PermissionDeniedError。"""
    if current_level < required_level:
        raise PermissionDeniedError(
            required=required_level,
            actual=current_level,
            action=action,
        )


def resolve_permission_level(
    user_id: str = "",
    chat_id: str = "",
    default_level: int | None = None,
) -> PermissionLevel:
    """按 user/chat 解析本次会话的权限等级。

    🔗 工程逻辑
        权限来源从"进程级全局常量"改为"每次会话解析"：调用方在处理 IM 事件时
        把 user_id/chat_id 传进来，命中 SecurityConfig.permission_rules 即按规则
        给到等级（可提权也可降权），未命中则用基线等级。

        匹配优先级：user_id > chat_id > "@all" > 基线。
        规则值越界（不在 L0-L4）时忽略该条并告警，避免配置笔误把人提到红线。
    """
    from lingxi.config import get_settings

    sec = get_settings().security
    fallback = int(
        default_level if default_level is not None else sec.default_permission_level
    )
    if not sec.permission_rules:
        return fallback  # type: ignore[return-value]

    rules: dict[str, int] = {}
    for key in (user_id, chat_id, "@all"):
        if not key:
            continue
        if key in sec.permission_rules:
            level = sec.permission_rules[key]
            if 0 <= level <= 4:
                rules = {key: level}
                break
            log.warning("permission_rule_out_of_range", subject=key, level=level)

    if not rules:
        return fallback  # type: ignore[return-value]

    subject, level = next(iter(rules.items()))
    if level != fallback:
        log.info("permission_level_resolved", subject=subject, level=level)
    return level  # type: ignore[return-value]


# ── shell 分级词表（fail-closed 白名单，而非黑名单）──

# 解释器：出现即视为任意代码执行
_INTERPRETERS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh",
    "python", "python2", "python3", "py", "node", "perl", "ruby",
    "php", "deno", "bun", "osascript", "powershell", "pwsh", "cmd",
})

# 只读白名单：仅这些程序 + 无写标记时判 PERM_AUTO。
#
# 入选标准（三条全满足才放进来）：
#   1. 自身没有"执行外部程序"的能力（无 -exec / system() / e 标志 / !! 逃逸）
#   2. 自身不写文件（重定向另由 _WRITE_MARKERS 拦截）
#   3. 不主动把敏感数据吐出来
#
# 从旧白名单里**移除**的程序及原因（这些此前都判 PERM_AUTO，即默认放行）：
#   awk  → BEGIN{system("...")} 任意代码执行
#   sed  → GNU sed 的 s///e 标志执行命令；-i 原地写文件
#   jq   → 可读任意文件路径（凭据外泄）且 -f 执行筛选脚本
#   yq   → 同 jq
#   find → -exec / -delete / | xargs rm
#   git  → push/reset/clean/checkout 全是写操作；改为按子命令分级（见下）
#   env / printenv → 把 LINGXI_LLM__API_KEY 等环境变量整体打印出来
#   less / more    → less 的 !cmd、more 的 ! 都能起 shell
_READONLY_ALLOWLIST = frozenset({
    "ls", "dir", "pwd", "cat", "type", "head", "tail",
    "wc", "stat", "file", "fd", "tree", "which", "where", "whoami",
    "id", "date", "uname", "hostname", "uptime",
    "echo", "grep", "egrep", "fgrep", "rg", "ag", "diff", "cmp",
    "sort", "uniq", "cut", "tr",
    "true", "false", "sleep", "df", "du", "free", "ps", "top", "ipconfig",
    "ifconfig", "netstat", "ping", "traceroute", "nslookup",
})

# 仅"读"的 git 子命令放行；其余 git 子命令一律高风险（可写工作区/推远端/改历史）
_GIT_READONLY_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "blame", "ls-files", "rev-parse", "describe",
})

# 自身不执行命令、但能把参数变成"要执行的程序"的包装器。
# 只放"任意位置出现都危险"的：这些按 token 命中即高风险。
# 刻意不含 tee / install / patch —— 它们作为**子命令词**太常见
# （`pip install requests` 里的 install 只是动词，不是 coreutils 的 install(1)），
# 放进这里会把 pip install 误判成高风险；那三个由第 7 步的正则按中危处理。
_COMMAND_EXEC_BINARIES = frozenset({
    "xargs", "sudo", "doas", "nohup", "eval", "exec", "env",
    "watch", "screen", "tmux", "strace", "gdb",
})

# 破坏性二进制：即使名字看着无害也提级
_DESTRUCTIVE_BINARIES = frozenset({
    "rm", "rmdir", "del", "erase", "mv", "move", "dd", "mkfs", "fdisk",
    "format", "chmod", "chown", "chgrp", "kill", "killall", "pkill",
    "shutdown", "reboot", "halt", "init", "service", "systemctl",
})

# 网络 / 包管理：显式中危
_NETWORK_BINARIES = frozenset({
    "curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync",
    "pip", "pip3", "npm", "yarn", "pnpm", "apt", "apt-get", "yum", "dnf",
    "brew", "gem", "cargo", "go", "docker", "kubectl",
})

# 写操作痕迹（出现在 argv 中即判定有写意图）
_WRITE_MARKERS = frozenset({">>", ">", "<", "2>", "&>"})

# shell 元字符：出现即意味着"后面还有一条命令"或"这里要展开执行"，
# 单看 argv[0] 的白名单判定因此完全失效（实测 `ls; rm -rf /` 曾判 PERM_AUTO）。
_CHAIN_MARKERS = (";", "&&", "||", "\n", "`", "$(")


def _basename_token(token: str) -> str:
    """取程序名（去路径前缀与 .exe 后缀）。

    必须用 removesuffix 而非 rstrip —— rstrip 按字符集合剥离，
    "date".rstrip(".exe") == "dat"，会把只读白名单命令悄悄踢出白名单。
    """
    return os.path.basename(token).lower().removesuffix(".exe")


# 凭据 / 密钥文件：读取即红线（BLUEPRINT L4「访问凭据」）
_SENSITIVE_PATHS = (
    "/etc/shadow", "/etc/gshadow", "/etc/sudoers",
    "/.ssh/", "/id_rsa", "/id_ed25519", "/.aws/credentials",
    "/.kube/config", "/.docker/config.json",
    "/.netrc", "/.git-credentials", "/.npmrc", "/.pypirc",
    "/.env", "/secrets", "/credentials",
)
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")


def _touches_sensitive_path(cmd_lower: str) -> bool:
    """命令是否触及凭据 / 密钥文件。

    fail-closed 取向：宁可多判。只在 argv 里出现敏感路径或敏感后缀即命中。
    """
    for marker in _SENSITIVE_PATHS:
        if marker in cmd_lower:
            return True
    try:
        argv = shlex.split(cmd_lower)
    except ValueError:
        argv = cmd_lower.split()
    return any(token.endswith(_SENSITIVE_SUFFIXES) for token in argv[1:])


def classify_shell_command(command: str, blocked_list: list[str]) -> PermissionLevel:
    """对 Shell 命令做分级分类（fail-closed）。

    🔗 设计依据（外部范式，非自创）
        Microsoft Defender 2026-05 agent 工具安全研究指出：
        "blocklists in dynamic languages are inherently fragile"，
        并建议用 AST/node allowlist 而非 denylist。命令同理——
        **只有真正无副作用的只读命令才是 PERM_AUTO，其余一律提级**。

    🔗 工程逻辑（判定顺序即优先级，越靠前越严格）
        1. 黑名单命中 → PERM_FORBIDDEN（硬红线，永不放行）
        2. shlex 解析失败（引号不闭合）→ PERM_HIGH_RISK
        3. shell 链式/展开元字符（; && || 换行 反引号 $( ）→ PERM_HIGH_RISK
        4. 解释器（含作为参数出现的解释器）→ PERM_HIGH_RISK
        5. 破坏性程序或命令包装器出现在**任意 token** → PERM_HIGH_RISK
        6. 管道：逐段取 argv[0]，全段都在只读白名单内才算 AUTO，否则 HIGH
        7. 写标记（重定向）→ PERM_MEDIUM_RISK
        8. git 按子命令分级（status/log/diff 只读放行，push/reset 等高风险）
        9. 白名单程序 → PERM_AUTO；网络/包管理 → PERM_MEDIUM_RISK
        10. 兜底 → PERM_MEDIUM_RISK

    🤖 AI 避坑
        - 兜底绝不能是 PERM_AUTO，也**不能是 PERM_LOW_RISK**：引擎默认权限等级是
          1（__main__.py:_PERMISSION_LEVEL），而 PERM_LOW_RISK 恰好等于 1，
          check_permission 判 current >= required 会通过 —— 于是"兜底 LOW_RISK"
          和"兜底 AUTO"在默认配置下**行为完全相同**，等于没兜住。
          旧实现正是栽在这里：`ls; rm -rf /` 因为 argv[0]=ls 命中白名单直接放行。
        - 白名单只放"读"，不放"读+写"，也不放任何能起子进程或读凭据的程序。
        - 本函数倾向**过度提级**而非漏判：误伤的表现只是多要一次人工确认，
          漏判的表现是任意代码执行。例如 `grep -E "a|b"` 会因含 `|` 被判高风险，
          这是可接受的代价。
        - 新增白名单命令前先确认它没有任何副作用、不能执行外部程序、不打印凭据。
    """
    cmd_lower = command.lower().strip()

    # ── 1. 黑名单（用户可配置，如 rm -rf /）──
    # 不能用裸子串匹配：`rm -rf /` 会误伤 `rm -rf /tmp/x`，
    # 而 `rm  -rf  /`（双空格）又绕不过去。改为按 argv[0] 的**程序名**
    # 精确比对；fork bomb 这类"整条命令"模式走规范化后整体比对。
    normalized = re.sub(r"\s+", " ", cmd_lower)
    try:
        blocked_argv = shlex.split(normalized)
    except ValueError:
        blocked_argv = normalized.split()
    blocked_prog = _basename_token(blocked_argv[0]) if blocked_argv else ""

    for blocked in blocked_list:
        target = re.sub(r"\s+", " ", blocked.lower().strip())
        if not target:
            continue
        # 整条命令模式（如 fork bomb ":(){ :|:& };:"）
        if target == normalized:
            return PERM_FORBIDDEN
        try:
            target_argv = shlex.split(target)
        except ValueError:
            target_argv = target.split()
        if not target_argv:
            continue
        if len(target_argv) == 1:
            # 单个程序名（mkfs / dd）：比对 argv[0]，覆盖 /sbin/mkfs 绝对路径，
            # 并按「程序名 + .后缀」匹配，使黑名单里的 "mkfs" 能拦住 mkfs.ext4/xfs
            target_prog = _basename_token(target_argv[0])
            if blocked_prog == target_prog or blocked_prog.startswith(f"{target_prog}."):
                return PERM_FORBIDDEN
        elif blocked_argv[: len(target_argv)] == target_argv:
            # argv **精确前缀**：黑名单写 "rm -rf" 拦所有 rm -rf，
            # 但不会像子串匹配那样把 `rm -rf /tmp/x` 误判成 `rm -rf /`
            return PERM_FORBIDDEN

    # ── 1b. 凭据文件 ──
    # `cat /etc/shadow` 的 argv[0] 命中只读白名单，整条被判 L0；
    # 但读凭据是 BLUEPRINT 明确的 L4 红线（"访问凭据"），必须单独兜住。
    if _touches_sensitive_path(cmd_lower):
        log.warning("sensitive_path_access", command=command[:80])
        return PERM_FORBIDDEN

    # ── 2. 解析 argv ──
    try:
        argv = shlex.split(command)
    except ValueError:
        # 引号不闭合 = 畸形输入，fail-closed
        log.warning("shell_parse_failed", command=command[:80])
        return PERM_HIGH_RISK

    if not argv:
        return PERM_MEDIUM_RISK

    # ── 3. 链式执行元字符：出现即说明"后面还有命令"或"这里要做命令替换" ──
    # 必须在按 argv[0] 查白名单**之前**判，否则 `ls; rm -rf /` 会被当成 ls。
    if any(marker in cmd_lower for marker in _CHAIN_MARKERS):
        log.warning("shell_chain_metachar_detected", command=command[:80])
        return PERM_HIGH_RISK

    def _exe(token: str) -> str:
        return _basename_token(token)

    prog = _exe(argv[0])
    tokens = {_exe(t) for t in argv[1:]}
    raw_tokens = {t.lower() for t in argv[1:]}
    joined = " ".join(argv).lower()

    # ── 4. 解释器 / 嵌套 shell → 等价任意代码执行 ──
    if prog in _INTERPRETERS or _INTERPRETERS & tokens:
        return PERM_HIGH_RISK

    # ── 5. 破坏性程序与命令包装器：任意 token 命中即提级 ──
    # 旧实现只检查 prog，于是 `ls | xargs rm -rf` 里的 rm 被漏掉。
    if prog in _DESTRUCTIVE_BINARIES or _DESTRUCTIVE_BINARIES & tokens:
        return PERM_HIGH_RISK
    if prog in _COMMAND_EXEC_BINARIES or _COMMAND_EXEC_BINARIES & tokens:
        return PERM_HIGH_RISK

    # ── 6. 管道：逐段都必须落在只读白名单里 ──
    if "|" in joined:
        segments = [seg.strip() for seg in joined.split("|") if seg.strip()]
        for seg in segments:
            seg_prog = _exe(shlex.split(seg)[0])
            if seg_prog not in _READONLY_ALLOWLIST:
                log.warning("shell_pipeline_segment_denied", segment=seg[:60])
                return PERM_HIGH_RISK

    # ── 7. 写操作标记 ──
    if _WRITE_MARKERS & raw_tokens or re.search(r"(^|\s)\d*>>?(\s|$)", joined):
        return PERM_MEDIUM_RISK
    if re.search(r"\b(?:sed\s+-i|tee|install|patch)\b", joined):
        return PERM_MEDIUM_RISK

    # ── 8. git：按子命令分级 ──
    if prog == "git":
        # 按参数顺序取第一个非 flag token 作为子命令（`git log --oneline` → log）。
        # 不能用 sorted()：那会让 `git push origin master` 取到 master、
        # 让 `git diff HEAD~1` 取到 HEAD~1，判定结果随参数顺序漂移。
        sub = next(
            (t for t in (a.lower() for a in argv[1:]) if not t.startswith("-")),
            "",
        )
        if sub in _GIT_READONLY_SUBCOMMANDS:
            return PERM_AUTO
        log.warning("shell_git_write_subcommand", subcommand=sub, command=command[:80])
        return PERM_HIGH_RISK

    # ── 9. 只读白名单 ──
    if prog in _READONLY_ALLOWLIST:
        return PERM_AUTO

    # ── 10. 网络 / 包管理：显式中危 ──
    if prog in _NETWORK_BINARIES or _NETWORK_BINARIES & tokens:
        return PERM_MEDIUM_RISK

    # ── 11. fail-closed 兜底：必须高于默认权限等级 1 ──
    return PERM_MEDIUM_RISK


# ── 角色篡改检测 ──

def detect_role_override(messages: list[dict[str, Any]]) -> bool:
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
    "PermissionDeniedError",
    "check_permission",
    "resolve_permission_level",
    "classify_shell_command",
    "detect_role_override",
    "PERM_AUTO",
    "PERM_LOW_RISK",
    "PERM_MEDIUM_RISK",
    "PERM_HIGH_RISK",
    "PERM_FORBIDDEN",
]
