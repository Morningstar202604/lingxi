"""Node 1.x — IM 适配器基类 + CQ 码解析器（协议层抽象）。

🔗 工程逻辑
    每个 IM 渠道一个适配器，但共通的 CQ 码解析逻辑抽取到基类。
    CQ 码格式：[type,k1=v1,k2=v2]，参数值中 `,` `&` `[` `]` 需反转义。

    ⚠️ 注意
    NapCat 走 WebSocket + OneBot v11（主动连接 NapCat 服务，接收推送）。
    Gewechat 走 HTTP REST + Callback（Gewechat 服务主动回推到你暴露的 HTTP 接口）。
    两者通信模型完全不同，所以各有独立的适配器，**不要强行共用**。

🤖 AI 避坑
    - CQ 码反转义顺序：先 `&#44;`（逗号）→ `&#91;`（左括号）→ `&#93;`（右括号）→ `&amp;`（&符号）。若顺序错，`&amp;#44;` 会被双重反转义。
    - OneBot 的 `user_id` / `message_id` 都是 int，但 json 传输后可能被序列化为 str，做 `safe_int()` 兼容。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from lingxi.core import get_logger
from lingxi.core.protocol import IMEvent, MessageSegment

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = get_logger(__name__)

# CQ 码正则
_CQ_RE = re.compile(r"\[CQ:([^,\]]+(?:,[^,\]]*)*)\]")

# 反转义映射（按正确顺序）
_CQ_UNESCAPE = [
    ("&#44;", ","),
    ("&#91;", "["),
    ("&#93;", "]"),
    ("&amp;", "&"),
]

# 转义映射（发送时使用）
_CQ_ESCAPE = [
    ("&", "&amp;"),
    (",", "&#44;"),
    ("[", "&#91;"),
    ("]", "&#93;"),
]


def _cq_unescape(value: str) -> str:
    """CQ 码参数值反转义。"""
    for encoded, decoded in _CQ_UNESCAPE:
        value = value.replace(encoded, decoded)
    return value


def _cq_escape(value: str) -> str:
    """CQ 码参数值转义。"""
    for plain, escaped in _CQ_ESCAPE:
        value = value.replace(plain, escaped)
    return value


def safe_int(value: Any) -> int | None:
    """安全转为 int，失败返回 None（不崩溃）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_cq_code(raw: str) -> list[MessageSegment]:
    """解析 CQ 码字符串为消息段列表。

    支持混合文本 + CQ 码的消息，也支持纯 CQ 码消息。
    """
    segments: list[MessageSegment] = []
    last_end = 0

    for match in _CQ_RE.finditer(raw):
        # 两段 CQ 码之间的纯文本
        text_between = raw[last_end:match.start()]
        if text_between.strip():
            segments.append(MessageSegment(type="text", data={"text": text_between}))

        # 解析 CQ 码
        inner = match.group(1)
        parts = inner.split(",")
        seg_type = parts[0]
        data: dict[str, str] = {}
        for part in parts[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                data[k.strip()] = _cq_unescape(v.strip())
        segments.append(MessageSegment(type=seg_type, data=data))
        last_end = match.end()

    # 最后一段纯文本
    tail = raw[last_end:]
    if tail.strip():
        segments.append(MessageSegment(type="text", data={"text": tail}))

    return segments


def _parse_onebot_message(message: Any) -> list[MessageSegment]:
    """解析 OneBot v11 消息字段（可能是 str 或 list[dict]）。"""
    if isinstance(message, list):
        segments = []
        for seg in message:
            if isinstance(seg, dict):
                t = seg.get("type", "")
                d = seg.get("data", {})
                if isinstance(d, dict):
                    segments.append(MessageSegment(type=t, data=d))
            elif isinstance(seg, str) and seg:
                segments.extend(_parse_cq_code(seg))
        return segments
    elif isinstance(message, str):
        if message.startswith("[CQ:") or "[CQ:" in message:
            return _parse_cq_code(message)
        return [MessageSegment(type="text", data={"text": message})] if message.strip() else []
    return []


class IMAdapter(ABC):
    """IM 适配器抽象基类。"""

    def __init__(self, on_event: Callable[[IMEvent], Awaitable[None]] | None = None) -> None:
        self._on_event = on_event
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def _dispatch(self, event: IMEvent) -> None:
        if self._on_event is not None:
            try:
                await self._on_event(event)
            except Exception as e:
                log.error("event_handler_failed", error=str(e))

    @abstractmethod
    async def send(self, message: Any) -> bool:
        """发送消息到对端渠道。返回是否成功。"""

    @abstractmethod
    async def run(self) -> None:
        """启动适配器（阻塞直到 stop/失败）。"""

    @abstractmethod
    async def stop(self) -> None:
        """停止适配器。"""
