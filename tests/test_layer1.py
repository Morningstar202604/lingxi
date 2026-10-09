"""Layer 1 测试 — CQ 解析 + 消息解析 + adapter dispatch。

🔗 工程逻辑
    Layer 1 涉及 WebSocket 但测试不建真实连接：
    - CQ 解析和消息解析是纯函数，直接测
    - adapter dispatch 用 mock handler，验证调用顺序
    - NapCat 网络部分留到 e2e 测试（不在单元测范围内）
    - GewechatAdapter 通过 handle_callback() 接收回调来测试
"""
from __future__ import annotations

import asyncio

import pytest

from lingxi.core import (
    ChannelType,
    IMEvent,
    MessageSegment,
    PostType,
    MessageType,
)
from lingxi.im.adapter import _parse_onebot_message, _parse_cq_code
from lingxi.im.gewechat import GewechatAdapter
from lingxi.im.napcat import NapCatAdapter
from lingxi.config.settings import OneBotConfig


# ── CQ 码解析 ──

class TestCQParser:
    def test_plain_text(self) -> None:
        result = _parse_cq_code("hello world")
        assert len(result) == 1
        assert result[0].type == "text"
        assert result[0].data["text"] == "hello world"

    def test_at_code(self) -> None:
        result = _parse_cq_code("[CQ:at,qq=12345]")
        assert len(result) == 1
        assert result[0].type == "at"
        assert result[0].data["qq"] == "12345"

    def test_image_code(self) -> None:
        result = _parse_cq_code("[CQ:image,url=https://img/i.png,file=i.png]")
        assert len(result) == 1
        assert result[0].type == "image"
        assert result[0].data["url"] == "https://img/i.png"

    def test_mixed_text_and_cq(self) -> None:
        """CQ 与文本混合：4 段 (at + 文本 + image)。"""
        result = _parse_cq_code("[CQ:at,qq=111] 你好 [CQ:image,url=x.png]")
        assert len(result) == 3
        assert result[0].type == "at"
        assert result[1].type == "text"
        assert result[1].data["text"] == " 你好 "
        assert result[2].type == "image"

    def test_broken_cq_no_close(self) -> None:
        """未闭合的 CQ 码应该当纯文本吞掉（不 crash）。"""
        result = _parse_cq_code("[CQ:at,qq=123 泄漏了")
        assert len(result) == 1
        assert result[0].type == "text"

    def test_at_all(self) -> None:
        result = _parse_cq_code("[CQ:at,qq=all]")
        assert result[0].type == "at"
        assert result[0].data["qq"] == "all"


# ── OneBot 消息解析 ──

class TestParseOnebot:
    def test_str_plain(self) -> None:
        result = _parse_onebot_message("你好")
        assert len(result) == 1
        assert result[0].text_content() == "你好"

    def test_str_with_cq(self) -> None:
        result = _parse_onebot_message("[CQ:face,id=123]笑一个")
        assert len(result) == 2
        assert result[0].type == "face"

    def test_structured_segments(self) -> None:
        raw = [
            {"type": "at", "data": {"qq": "222"}},
            {"type": "text", "data": {"text": "看看这个"}},
        ]
        result = _parse_onebot_message(raw)
        assert len(result) == 2
        assert result[0].type == "at"
        assert result[1].text_content() == "看看这个"

    def test_empty_structure(self) -> None:
        result = _parse_onebot_message([])
        assert len(result) == 0


# ── NapCat event conversion ──

class TestNapCatEventConversion:
    def _napcat(self) -> NapCatAdapter:
        return NapCatAdapter(OneBotConfig(ws_url="ws://test:3001"))

    def test_group_message(self) -> None:
        raw = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 111,
            "group_id": 888,
            "raw_message": "[CQ:at,qq=999] hello",
            "message": "[CQ:at,qq=999] hello",
            "time": 1700000000,
            "self_id": 999,
            "message_id": 42,
        }
        napcat = self._napcat()
        ev = napcat._to_im_event(raw)
        assert ev is not None
        assert ev.channel == ChannelType.QQ_GROUP
        assert ev.user_id == "111"
        assert ev.group_id == "888"
        assert ev.is_group
        assert ev.self_id == "999"
        assert ev.message_id == "42"
        assert any(s.type == "at" for s in ev.segments)
        assert ev.text == " hello"

    def test_private_message(self) -> None:
        raw = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 111,
            "raw_message": "你好",
            "message": "你好",
            "time": 1700000000,
            "self_id": 999,
            "message_id": 1,
        }
        napcat = self._napcat()
        ev = napcat._to_im_event(raw)
        assert ev is not None
        assert ev.channel == ChannelType.QQ_PRIVATE
        assert ev.is_private
        assert ev.group_id is None
        assert ev.text == "你好"

    def test_non_message_event(self) -> None:
        raw = {"post_type": "notice", "notice_type": "group_increase"}
        napcat = self._napcat()
        assert napcat._to_im_event(raw) is None

    def test_structured_message(self) -> None:
        raw = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 111,
            "group_id": 888,
            "raw_message": "",
            "message": [
                {"type": "at", "data": {"qq": "999"}},
                {"type": "text", "data": {"text": "帮我看下"}},
            ],
            "time": 1700000000,
            "self_id": 999,
        }
        napcat = self._napcat()
        ev = napcat._to_im_event(raw)
        assert ev is not None
        assert len(ev.segments) == 2
        assert ev.text == "帮我看下"


# ── Gewechat event conversion ──

class TestGewechatEventConversion:
    """测试 GewechatAdapter 解析 Gewechat 回调格式 → IMEvent。"""

    def _gewechat(self) -> GewechatAdapter:
        return GewechatAdapter(
            api_base="http://127.0.0.1:2531",
            token="test-token",
            app_id="test-appid",
        )

    def test_group_uses_wechat_channel(self) -> None:
        """Gewechat 群聊消息：toWxid 包含 @chatroom → WECHAT_GROUP。"""
        gw_data = {
            "newMsgId": 12345,
            "fromUser": "wxid_user222",
            "toUser": "wxid_self999",
            "msgType": 1,
            "content": "@智犀 北京天气",
            "toWxid": "xxxxx@chatroom",
            "atList": ["wxid_self999"],
            "timestamp": 1700000000,
        }
        gwc = self._gewechat()
        ev = gwc._to_im_event(gw_data)
        assert ev is not None
        assert ev.channel == ChannelType.WECHAT_GROUP
        # 微信 group_id 保留原始格式（不转 int，因为有 @chatroom 后缀）
        assert "xxxxx@chatroom" in (ev.group_id or "")

    def test_private_uses_wechat_channel(self) -> None:
        """Gewechat 私聊消息：toWxid 不含 @chatroom → WECHAT_PRIVATE。"""
        gw_data = {
            "newMsgId": 12346,
            "fromUser": "wxid_user111",
            "toUser": "wxid_self999",
            "msgType": 1,
            "content": "你好",
            "toWxid": "wxid_user111",
            "atList": [],
            "timestamp": 1700000001,
        }
        gwc = self._gewechat()
        ev = gwc._to_im_event(gw_data)
        assert ev is not None
        assert ev.channel == ChannelType.WECHAT_PRIVATE
        assert ev.user_id == "wxid_user111"


# ── Adapter event dispatch (mock handler) ──

class TestAdapterDispatch:
    @pytest.mark.asyncio
    async def test_handler_called(self) -> None:
        calls: list[IMEvent] = []

        async def _handler(ev: IMEvent) -> None:
            calls.append(ev)

        adapter = NapCatAdapter(
            config=OneBotConfig(ws_url="ws://test:3001"),
            on_event=_handler,
        )

        event = IMEvent(
            post_type=PostType.MESSAGE,
            message_type=MessageType.PRIVATE,
            channel=ChannelType.QQ_PRIVATE,
            user_id="111",
            raw_message="hello",
        )
        await adapter._dispatch(event)

        assert len(calls) == 1
        assert calls[0].user_id == "111"

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_crash(self) -> None:
        """无 handler 时 _dispatch 不崩溃。"""
        adapter = NapCatAdapter(
            config=OneBotConfig(ws_url="ws://test:3001"),
            on_event=None,
        )

        event = IMEvent(
            post_type=PostType.MESSAGE,
            message_type=MessageType.PRIVATE,
            channel=ChannelType.QQ_PRIVATE,
            user_id="111",
            raw_message="hi",
        )
        await adapter._dispatch(event)  # 不应 raise
