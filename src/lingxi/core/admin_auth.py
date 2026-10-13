"""管理面凭据校验 —— 全项目**唯一**实现（消除 4 份复制粘贴）。

🔗 工程逻辑
    `dashboard_api` / `mcp.router` / `seat.router` / `workflow.router` 此前各自
    抄了一份 `_require_admin`：常量时间比较、默认 token 判定、503 vs 401。
    复制粘贴的直接后果是**策略漂移**——本轮给 Dashboard 加"loopback 零配置
    放行"时，另外三个模块会静默保持 fail-closed，于是同一个进程里
    「工单能看、MCP 调不了」这种半残状态，比全挂更难排查。

    因此这里是单一事实来源：所有路由调用 `require_admin()` /
    `require_scrape()`，暴露面（是否 loopback）由 `set_local_only()` 一次性登记。

安全分级（按**暴露面**，不是按"有没有配置"）
    - 只监听 loopback + ``security.allow_anonymous_local`` → 允许**不带 token** 匿名
    - 监听 0.0.0.0 或未登记 → 未配置 token 即 503（fail-closed）
    - 只要请求**带了** token → 一律走常量时间比较，带错就是 401

🤖 AI 避坑
    - 免密分支要求 ``not x_admin_token``：带上错误 token 却被放行，等于
      "配了凭据反而更容易被绕过"。
    - 不要用 FastAPI 的 ``Header(None)`` 默认值当缺省凭据；缺省必须是 None。
    - 比较用 ``secrets.compare_digest``，不要 ``==``（时序侧信道）。
"""
from __future__ import annotations

import secrets

from fastapi import HTTPException

from lingxi.core import get_logger

log = get_logger(__name__)

# 与 SecurityConfig.admin_token 的默认值一致，用于识别"未改配置"
DEFAULT_ADMIN_TOKEN = "change-me-in-production"

# 进程级的"仅 loopback"登记位。由 Dashboard 装配时按监听地址设置一次；
# 默认 False（按已暴露处理），这样任何绕过装配直接调 require_admin 的路径
# （测试、脚本）都落在安全的一侧。
_local_only = False


def set_local_only(value: bool) -> None:
    """登记 Dashboard 监听地址是否为 loopback。

    必须在**启动 HTTP 服务之前**调用：晚于第一个请求就没有意义了。
    """
    global _local_only
    _local_only = value
    if value:
        from lingxi.config.settings import get_settings

        if get_settings().security.allow_anonymous_local:
            log.warning(
                "dashboard_local_anonymous_mode",
                detail=(
                    "Dashboard 仅监听本机且未配置 LINGXI_SECURITY__ADMIN_TOKEN，"
                    "管理面以本地免密模式运行。任何本机进程都可读写工单 —— "
                    "需要收紧请设置 security.allow_anonymous_local=false"
                ),
            )


def is_local_only() -> bool:
    return _local_only


def _unconfigured(expected: str) -> bool:
    """凭据是否处于"用户压根没配"的状态。"""
    from lingxi.config.settings import get_settings

    return expected == DEFAULT_ADMIN_TOKEN and not get_settings().debug


def _anonymous_local_allowed() -> bool:
    from lingxi.config.settings import get_settings

    return _local_only and get_settings().security.allow_anonymous_local


def require_admin(x_admin_token: str | None, *, detail: str | None = None) -> None:
    """管理凭据校验。所有 /api 管理端点共用。

    Args:
        x_admin_token: 请求头里的管理凭据（None = 没带）
        detail: 自定义 503 文案；缺省给出设置方法

    Raises:
        HTTPException: 503 未配置凭据 / 401 凭据错误
    """
    from lingxi.config.settings import get_settings

    expected = get_settings().security.admin_token.get_secret_value()

    if _unconfigured(expected):
        if _anonymous_local_allowed() and not x_admin_token:
            return  # 本地模式：无凭据放行
        raise HTTPException(
            status_code=503,
            detail=detail
            or (
                "未配置管理凭据：请设置 LINGXI_SECURITY__ADMIN_TOKEN "
                "环境变量后重启"
            ),
        )

    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_scrape(x_admin_token: str | None) -> None:
    """/metrics 抓取鉴权：scrape_token 优先，未配置则回退 admin_token。

    指标端点不是公开信息（会泄露内部端点名与状态码分布），
    所以非 loopback 时同样 fail-closed。
    """
    from lingxi.config.settings import get_settings

    settings = get_settings()
    scrape = settings.metrics.scrape_token
    expected = (
        scrape.get_secret_value() if scrape is not None
        else settings.security.admin_token.get_secret_value()
    )

    if (
        expected == DEFAULT_ADMIN_TOKEN
        and settings.metrics.scrape_token is None
        and not settings.debug
    ):
        if _anonymous_local_allowed() and not x_admin_token:
            return  # 本地模式：Prometheus 抓本机 sidecar 是常态
        raise HTTPException(
            status_code=503,
            detail=(
                "/metrics 未配置抓取凭据：请设置 "
                "LINGXI_METRICS__SCRAPE_TOKEN 环境变量"
            ),
        )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


__all__ = [
    "DEFAULT_ADMIN_TOKEN",
    "is_local_only",
    "require_admin",
    "require_scrape",
    "set_local_only",
]
