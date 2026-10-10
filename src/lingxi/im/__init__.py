"""Layer 1 公开符号 — 引用 adapter + napcat + gewechat。

Node 1.3 = 统一事件流 = 由 IMEvent + IMAdapter 提供，不需要额外文件。
统一事件流的设计在 protocol.py 的 IMEvent 和 adapter.py 的 IMAdapter 中已经定义。
"""
from lingxi.im.adapter import IMAdapter
from lingxi.im.gewechat import GewechatAdapter
from lingxi.im.napcat import NapCatAdapter

__all__ = ["IMAdapter", "NapCatAdapter", "GewechatAdapter"]
