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
from pathlib import Path

import pytest

from lingxi.config.settings import Settings, OneBotConfig, LLMConfig, MemoryConfig, AgentConfig, SecurityConfig


@pytest.fixture(autouse=True)
def _init_logging() -> None:
    """每个测试运行前初始化日志系统（仅控制台，DEBUG 级别）。"""
    import structlog
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
