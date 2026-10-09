"""核心基础模块：协议模型 + 结构化日志。"""
from lingxi.core.logging import configure_logging, get_logger, get_trace_id, set_trace_id
from lingxi.core.protocol import (
    ChannelType,
    IMEvent,
    MessageType,
    MessageSegment,
    OutboundMessage,
    PostType,
)

__all__ = [
    "ChannelType",
    "IMEvent",
    "MessageType",
    "MessageSegment",
    "OutboundMessage",
    "PostType",
    "configure_logging",
    "get_logger",
    "get_trace_id",
    "set_trace_id",
]
