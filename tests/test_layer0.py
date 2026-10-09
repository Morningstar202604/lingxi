"""Layer 0 测试 — 配置加载 + 协议模型 + 日志

🔗 工程逻辑
    Layer 0 测试不涉及 IO（网络/文件系统），全部用内存 mock。
    这保证 CI 在任何环境都能跑，不依赖外部服务。

🤖 AI 避坑
    - pytest-asyncio mode=strict: 异步测试必须显式标记
    - conftest.py 自动发现规则: 文件名必须是 conftest.py
    - fixture scope="function": 默认值，每个测试独立状态
"""
import asyncio

import pytest
from pydantic import ValidationError

from lingxi.core import (
    ChannelType,
    IMEvent,
    MessageSegment,
    PostType,
    MessageType,
    OutboundMessage,
    set_trace_id,
    get_trace_id,
)


# ── MessageSegment ──

class TestMessageSegment:
    def test_text_content(self) -> None:
        seg = MessageSegment(type="text", data={"text": "hello"})
        assert seg.text_content() == "hello"

    def test_text_content_not_text(self) -> None:
        seg = MessageSegment(type="image", data={"url": "http://x/y.png"})
        assert seg.text_content() == ""

    def test_is_at_everyone(self) -> None:
        seg = MessageSegment(type="at", data={"qq": "all"})
        assert seg.is_at()
        assert seg.is_at("all")
        assert not seg.is_at("123")

    def test_is_at_specific(self) -> None:
        seg = MessageSegment(type="at", data={"qq": "12345"})
        assert seg.is_at("12345")
        assert not seg.is_at("99999")

    def test_image_url(self) -> None:
        seg: MessageSegment = MessageSegment(type="image", data={"url": "https://img/1.jpg"})
        assert seg.is_image()
        assert seg.image_url == "https://img/1.jpg"

    def test_image_url_not_image(self) -> None:
        seg = MessageSegment(type="text", data={"text": "hi"})
        assert not seg.is_image()
        assert seg.image_url is None


# ── IMEvent ──

class TestIMEvent:
    def _make_event(self, **overrides) -> IMEvent:
        base = {
            "post_type": PostType.MESSAGE,
            "message_type": MessageType.GROUP,
            "channel": ChannelType.QQ_GROUP,
            "user_id": "111",
            "raw_message": "hello",
            "segments": [MessageSegment(type="text", data={"text": "hello"})],
        }
        base.update(overrides)
        return IMEvent(**base)

    def test_is_group(self) -> None:
        ev = self._make_event()
        assert ev.is_group
        assert not ev.is_private

    def test_is_private(self) -> None:
        ev = self._make_event(
            message_type=MessageType.PRIVATE,
            channel=ChannelType.QQ_PRIVATE,
        )
        assert ev.is_private
        assert not ev.is_group

    def test_chat_id_group(self) -> None:
        ev = self._make_event(group_id="888")
        assert ev.chat_id == "888"

    def test_chat_id_private(self) -> None:
        ev = self._make_event(
            message_type=MessageType.PRIVATE,
            channel=ChannelType.QQ_PRIVATE,
            group_id=None,
            user_id="111",
        )
        assert ev.chat_id == "111"

    def test_text(self) -> None:
        ev = self._make_event(
            segments=[
                MessageSegment(type="at", data={"qq": "222"}),
                MessageSegment(type="text", data={"text": "hello"}),
            ],
        )
        assert ev.text == "hello"

    def test_at_list(self) -> None:
        ev = self._make_event(
            segments=[
                MessageSegment(type="at", data={"qq": "222"}),
                MessageSegment(type="at", data={"qq": "333"}),
            ],
        )
        assert ev.at_list == ["222", "333"]

    def test_image_urls(self) -> None:
        ev = self._make_event(
            segments=[
                MessageSegment(type="image", data={"url": "https://a/1.png"}),
                MessageSegment(type="text", data={"text": "看这个"}),
                MessageSegment(type="image", data={"url": "https://a/2.png"}),
            ],
        )
        assert ev.image_urls == ["https://a/1.png", "https://a/2.png"]

    def test_raw_message_stored(self) -> None:
        ev = self._make_event(raw_message="[CQ:at,qq=222] hello")
        assert ev.raw_message == "[CQ:at,qq=222] hello"

    def test_validation_error_missing_required(self) -> None:
        with pytest.raises(ValidationError):
            IMEvent(post_type=PostType.MESSAGE, channel=ChannelType.QQ_GROUP)  # missing user_id, raw_message  # noqa: E501


# ── OutboundMessage ──

class TestOutboundMessage:
    def test_text_factory(self) -> None:
        msg = OutboundMessage.text(channel=ChannelType.QQ_PRIVATE, target_id="123", content="你好")
        assert msg.channel == ChannelType.QQ_PRIVATE
        assert msg.target_id == "123"
        assert len(msg.segments) == 1
        assert msg.segments[0].type == "text"
        assert msg.segments[0].data["text"] == "你好"


# ── trace_id ──

class TestTraceId:
    def test_auto_generate(self) -> None:
        tid = get_trace_id()
        assert isinstance(tid, str)
        assert len(tid) == 12

    def test_set_and_get(self) -> None:
        set_trace_id("test-trace-01")
        assert get_trace_id() == "test-trace-01"

    def test_isolation_across_tasks(self) -> None:
        """异步任务应该各有各的 trace_id 上下文。"""
        set_trace_id("main-task")
        assert get_trace_id() == "main-task"

        async def _child() -> str:
            # 新 task 继承父 context copy（ContextVar 语义）
            return get_trace_id()

        child_tid = asyncio.run(_child())
        # Python 3.12+ ContextVar: asyncio.create_task 继承父快照
        assert child_tid == "main-task"
