"""Node 1.2 — Gewechat（微信）适配器 v2：HTTP REST + Callback 模式。

🔗 工程逻辑
    Gewechat 与 NapCat 的通信模型完全不同：

    ┌─────────────────────── Gewechat 架构 ───────────────────────┐
    │                                                              │
    │   微信用户发消息 ──→ Gewechat 服务 ──→ HTTP POST Callback     │
    │                                          │                   │
    │                                    你的 HTTP 服务 (FastAPI)   │
    │                                          │                   │
    │                                    解析 Gewechat JSON 格式    │
    │                                          │                   │
    │                                    转换为 IMEvent 统一事件     │
    │                                                              │
    │   你的代码 ──→ HTTP POST ──→ Gewechat API ──→ 微信用户        │
    │                                                              │
    └──────────────────────────────────────────────────────────────┘

    Gewechat API 规范（https://github.com/Devo919/Gewechat）：
    - 所有 API 均需 Header: X-GEWE-TOKEN: <your_token>
    - 发送文本: POST /gewe/v2/api/message/postText
      Body: { "appId": "<deviceAppId>", "toWxid": "<wxid>", "content": "..." }
    - 获取登录状态: GET /gewe/v2/api/login/getLoginConfig?appId=...
    - 获取用户信息: GET /gewe/v2/api/user/getProfile?appId=...&wxid=...

    Gewechat 回调字段（POST 到你的 /api/gewechat/callback）：
    - data.newMsgId: 消息 ID (int)
    - data.fromUser: 发送者 wxid (str)
    - data.toUser: 接收者 wxid (str，即机器人自己)
    - data.msgType: 消息类型 (int: 1=文本, 3=图, 34=语音, 47=表情, 49=卡片, 10002=系统)
    - data.content: 消息内容 (str，文本直接是字符串，富媒体是 JSON)
    - data.atList: @列表 (list[str])
    - data.toWxid: 群聊时为 "xxx@chatroom"，私聊时为个人 wxid
    - data.timestamp: 时间戳 (int)

🤖 AI 避坑
    - Gewechat 群 wxid 格式 "xxxxx@chatroom"，不能 int() 强转！
    - msgType=49 (链接/小程序卡片) 时 content 是 XML/JSON，不是纯文本，需单独解析
    - Gewechat 的 atList 只包含 wxid，不包含被 at 者的昵称
    - Gewechat 服务本身需要先登录（扫码），否则所有 API 返回 200 但 ret 非 0
    - appId 是每设备唯一的，必须在所有 API 调用中一致传递（不同 appId 对应不同微信账号）
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx
from pydantic import BaseModel

from lingxi.core import get_logger
from lingxi.core.protocol import ChannelType, IMEvent, MessageSegment
from lingxi.im.adapter import IMAdapter, safe_int

log = get_logger(__name__)

# Gewechat 消息类型映射
_GW_MSG_TEXT = 1
_GW_MSG_IMAGE = 3
_GW_MSG_VOICE = 34
_GW_MSG_EMOJI = 47
_GW_MSG_CARD = 49
_GW_MSG_SYSTEM = 10002


class GewechatMessage(BaseModel):
    """Gewechat 回调中 data 字段的内部表示（仅作参考，实际在 handle_callback 中直接 dict 取值）。"""

    model_config = {"extra": "allow"}  # 兼容 Pydantic v2

    new_msg_id: int = 0
    from_user: str = ""
    to_user: str = ""
    msg_type: int = 0
    content: str = ""
    at_list: list[str] = []
    to_wxid: str = ""
    timestamp: int = 0
    group_name: str = ""
    from_user_name: str = ""


class GewechatAdapter(IMAdapter):
    """ Gewechat 适配器：HTTP REST + Callback 接收。

    使用方法：
        adapter = GewechatAdapter(
            api_base="http://127.0.0.1:2531",
            token="your-token",
            app_id="device-app-id",
            callback_path="/api/gewechat/callback",
            on_event=handle_event,
        )
        # 1. 在你的 FastAPI 路由中注册 callback_path → adapter.handle_callback
        # 2. await adapter.run()  # 启动健康探测 + reconnect
    """

    def __init__(
        self,
        api_base: str,
        token: str,
        app_id: str,
        callback_path: str = "/api/gewechat/callback",
        on_event: Callable[[IMEvent], Awaitable[None]] | None = None,
        health_check_interval: float = 30.0,
    ) -> None:
        super().__init__(on_event=on_event)
        self._api_base = api_base.rstrip("/")
        self._token = token
        self._app_id = app_id
        self._callback_path = callback_path
        self._health_interval = health_check_interval
        self._client: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "X-GEWE-TOKEN": self._token,
            "Content-Type": "application/json",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._api_base,
                headers=self._headers(),
                timeout=15.0,
            )
        return self._client

    async def send(self, message: Any) -> bool:
        """通过 Gewechat API 发送消息。"""
        from lingxi.core.protocol import OutboundMessage

        client = await self._get_client()
        if isinstance(message, OutboundMessage):
            # 解密 OutboundMessage 的 segments 为纯文本
            texts = []
            for seg in message.segments:
                if seg.type == "text":
                    t = seg.data.get("text", "")
                    if t:
                        texts.append(t)
            content = "\n".join(texts)
            target = message.target_id
        elif isinstance(message, str):
            content = message
            target = ""
        else:
            log.warning("gewechat_send_unsupported_type", type=type(message).__name__)
            return False

        if not target:
            log.warning("gewechat_send_no_target")
            return False

        # Gewechat 通过 to_wxid 字段识别目标（私聊 wxid，群聊 xxx@chatroom）
        payload = {
            "appId": self._app_id,
            "toWxid": target,
            "content": content,
        }

        try:
            resp = await client.post("/gewe/v2/api/message/postText", json=payload)
            data = resp.json() if resp.content else {}
            if resp.status_code == 200 and data.get("ret") == 0:
                log.debug("gewechat_send_ok", target=target[:30])
                return True
            log.warning("gewechat_send_failed", ret=data.get("ret"), msg=data.get("msg", ""))
            return False
        except httpx.HTTPError as e:
            log.exception("gewechat_http_error", error=str(e))
            return False

    async def handle_callback(self, payload: dict[str, Any]) -> None:
        """处理 Gewechat 的 HTTP POST 回调。

        在 FastAPI 路由中调用这个函数::

            @app.post("/api/gewechat/callback")
            async def gewechat_webhook(data: dict):
                await adapter.handle_callback({"data": data})
                return {"ret": 0}
        """
        # Gewechat 的 POST body 最外层是 data 字段
        data = payload.get("data") or payload
        if not data:
            return

        event = self._to_im_event(data)
        if event is not None:
            await self._dispatch(event)

    def _to_im_event(self, data: dict[str, Any]) -> IMEvent | None:
        """ Gewechat 的回调数据 → 统一 IMEvent。"""
        # 安全取字段
        new_msg_id = safe_int(data.get("newMsgId")) or 0
        from_user = str(data.get("fromUser") or data.get("from_user") or "")
        to_user = str(data.get("toUser") or data.get("to_user") or "")
        msg_type = safe_int(data.get("msgType")) or 0
        content = str(data.get("content") or "")
        at_list = data.get("atList") or []
        to_wxid = str(data.get("toWxid") or data.get("to_wxid") or "")
        timestamp = safe_int(data.get("timestamp")) or 0

        if not from_user or not content:
            return None

        # 判断私聊 vs 群聊
        is_group = "@chatroom" in to_wxid or "@chatroom" in to_user

        # 构建 IMEvent（使用正确的 channel 类型）
        if is_group:
            group_id = to_wxid or to_user  # 不使用 int() 强转
            segment_list = _parse_gw_message_segments(msg_type, content)
            return IMEvent(
                post_type="notice",
                message_type="group",
                channel=ChannelType.WECHAT_GROUP,
                user_id=from_user,
                group_id=group_id,
                raw_message=content,
                segments=segment_list,
            )
        else:
            segment_list = _parse_gw_message_segments(msg_type, content)
            return IMEvent(
                post_type="notice",
                message_type="private",
                channel=ChannelType.WECHAT_PRIVATE,
                user_id=from_user,
                raw_message=content,
                segments=segment_list,
            )

    async def health_check(self) -> bool:
        """检查 Gewechat 服务是否可达且已登录。"""
        try:
            client = await self._get_client()
            resp = await client.get(
                "/gewe/v2/api/login/getLoginConfig",
                params={"appId": self._app_id},
            )
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                ret = data.get("ret")
                if ret == 0:
                    log.info("gewechat_healthy", app_id=self._app_id[:20])
                    return True
                log.warning("gewechat_unhealthy", ret=ret, msg=data.get("msg", ""))
                return False
            log.warning("gewechat_health_status", status=resp.status_code)
            return False
        except httpx.HTTPError as e:
            log.warning("gewechat_health_connection_failed", error=str(e))
            return False

    async def run(self) -> None:
        """启动 Gewechat 适配器：定期健康检查 + 自检 app_id。"""
        self._running = True
        log.info("gewechat_adapter_started", api_base=self._api_base, app_id=self._app_id[:20])

        consecutive_failures = 0
        max_failures = 5

        try:
            while self._running:
                healthy = await self.health_check()
                if healthy:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= max_failures:
                        log.error("gewechat_too_many_failures", count=consecutive_failures)
                        # 不退出，继续尝试；由上层决定是否重启

                await asyncio.sleep(self._health_interval)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def stop(self) -> None:
        """停止适配器并释放 HTTP client。"""
        self._running = False
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        log.info("gewechat_adapter_stopped")


def _parse_gw_message_segments(msg_type: int, content: str) -> list[MessageSegment]:
    """根据 Gewechat msgType 类型将 content 解析为 MessageSegment 列表。

    Gewechat 不返回结构化段列表，所有内容都在 content 字段里：
    - msgType=1: content 是纯文本
    - msgType=3: content 是 JSON，包含 fileId / xml / aesKey 等（图片）
    - msgType=34: content 是 JSON，包含 silk 语音数据
    - msgType=47: content 是 JSON，包含表情 CDN URL
    - msgType=49: content 是 XML/JSON 卡片
    - msgType=10002: content 是系统通知文本
    """
    import json

    if msg_type == _GW_MSG_TEXT or msg_type == _GW_MSG_SYSTEM:
        return [MessageSegment(type="text", data={"text": content})] if content.strip() else []
    elif msg_type == _GW_MSG_IMAGE:
        # content 通常是 XML 或 JSON，包含 CDN URL；提取 cdnwxurl 字段
        image_url = _extract_image_url(content)
        if image_url:
            return [MessageSegment(type="image", data={"url": image_url})]
        return [MessageSegment(type="text", data={"text": "[图片]"})]
    elif msg_type == _GW_MSG_VOICE:
        return [MessageSegment(type="text", data={"text": "[语音]"})]
    elif msg_type == _GW_MSG_EMOJI:
        return [MessageSegment(type="text", data={"text": "[表情]"})]
    elif msg_type == _GW_MSG_CARD:
        title = _extract_card_title(content)
        if title:
            return [MessageSegment(type="text", data={"text": f"[卡片] {title}"})]
        return [MessageSegment(type="text", data={"text": "[链接卡片]"})]
    else:
        return [MessageSegment(type="text", data={"text": content})] if content.strip() else []


def _extract_image_url(content: str) -> str | None:
    """从 Gewechat 图片消息的 content 中提取 CDN URL。"""
    import json
    import re

    # 尝试 JSON 解析
    try:
        d = json.loads(content)
        for key in ("cdnwxurl", "url", "fileId", "fileIdFull"):
            v = d.get(key)
            if isinstance(v, str) and v:
                return v
    except (json.JSONDecodeError, TypeError):
        pass

    # 尝试从 XML 中提取
    m = re.search(r'cdnwxurl\s*=\s*"([^"]+)"', content)
    if m:
        return m.group(1)
    m = re.search(r'"cdnwxurl":"([^"]+)"', content)
    if m:
        return m.group(1)
    return None


def _extract_card_title(content: str) -> str | None:
    """从 Gewechat 卡片消息 content 中提取标题。"""
    import json
    import re

    try:
        d = json.loads(content)
        for key in ("title", "displayName", "appname"):
            if d.get(key):
                return str(d[key])
    except (json.JSONDecodeError, TypeError):
        pass

    m = re.search(r'<title>([^<]+)</title>', content)
    if m:
        return m.group(1)
    m = re.search(r'"title"\s*:\s*"([^"]+)"', content)
    if m:
        return m.group(1)
    return None
