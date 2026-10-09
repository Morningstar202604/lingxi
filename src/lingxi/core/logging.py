"""Node 0.4 — structlog 结构化日志 + 统一 trace_id 链路追踪。

🔗 工程逻辑
    为什么选 structlog 而不是标准 logging 或 loguru？
    - structlog 是 Python 日志的事实标准（FastAPI/Click/Django 都在用）
    - 原生绑定 key-value context → JSON 输出，与任何后端（ELK/Grafana/Loki）天然对接
    - 日志 = 事件流，不是文本。trace_id 串联所有相关日志

🤖 AI 避坑
    - 不要在每个模块创建自己的 logger: get_logger() 返回 Module-level logger 是正确做法
    - trace_id 必须从请求入口注入，不能在业务代码里生成（否则无法串联）
    - 不要在日志里打 SecretStr 的值（pydantic 已掩码 repr 但 str() 会裸露！）
"""
from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from functools import lru_cache
from typing import Any

import structlog

# ── trace_id 上下文变量（每个 asyncio task 独立）──
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_trace_id(tid: str | None) -> None:
    """设置当前上下文的 trace_id。在请求/事件入口调用。"""
    trace_id_var.set(tid)


def get_trace_id() -> str:
    """获取当前 trace_id，没有则自动生成一个。"""
    tid = trace_id_var.get()
    if tid is None:
        tid = uuid.uuid4().hex[:12]
        trace_id_var.set(tid)
    return tid


def _add_trace_id(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: 把 trace_id 自动加入每条日志。"""
    tid = trace_id_var.get()
    if tid:
        event_dict["trace_id"] = tid
    return event_dict


def _render_to_stderr(logger: Any, method_name: str, event_dict: dict[str, Any]) -> None:
    """Fallback processor — 当 structlog 未初始化时直接打到 stderr。"""
    # 生产环境由 configure_logging() 覆盖
    print(event_dict, file=sys.stderr)


@lru_cache(maxsize=None)
def configure_logging(log_level: str = "INFO", json_format: bool = False) -> None:
    """初始化全局结构化日志。幂等（重复调用不叠加 handler）。"""
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        _add_trace_id,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if json_format
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # 把第三方库 logger 也接到 structlog
    for name in ("uvicorn", "uvicorn.access", "httpcore", "httpx"):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """获取模块级 logger。

    用法::

        from lingxi.core.logging import get_logger
        log = get_logger(__name__)
        log.info("user_login", user_id="42")
    """
    return structlog.get_logger(name)


__all__ = [
    "configure_logging",
    "get_logger",
    "set_trace_id",
    "get_trace_id",
    "trace_id_var",
]
