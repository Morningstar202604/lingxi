"""Node 1.1 — NapCat WebSocket OneBot 客户端 (QQ) v2。

🔗 工程逻辑
    NapCat 通过 WebSocket 暴露 OneBot v11 API：
    - 接收事件：服务端 push JSON 到我们
    - 发送消息：我们 push JSON 到服务端
    - 心跳：定期发 {"action":"get_status","echo":"NapCat_heartbeat_<ts>"}

    单一 bidir WebSocket（OneBot v11 标准：/api/send_group_msg、send_private_msg 等）。

🤖 AI 避坑
    - 发送结构化消息段时必须携带 "auto_escape": true，防止 NapCat 把 JSON 数组当 CQ 字符串解析
    - 群 ID 和 QQ 号都是 int，但某些 OneBot 实现返回 str，需用 safe_int()
    - OneBot 4 种上报类型都要处理：message / notice / request / meta_event
    - gather(receive, heartbeat) 中一个失败不应 kill 另一个
    - heartbeat 失败后必须退出并让外层的重连循环接手
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

import websockets
from websockets.asyncio.client import ClientConnection

from lingxi.config.settings import OneBotConfig
from lingxi.core import (
    ChannelType,
    IMEvent,
    MessageType,
    PostType,
    get_logger,
)
from lingxi.im.adapter import IMAdapter, _parse_onebot_message, safe_int

log = get_logger(__name__)


class NapCatAdapter(IMAdapter):
    """NapCat OneBot v11 WebSocket 适配器。"""

    def __init__(
        self,
        config: OneBotConfig,
        bot_qq: str | None = None,
        on_event: Callable[[IMEvent], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(on_event=on_event)
        self._config = config
        self._bot_qq = bot_qq
        self._ws: ClientConnection | None = None
        self._echo_counter = 0
        self._pending_echo: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def run(self) -> None:
        """启动 NapCat 连接（含自动重连 + 错误隔离）。"""
        self._running = True
        backoff = self._config.reconnect_interval

        while self._running:
            try:
                log.info("napcat_connecting", ws_url=self._config.ws_url)
                async with websockets.connect(
                    self._config.ws_url,
                    additional_headers=self._auth_headers(),
                ) as ws:
                    self._ws = ws
                    backoff = self._config.reconnect_interval
                    log.info("napcat_connected")

                    # 收事件 + 发心跳：各自内部容错，互不 kill
                    await self._run_with_error_isolation(ws)

            except asyncio.CancelledError:
                break
            except Exception:
                if not self._running:
                    break
                log.warning("napcat_connection_lost", backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

        self._running = False
        log.info("napcat_adapter_exited")

    async def _run_with_error_isolation(self, ws: ClientConnection) -> None:
        """两个任务独立运行，一个退出不 kill 另一个；外层负责收尸。"""
        recv_task = asyncio.create_task(self._receive_loop(ws))
        hb_task = asyncio.create_task(self._heartbeat_loop(ws))

        try:
            # 任何一个完成（正常或异常）就继续
            done, pending = await asyncio.wait(
                [recv_task, hb_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 检查 completed task 是否有异常
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    log.warning("napcat_task_error", error=str(exc))
        finally:
            # 清理未完成的 task
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            # 清理 pending_echo futures（避免 hang 在已断开的连接上）
            self._cleanup_pending_echo()

    async def stop(self) -> None:
        self._running = False
        self._cleanup_pending_echo()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _cleanup_pending_echo(self) -> None:
        for fut in self._pending_echo.values():
            if not fut.done():
                fut.cancel()
        self._pending_echo.clear()

    # ── 发送消息 ──

    async def send(self, message: Any) -> None:
        """通过 OneBot send_*_msg API 发消息。"""
        from lingxi.core.protocol import OutboundMessage

        if not self._ws:
            log.warning("napcat_not_connected")
            return

        # channel → action + 目标 ID
        if message.channel == ChannelType.QQ_GROUP:
            action = "send_group_msg"
            params: dict[str, Any] = {"group_id": safe_int(message.target_id)}
        elif message.channel == ChannelType.QQ_PRIVATE:
            action = "send_private_msg"
            params = {"user_id": safe_int(message.target_id)}
        elif message.channel in (ChannelType.WECHAT_GROUP, ChannelType.WECHAT_PRIVATE):
            # NapCat 不支持微信；返回并记录（避免 silence 失败）
            log.warning("napcat_cannot_send_wechat", channel=message.channel.value)
            return
        else:
            log.warning("napcat_unknown_channel", channel=message.channel.value)
            return

        # 检查 ID 是否有效
        target_id = params.get("user_id") or params.get("group_id")
        if target_id is None:
            log.warning("napcat_send_invalid_target", target=message.target_id)
            return

        # 结构化段 → OneBot 数组格式
        onebot_msg = [
            {"type": seg.type, "data": seg.data}
            for seg in message.segments
        ]

        # P0 修复：必须带 auto_escape=True，避免 NapCat 当 CQ 码字符串处理
        params["message"] = onebot_msg
        params["auto_escape"] = True

        await self._api_call(action, params)

    # ── 内部实现 ──

    def _auth_headers(self) -> dict[str, str]:
        if self._config.access_token:
            return {"Authorization": f"Bearer {self._config.access_token.get_secret_value()}"}
        return {}

    def _next_echo(self) -> str:
        self._echo_counter += 1
        return f"lingxi_{self._echo_counter}"

    async def _api_call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """API 请求 + echo 匹配响应。"""
        echo = self._next_echo()
        payload = {"action": action, "params": params, "echo": echo}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_echo[echo] = fut

        if self._ws:
            try:
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                self._pending_echo.pop(echo, None)
                raise

        try:
            return await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            log.warning("api_timeout", action=action)
            return {"status": "timeout", "data": None}
        finally:
            self._pending_echo.pop(echo, None)

    async def _receive_loop(self, ws: ClientConnection) -> None:
        """持续接收事件并 dispatch（message/notice/request/meta_event 全处理）。"""
        try:
            async for raw in ws:
                if not self._running:
                    break
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    log.warning("invalid_json", raw=str(raw)[:200])
                    continue

                # 响应（有 echo 字段）
                if "echo" in data:
                    fut = self._pending_echo.get(data["echo"])
                    if fut and not fut.done():
                        fut.set_result(data)
                    continue

                # 事件（有 post_type 字段）
                if "post_type" not in data:
                    continue

                event = self._to_im_event(data)
                if event is not None:
                    await self._dispatch(event)

        except websockets.ConnectionClosed:
            log.info("napcat_receive_connection_closed")
        except asyncio.CancelledError:
            pass

    async def _heartbeat_loop(self, ws: ClientConnection) -> None:
        """定期发心跳包。"""
        while self._running:
            try:
                if ws and ws.state == websockets.protocol.State.OPEN:
                    ts = int(time.time() * 1000)
                    await ws.send(json.dumps({
                        "action": "get_status",
                        "echo": f"NapCat_heartbeat_{ts}",
                    }))
                else:
                    # ws 已关闭，退出 heartbeat 让外层重连
                    break
            except websockets.ConnectionClosed:
                log.info("napcat_heartbeat_ws_closed")
                break
            except Exception as e:
                log.warning("napcat_heartbeat_error", error=str(e))
                break

            await asyncio.sleep(self._config.heartbeat_interval)

    def _to_im_event(self, data: dict[str, Any]) -> IMEvent | None:
        """OneBot 事件 → 统一 IMEvent。

        处理 4 种 post_type:
        - message: 普通消息（私聊/群聊）→ 返回 IMEvent
        - notice: 群成员变动、群管理等 → 可选处理（返回 notice 事件）
        - request: 加群/加好友请求 → 返回 request 事件
        - meta_event: 生命周期/心跳 → 仅日志，不生成 IMEvent
        """
        post_type = data.get("post_type")

        if post_type == "message":
            return self._handle_message(data)
        elif post_type == "notice":
            return self._handle_notice(data)
        elif post_type == "request":
            return self._handle_request(data)
        elif post_type == "meta_event":
            # meta_event 只用于生命周期感知，不生成用户 IMEvent
            log.debug("meta_event_received", type=data.get("meta_event_type"))
            return None
        else:
            log.debug("unknown_post_type", post_type=post_type)
            return None

    def _handle_message(self, data: dict[str, Any]) -> IMEvent | None:
        msg_type_str = data.get("message_type", "private")
        try:
            msg_type = MessageType(msg_type_str)
        except ValueError:
            log.debug("unknown_message_type", type=msg_type_str)
            return None

        channel = (
            ChannelType.QQ_GROUP
            if msg_type == MessageType.GROUP
            else ChannelType.QQ_PRIVATE
        )

        # OneBot 的 user_id / group_id / message_id 都是 int，但某些实现返回 str
        user_id = safe_int(data.get("user_id"))
        group_id = safe_int(data.get("group_id")) if data.get("group_id") else None
        message_id = safe_int(data.get("message_id")) if data.get("message_id") else None

        # 过滤自己的消息（防止机器人自己触发自己）
        self_id = safe_int(data.get("self_id"))
        if self_id and user_id and self_id == user_id:
            return None

        segments = _parse_onebot_message(data.get("message", ""))

        return IMEvent(
            post_type=PostType.MESSAGE,
            message_type=msg_type,
            channel=channel,
            self_id=str(self_id) if self_id else None,
            user_id=str(user_id) if user_id else "",
            group_id=str(group_id) if group_id is not None else None,
            message_id=str(message_id) if message_id else None,
            raw_message=data.get("raw_message", ""),
            segments=segments,
            timestamp=data.get("time"),
        )

    def _handle_notice(self, data: dict[str, Any]) -> IMEvent | None:
        """处理 notice 事件（群成员变动、群管理等）。

        当前策略：仅把关键 notice 记入日志 + 可选生成特殊 IMEvent。
        大多数 notice 不需要作为消息回复，但未来可扩展（如欢迎新成员）。
        """
        notice_type = data.get("notice_type", "")
        user_id = safe_int(data.get("user_id"))
        group_id = safe_int(data.get("group_id")) if data.get("group_id") else None

        log.info("napcat_notice", type=notice_type, user_id=user_id, group_id=group_id)
        # 不返回 IMEvent（避免干扰正常对话流）
        return None

    def _handle_request(self, data: dict[str, Any]) -> IMEvent | None:
        """处理 request 事件（加群/加好友请求）。

        当前策略：仅坐席通过 Dashboard 处理；机器人自己不回应请求。
        记录日志供坐席在 Dashboard 中看到。
        """
        request_type = data.get("request_type", "")
        user_id = safe_int(data.get("user_id"))
        group_id = safe_int(data.get("group_id")) if data.get("group_id") else None

        log.info("napcat_request", type=request_type, user_id=user_id, group_id=group_id)
        return None


__all__ = ["NapCatAdapter", "_parse_onebot_message", "safe_int"]
