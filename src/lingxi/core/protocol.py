"""OneBot v11 协议模型。涵盖消息段、事件、API 请求/响应。"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── 消息段 ──

class MessageSegment(BaseModel):
    """OneBot v11 消息段。

    每个段由 type + data 组成：
    - type: "text" | "image" | "at" | "face" | "reply" | ...
    - data: dict，结构因 type 而异
    """

    type: str
    data: dict[str, Any] = Field(default_factory=dict)

    def text_content(self) -> str:
        """如果是 text 段，返回其文本内容。"""
        if self.type == "text":
            return self.data.get("text", "")
        return ""

    def is_at(self, qq: str | None = None) -> bool:
        """是否是 @ 段，且可选地检查 @ 的是否为特定 qq。"""
        if self.type != "at":
            return False
        if qq is None:
            return True
        return str(self.data.get("qq", "")) == str(qq)

    def is_image(self) -> bool:
        return self.type == "image"

    @property
    def image_url(self) -> str | None:
        return self.data.get("url") if self.is_image() else None


# ── 统一 IM 事件（独立于 NapCat/Gewechat 的实现差异）──

class ChannelType(str, Enum):
    """IM 渠道类型。"""

    QQ_PRIVATE = "qq_private"
    QQ_GROUP = "qq_group"
    WECHAT_PRIVATE = "wechat_private"
    WECHAT_GROUP = "wechat_group"


class PostType(str, Enum):
    """OneBot 上报类型。"""

    MESSAGE = "message"
    NOTICE = "notice"
    REQUEST = "request"
    META_EVENT = "meta_event"


class MessageType(str, Enum):
    """消息类型。"""

    PRIVATE = "private"
    GROUP = "group"


class IMEvent(BaseModel):
    """统一 IM 事件 — adapter 层输出标准格式。

    adapter 负责把 NapCat/Gewechat 不同格式的原始事件转成这个。
    core 层只认这个，不需要知道底层是哪个 IM。
    """

    post_type: PostType
    message_type: MessageType | None = None
    channel: ChannelType
    self_id: str | None = None  # 机器人自身 ID
    user_id: str
    group_id: str | None = None
    message_id: str | None = None
    raw_message: str  # 原始消息字符串（含 CQ 码，可用于调试）
    segments: list[MessageSegment] = Field(default_factory=list)
    timestamp: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    # ── helpers ──

    @property
    def is_group(self) -> bool:
        return self.channel in (ChannelType.QQ_GROUP, ChannelType.WECHAT_GROUP)

    @property
    def is_private(self) -> bool:
        return not self.is_group

    @property
    def chat_id(self) -> str:
        """统一群/私聊 ID：群聊返回 group_id，私聊返回 user_id。"""
        return self.group_id or self.user_id

    @property
    def text(self) -> str:
        """拼接所有 text 段为纯文本。"""
        return "".join(s.text_content() for s in self.segments)

    @property
    def at_list(self) -> list[str]:
        """返回所有被 @ 的用户 ID 列表。"""
        return [s.data.get("qq", "") for s in self.segments if s.type == "at" and "qq" in s.data]

    @property
    def image_urls(self) -> list[str]:
        """返回所有图片段的 URL。"""
        return [u for s in self.segments if (u := s.image_url) is not None]


# ── 回复构建 ──

class OutboundMessage(BaseModel):
    """反向发送给 IM 的消息。"""

    channel: ChannelType
    target_id: str  # chat_id (group_id or user_id)
    segments: list[MessageSegment] = Field(default_factory=list)

    @classmethod
    def text(cls, channel: ChannelType, target_id: str, content: str) -> "OutboundMessage":
        return cls(
            channel=channel,
            target_id=target_id,
            segments=[MessageSegment(type="text", data={"text": content})],
        )
