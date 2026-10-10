"""Node 0.5 — pytest fixtures 共享配置。

🔗 工程逻辑
    conftest.py 里的 fixture 自动注入到同目录及子目录的所有测试。
    这里提供：
    - 测试级日志初始化（避免 "no handler found" 警告）
    - 临时数据目录（函数级隔离，自动清理）

🤖 AI 避坑
    - fixture 名称不能以下划线开头（pytest 会忽略）
    - yield fixture 清理代码在测试异常时也执行（finally 语义）
    - tmp_path 是 pytest 内置 fixture，别自己重新发明
"""
from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from pydantic import SecretStr

from lingxi.config.settings import (
    AgentConfig,
    LLMConfig,
    MemoryConfig,
    OneBotConfig,
    SecurityConfig,
    Settings,
)


@pytest.fixture(autouse=True)
def _init_logging() -> None:
    """每个测试运行前初始化日志系统（仅控制台，DEBUG 级别）。"""
    from lingxi.core.logging import configure_logging

    configure_logging(log_level="DEBUG")


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """创建一个临时数据目录，测试后自动清理。"""
    data_dir = tmp_path / "lingxi_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    yield data_dir
    # tmp_path 由 pytest 自动清理，这里是演示性保护
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture
def test_settings(tmp_data_dir: Path) -> Settings:
    """测试用 Settings 实例 — 所有字段用默认值。"""
    return Settings(
        data_dir=tmp_data_dir,
        debug=True,
        log_level="DEBUG",
        onebot=OneBotConfig(ws_url="ws://test:3001"),
        llm=LLMConfig(model="test-model"),
        memory=MemoryConfig(backend="local"),
        agent=AgentConfig(react_steps=2),
        security=SecurityConfig(permission_model="open"),
    )


@pytest.fixture
def admin_token_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """给 Dashboard API 注入非默认 admin_token，并让 get_settings 返回它。

    Dashboard 现在对"未改默认 token"fail-closed（见 docs/AUDIT-2026-10-10.md #8），
    故需要认证的测试必须显式配置凭据，而不是依赖默认值。
    """
    from lingxi.config.settings import get_settings

    token = "test-admin-token-8f3a1c"
    settings = get_settings()
    monkeypatch.setattr(
        settings.security,
        "admin_token",
        SecretStr(token),
        raising=False,
    )
    return token


@pytest.fixture
def admin_headers(admin_token_env: str) -> dict[str, str]:
    """带有效管理 token 的请求头。"""
    return {"x-admin-token": admin_token_env}
