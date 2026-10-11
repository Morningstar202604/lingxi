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
from collections import deque
from contextvars import ContextVar
from functools import cache
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from structlog.typing import EventDict, Processor


class MemoryLogHandler(logging.Handler):
    """内存环形日志缓冲区（供 Dashboard /api/logs 读取）。

    在 get_logger() 之前调用 configure_logging() 时附带此 handler，
    前端 /API/logs 即可拿到最近 N 条结构化日志而不必落盘或对接 ELK。
    """

    def __init__(self, maxlen: int = 1000) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry: dict[str, Any] = {
                "time": self.format(record),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            self._records.append(entry)
        except Exception:
            self.handleError(record)

    def recent(self, n: int = 200) -> list[dict[str, Any]]:
        """返回最近 n 条日志（最新的排最后）。"""
        items = list(self._records)[-n:]
        return items

    def clear(self) -> None:
        self._records.clear()


# 全局单例 — dashboard_api.py 直接 import 使用
memory_handler = MemoryLogHandler(maxlen=1000)

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


def _add_trace_id(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:  # noqa: ARG001
    """structlog processor: 把 trace_id 自动加入每条日志。"""
    tid = trace_id_var.get()
    if tid:
        event_dict["trace_id"] = tid
    return event_dict


@cache
def configure_logging(log_level: str = "INFO", json_format: bool = False) -> None:
    """初始化全局结构化日志。幂等（重复调用不叠加 handler）。"""
    shared_processors: list[Processor] = [
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
    # 把内存 handler 也接上，让 /api/logs 能读最近日志
    memory_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root.addHandler(memory_handler)
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
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


__all__ = [
    "configure_logging",
    "get_logger",
    "set_trace_id",
    "get_trace_id",
    "trace_id_var",
]
