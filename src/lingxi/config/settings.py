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
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class PostgresConfig(BaseModel):
    """PostgreSQL 连接配置 (可选后端)。"""

    dsn: SecretStr = SecretStr("postgresql://zhixi:zhixi@localhost:5432/zhixi")
    pool_min: int = 2
    pool_max: int = 10


class PersistenceConfig(BaseModel):
    """持久化后端选择配置。"""

    backend: Literal["sqlite", "postgresql", "memory"] = "sqlite"
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)


class OneBotConfig(BaseModel):
    """OneBot v11 连接配置 (NapCat for QQ)。"""

    ws_url: str = "ws://127.0.0.1:3001"
    access_token: SecretStr | None = None
    reconnect_interval: float = 5.0
    heartbeat_interval: float = 30.0


class GewechatConfig(BaseModel):
    """Gewechat 微信适配配置。

    Gewechat 通过 HTTP REST API 控微信，消息走 HTTP 回调推送。
    需配合独立的 Gewechat 客户端运行在用户本机。
    """

    enabled: bool = False
    api_base: str = "http://127.0.0.1:2531"
    token: str = ""
    app_id: str = ""
    callback_path: str = "/api/gewechat/callback"
    callback_token: SecretStr | None = None
    verify_token: bool = False


class LLMConfig(BaseModel):
    """多模型路由配置。"""

    provider: Literal["openai", "anthropic", "deepseek", "ollama", "litellm"] = "litellm"
    model: str = "gpt-4o-mini"
    api_key: SecretStr | None = None
    base_url: str | None = None
    temperature: float = 0.2
    max_tokens: int = 1024


class EmbeddingConfig(BaseModel):
    """Embedding 向量配置 (用于 RAG 知识库)。"""

    model: str = "text-embedding-3-small"
    base_url: str | None = None
    api_key: SecretStr | None = None
    dimensions: int = 1536  # 首次 embedding 后自动覆盖
    chunk_size: int = 500  # token
    chunk_overlap: float = 0.1  # 10%


class KnowledgeConfig(BaseModel):
    """知识库 RAG 配置。"""

    enabled: bool = True
    persist_dir: str = "./data/chroma"
    collection: str = "zhixi_default"
    top_k: int = 5
    score_threshold: float = 0.3
    max_context_chars: int = 2000


class MemoryConfig(BaseModel):
    """记忆系统配置。

    支持三种后端（可组合）：
    - local    → SQLite + 内置管线（默认，零依赖跑通）
    - mem0     → Mem0 语义召回（需 mem0ai + embedding provider）
    - neo4j    → Neo4j 实体图谱（需 neo4j 驱动）
    """

    # ── 后端选择 ──
    backend: Literal["local", "mem0", "neo4j"] = "local"
    mem0_api_key: str | None = None             # 非空时启用 Mem0 语义召回
    embed_model: str = "text-embedding-3-small"
    recall_top_k: int = 5

    # ── SQLite 持久化（tickets / queue / memory 共享一张 DB 文件）──
    # 相对于 settings.data_dir 解析（见 __main__.py 的 data_dir / db_path）。
    # 此处不要再带 "data/" 前缀，否则会拼成 data/data/xxx.db。
    db_path: str = "zhixi_memory.db"
    persistent: bool = True  # 启用写通式持久化 (False 则纯内存, 用于测试)

    # ── Lorebook YAML 加载路径 ──
    lorebook_paths: list[str] = Field(default_factory=list)

    # ── Neo4j 图谱（生产环境大规模实体关系）──
    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    neo4j_password: SecretStr | None = None


class AgentConfig(BaseModel):
    """Agent 编排配置。"""

    react_steps: int = 3
    confidence_threshold: float = 0.6
    context_char_budget: int = 4000
    max_concurrent_sessions: int = 200


class SecurityConfig(BaseModel):
    """分级权限与护栏配置。"""

    admin_token: SecretStr = SecretStr("change-me-in-production")
    permission_model: Literal["open", "standard", "strict"] = "standard"
    blocked_commands: list[str] = Field(default_factory=lambda: [
        "rm -rf /", "mkfs", "dd if=/dev/zero", ":(){ :|:& };:",
    ])


class AuthConfig(BaseModel):
    """JWT RBAC 鉴权配置。"""

    jwt_secret: str = "change-me-in-production-please-use-strong-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    enabled: bool = False  # 默认不启用，按需开启


class MetricsConfig(BaseModel):
    """Prometheus 指标配置。"""

    enabled: bool = True
    path: str = "/metrics"


class WebhookConfig(BaseModel):
    """出站 Webhook 签名配置。"""

    hmac_secret: SecretStr | None = None  # None 时不签名（兼容旧规则）
    timeout: int = 5
    max_retries: int = 2


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
    gewechat: GewechatConfig = Field(default_factory=GewechatConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, v: str | Path) -> Path:
        p = Path(v).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p


@cache
def get_settings() -> Settings:
    """获取全局单例 Settings（@cache 保证只构造一次）。"""
    return Settings()
