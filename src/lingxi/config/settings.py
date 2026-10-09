"""Node 0.2 — Pydantic Settings，YAML/ENV 双源配置。

🔗 工程逻辑
    为什么用 Pydantic Settings 而不是纯 YAML 或纯 ENV？
    - ENV 适合秘密（密钥、token），YAML 适合结构化配置（人格、工单规则）
    - Pydantic Settings 自然融合两者：ENV 优先级高于 YAML
    - 启动时立即校验（fail-fast），不运行时才爆炸

🤖 AI 避坑
    - 不要在 Settings 里放动态状态（计数器、连接池引用），它是只读的
    - SecretStr 类型用于密钥，repr 时自动掩码
    - 不要在模块顶层调用 get_settings()，用依赖注入模式
    - lru_cache 的生命周期 = 进程生命周期，进程内单例够用
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OneBotConfig(BaseModel):
    """OneBot v11 连接配置。"""

    ws_url: str = "ws://127.0.0.1:3001"
    access_token: SecretStr | None = None
    reconnect_interval: float = 5.0
    heartbeat_interval: float = 30.0


class LLMConfig(BaseModel):
    """多模型路由配置。"""

    provider: Literal["openai", "anthropic", "deepseek", "ollama", "litellm"] = "litellm"
    model: str = "gpt-4o-mini"
    api_key: SecretStr | None = None
    base_url: str | None = None
    temperature: float = 0.2
    max_tokens: int = 1024


class MemoryConfig(BaseModel):
    """记忆系统配置。"""

    backend: Literal["mem0", "local"] = "local"
    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    neo4j_password: SecretStr | None = None
    embed_model: str = "text-embedding-3-small"
    recall_top_k: int = 5


class AgentConfig(BaseModel):
    """Agent 编排配置。"""

    react_steps: int = 3
    confidence_threshold: float = 0.6
    context_char_budget: int = 4000
    max_concurrent_sessions: int = 200


class MemoryConfig(BaseModel):
    """记忆系统配置。"""

    db_path: str = "data/zhixi_memory.db"      # SqliteSaver 持久化路径
    lorebook_paths: list[str] = Field(default_factory=list)
    mem0_api_key: str | None = None             # 非空时启用 Mem0 后端


class SecurityConfig(BaseModel):
    """分级权限与护栏配置。"""

    admin_token: SecretStr = SecretStr("change-me-in-production")
    permission_model: Literal["open", "standard", "strict"] = "standard"
    blocked_commands: list[str] = Field(default_factory=lambda: [
        "rm -rf /", "mkfs", "dd if=/dev/zero", ":(){ :|:& };:",
    ])


class Settings(BaseSettings):
    """智犀 (ZhiXi) 全局配置。

    加载顺序：默认值 < YAML 文件 < 环境变量（后者覆盖前者）
    """

    model_config = SettingsConfigDict(
        env_prefix="LINGXI_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "lingxi"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    data_dir: Path = Path("./data")
    yaml_config_path: Path | None = None

    onebot: OneBotConfig = Field(default_factory=OneBotConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, v: str | Path) -> Path:
        p = Path(v).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局单例 Settings（lru_cache 保证只构造一次）。"""
    return Settings()
