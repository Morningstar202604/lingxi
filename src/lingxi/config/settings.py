"""Node 0.2 — Pydantic Settings，YAML/ENV 双源配置。

🔗 工程逻辑
    为什么用 Pydantic Settings 而不是纯 YAML 或纯 ENV？
    - ENV 适合秘密（密钥、token），YAML 适合结构化配置（人格、工单规则）
    - Pydantic Settings 自然融合两者：ENV 优先级高于 YAML
    - 启动时立即校验（fail-fast），不运行时才爆炸

    env 文件还有个数据目录候选（`$LINGXI_DATA_DIR/lingxi.env`）：
    桌面版由 Tauri 壳注入 LINGXI_DATA_DIR，用户在「模型设置」页写的云端
    API Key 存这里，双击 App 即可生效，不需要碰项目目录。

🤖 AI 避坑
    - 不要在 Settings 里放动态状态（计数器、连接池引用），它是只读的
    - SecretStr 类型用于密钥，repr 时自动掩码
    - 不要在模块顶层调用 get_settings()，用依赖注入模式
    - lru_cache 的生命周期 = 进程生命周期，进程内单例够用
    - 多 env 文件时 pydantic-settings 是"后者覆盖前者"，lingxi.env 放后面
"""
import os
from functools import cache
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_file_candidates() -> tuple[str, ...]:
    """env 文件候选（后者覆盖前者）：项目根 .env + 数据目录 lingxi.env。"""
    files = [".env"]
    data_dir = os.environ.get("LINGXI_DATA_DIR", "")
    if data_dir:
        files.append(str(Path(data_dir) / "lingxi.env"))
    return tuple(files)


class PostgresConfig(BaseModel):
    """PostgreSQL 连接配置 (可选后端)。"""

    dsn: SecretStr = SecretStr("postgresql://zhixi:zhixi@localhost:5432/zhixi")
    pool_min: int = 2
    pool_max: int = 10


class Neo4jConfig(BaseModel):
    """Neo4j 图谱后端配置 (可选, 大规模多跳查询)。"""

    enabled: bool = False
    uri: str = "bolt://127.0.0.1:7687"
    username: str = "neo4j"
    password: SecretStr = SecretStr("zhixi_neo4j")


class Mem0Config(BaseModel):
    """Mem0 语义记忆后端配置 (可选, 自动事实抽取 + 向量召回 + 遗忘)。

    启用前提: 安装 mem0ai extra + 可访问的 embedding 提供方
    (默认 ollama: nomic-embed-text)。未装依赖或后端不可达时
    配置校验 fail-fast 报错, 不静默降级。
    """

    enabled: bool = False
    # embedding 提供方: "ollama" (本地, 配合 Ollama 服务) | "openai"
    embed_provider: Literal["ollama", "openai"] = "ollama"
    embed_model: str = "nomic-embed-text"
    # ollama 服务地址（原生 API 根，不带 /v1 —— mem0 的 ollama 客户端
    # 自己拼 /api/tags、/api/embed；带 /v1 会 404。构造时会自动去掉尾缀 /v1）
    base_url: str = "http://127.0.0.1:11434"
    api_key: SecretStr | None = None
    # 记忆抽取 LLM (默认跟 llm 配置走 ollama)
    llm_model: str = "qwen2.5:7b"
    # 向量库: 本地 chroma 文件 (相对 data_dir)
    collection: str = "zhixi_mem0"
    history_db: str = "mem0_history.db"


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
    """多模型路由配置。

    🔗 工程逻辑
        默认使用 Ollama 本地模型，零配置即可跑通；用户可通过环境变量
        切换到 OpenAI/DeepSeek 等云端模型。litellm 作为统一网关，
        统一处理各家 API 差异，Ollama 作为零配置 fallback。

    优先级：环境变量 > YAML > 默认值（Ollama 本地）
    """

    provider: Literal["openai", "anthropic", "deepseek", "ollama", "litellm"] = "ollama"
    model: str = "qwen2.5:7b"
    api_key: SecretStr | None = None
    base_url: str | None = "http://127.0.0.1:11434/v1"
    temperature: float = 0.2
    max_tokens: int = 1024

    # 备用模型链（按优先级尝试），用于主模型失败时的自动降级
    fallback_models: list[str] = Field(default_factory=lambda: [
        "qwen2.5:7b", "llama3.1:8b", "mistral:7b"
    ])


class LLMCacheConfig(BaseModel):
    """LLM 响应缓存（litellm 官方 cache）。

    🔗 工程逻辑
        相同 prompt 短时间内重复调用（重试、多用户问同样的问题、
        开发期反复调试）直接命中缓存，省 token 也省延迟。
        本地模型（ollama）收益最大：磁盘换 CPU。

    默认 disk 后端（零外部依赖）；有 Redis 时切 redis（多进程共享）。
    """

    enabled: bool = False
    backend: Literal["disk", "redis"] = "disk"
    ttl_seconds: int = 3600
    disk_dir: str = ".litellm_cache"
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379


class RateLimitConfig(BaseModel):
    """HTTP 限流（slowapi 官方库）。

    🔗 工程逻辑
        只限制"重"端点（LLM 推理 / 知识库写入），读类轮询端点不限 ——
        否则界面自己 3 秒一次的轮询就会把额度用光。

    额度按"单人正常操作上限"给：heavy=30/分钟 意味着一个用户最快
    30 次/分钟的推理请求也够用（真实对话远低于此），而脚本刷会被挡住。
    """

    enabled: bool = True
    heavy: str = "30/minute"
    light: str = "120/minute"


class EmbeddingConfig(BaseModel):
    """Embedding 向量配置 (用于 RAG 知识库)。

    🔗 工程逻辑
        默认**跟随主 LLM 的 provider**（`model` 留空表示"自动"）：
        - provider=ollama（零配置默认）→ 用本地 `nomic-embed-text`，
          与主模型同一服务、同一个 host，无需任何额外配置
        - provider=云端（openai/deepseek/...）→ 用该家的 embedding 端点

        此前默认写死 `text-embedding-3-small`（OpenAI 云端）：本地部署
        或未配 OpenAI key 时，embedding 调用失败并被静默降级成零向量 ——
        RAG 看起来"能用"，实际召回全是噪声（AUDIT 已知限制之一）。

    🤖 AI 避坑
        - `model` 空串 ≠ 配错：它是"自动推导"的显式信号。
        - 自动推导只在构造时发生一次；运行期改 settings 不生效（保持可预测）。
    """

    # 空 = 跟随 llm.provider 自动选择；显式填写则优先
    model: str = ""
    base_url: str | None = None
    api_key: SecretStr | None = None
    dimensions: int = 1536  # 首次 embedding 后自动覆盖
    chunk_size: int = 500  # token
    chunk_overlap: float = 0.1  # 10%

    # 各 provider 的默认 embedding 模型（自动推导用）
    _PROVIDER_DEFAULTS: ClassVar[dict[str, str]] = {
        "ollama": "ollama/nomic-embed-text",
        "openai": "openai/text-embedding-3-small",
        "deepseek": "openai/text-embedding-3-small",  # DeepSeek 无自有 embedding，走兼容端点
        "litellm": "openai/text-embedding-3-small",
        "anthropic": "openai/text-embedding-3-small",  # Anthropic 无 embedding API
    }

    def resolved_model(self, llm_provider: str) -> str:
        """解析出实际使用的 embedding 模型（含 litellm 前缀）。"""
        if self.model:
            return self.model
        return self._PROVIDER_DEFAULTS.get(
            llm_provider, "openai/text-embedding-3-small"
        )

    def resolved_base_url(self, llm_provider: str, llm_base_url: str | None) -> str:
        """解析 base_url：显式配置优先；否则跟随主 LLM。

        实测（2026-10-15，本地 Ollama 0.40.3）：litellm 的 ollama embedding
        走**原生 API**（/api/embed），带 `/v1` 的 URL 一律 404
        ——与 chat 同一个坑（见 agent/llm.resolve_base_url）。
        """
        if self.base_url:
            return self.base_url
        if llm_provider == "ollama":
            base = (llm_base_url or "http://127.0.0.1:11434").rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            return base
        return llm_base_url or ""


class KnowledgeConfig(BaseModel):
    """知识库 RAG 配置。"""

    enabled: bool = True
    persist_dir: str = "./data/chroma"
    collection: str = "zhixi_default"
    top_k: int = 5
    score_threshold: float = 0.3
    max_context_chars: int = 2000

    # ── rerank（召回后 cross-encoder 重排）──
    # RAG 质量最高性价比的单点提升：向量召回是双塔（丢交互信息），
    # cross-encoder 让 query/doc 逐 token 交互。用本地 flashrank（无 API key）。
    # 依赖缺失/模型下载失败自动降级为原序，不影响检索可用性。
    rerank_enabled: bool = True
    rerank_model: str = "ms-marco-MiniLM-L-12-v2"


class MemoryConfig(BaseModel):
    """记忆系统配置。

    后端为内置 SQLite 管线（local）。Mem0 / Neo4j 后端在本版本**没有实现**，
    相关的配置项已删除 —— 与其留一堆配了也静默失效的死配置，不如让配置面
    与真实能力一致（见 docs/AUDIT-2026-10-10.md 第四章 4.4）。
    """

    # ── SQLite 持久化（tickets / queue / memory 共享一张 DB 文件）──
    # 相对于 settings.data_dir 解析（见 __main__.py 的 data_dir / db_path）。
    # 此处不要再带 "data/" 前缀，否则会拼成 data/data/xxx.db。
    db_path: str = "zhixi_memory.db"
    persistent: bool = True  # 启用写通式持久化 (False 则纯内存, 用于测试)
    # 实体图谱库（相对 data_dir）。默认与业务数据分文件，
    # 避免图谱重建时锁住正在写工单的那张表。
    graph_db_path: str = "zhixi_graph.db"

    # ── Lorebook YAML 加载路径 ──
    lorebook_paths: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    """Agent 编排配置。"""

    react_steps: int = 3
    confidence_threshold: float = 0.6
    context_char_budget: int = 4000
    max_concurrent_sessions: int = 200
    # 工具调用（function calling）总开关。关掉则不向模型下发 tools=，
    # 模型只能返回文本 —— 用于不支持 function calling 的私有模型端点。
    tool_calling: bool = True

    # ── 可靠性 ──
    # LLM 调用失败重试。0 = 不重试（旧的脆弱行为：一次抖动就转人工）。
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_retry_base_delay: float = 0.5  # 秒，指数退避基数

    # ── 上下文控制 ──
    # 单次工具结果注入 prompt 的上限。超了会挤爆上下文且放大注入风险。
    tool_result_max_chars: int = Field(default=2000, ge=200)
    # 连续相同工具调用超过该次数即短路（模型陷入循环时的护栏）
    max_identical_tool_calls: int = Field(default=2, ge=1)


class SecurityConfig(BaseModel):
    """分级权限与护栏配置。

    🔗 工程逻辑
        default_permission_level 是所有会话的基线等级；
        permission_rules 允许**按用户/会话**单独提权或降权，
        key 可以是 user_id、chat_id 或 "@all"。取值优先 user_id > chat_id > @all。
        例::

            LINGXI_SECURITY__PERMISSION_RULES='{"u_vip_001": 2, "wxid_abc": 0}'

        命中规则即视为该会话的权限等级（覆盖基线），由
        ``lingxi.persona.guardrails.resolve_permission_level`` 解析。
    """

    admin_token: SecretStr = SecretStr("change-me-in-production")
    permission_model: Literal["open", "standard", "strict"] = "standard"

    # 本机免密模式（仅在 Dashboard 只监听 loopback 时生效）。
    #
    # 背景：AUDIT-2026-10-10 #8 把"未配置 admin_token"改成 fail-closed，
    # 安全上正确，但默认部署 `--dashboard-host 127.0.0.1` 也被打死了 ——
    # 用户双击 exe、什么都不配，看到的却是满屏 503。
    #
    # 切分依据是**暴露面**而不是"有没有配置"：
    # - 只监听 127.0.0.1/::1 → 服务在网络上不可达，免密是产品的一部分
    # - 监听 0.0.0.0 → 工单/认领/关闭暴露给同网段任何主机，必须配 token
    #
    # 配了 token 的部署一律照常校验；这里只影响"压根没配"这一种情况。
    # 需要收紧（哪怕只跑本机也要求凭据）时置 False。
    allow_anonymous_local: bool = True

    # ── CORS（前端直连后端时必需）──
    #
    # 为什么前端不再走 Next rewrites 代理：`/api/events` 是 SSE 长连接，
    # 而 Next dev 的 rewrites 会缓冲响应体（实测：浏览器拿到 200
    # text/event-stream 却一帧都收不到，urllib 直连同一端点立即收到
    # `event: connected`）。代理只对普通请求友好，对流式是死路。
    # 改成前端直连 + 后端开 CORS，dev 与 Tauri 两条路径行为一致。
    #
    # 默认只放行 loopback 来源：Dashboard 默认也只监听 loopback，两侧对齐。
    # 要给局域网的前端访问，把具体 origin 加进 cors_origins。
    cors_allow_loopback: bool = True
    cors_origins: list[str] = Field(default_factory=list)
    blocked_commands: list[str] = Field(default_factory=lambda: [
        "rm -rf /", "mkfs", "dd if=/dev/zero", ":(){ :|:& };:",
    ])

    # ── 分级权限：按会话给到（不再是进程级全局常量）──
    default_permission_level: int = Field(default=1, ge=0, le=4)
    permission_rules: dict[str, int] = Field(default_factory=dict)


class AuthConfig(BaseModel):
    """JWT RBAC 鉴权配置。

    🔗 工程逻辑
        启用鉴权时密钥**必须由部署方提供**，且不允许是历史默认值。
        旧默认值 `change-me-in-production-please-use-strong-secret` 是源码里公开
        的常量，而签发端（auth/router.py）当时不做任何校验 —— 任何读过源码的
        人都能伪造 admin JWT 直通所有管理端点。现在改为 fail-closed：
        enabled=True 却配不出合格密钥，启动即报错，而不是悄悄用弱密钥签发。

    🤖 AI 避坑
        - 不要把"默认值 + 运行时才检查"当成防护：检查必须落在配置模型上，
          否则任何绕过装配直接读 settings 的路径（测试、脚本）都会继承弱默认。
        - jwt_secret 保留 None 而不是给个占位串：占位串本身就是凭据。
    """

    # HS256 密钥；enabled=True 时必填且需满足 _MIN_SECRET_BYTES
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    enabled: bool = False  # 默认不启用，按需开启

    # 引导管理员口令。为空则不播种任何用户 —— 不存在内置 admin/admin123。
    # 用户名固定为 admin；要建其他账号请用 /auth/users（需 USER_MANAGE）。
    admin_username: str = "admin"
    admin_password: SecretStr | None = None

    # API Key 认证：{username: sha256(key) 的十六进制}。
    # 空 dict = API Key 这条认证路径整体关闭（不再接受任何 X-API-Key）。
    # 刻意不在源码里放明文 key —— 旧的 zxk_admin_xxx 等同后门口令。
    api_key_hashes: dict[str, str] = Field(default_factory=dict)

    # 历史上出现在源码/文档里的弱默认值，一律视为未配置
    _BANNED_SECRETS: ClassVar[frozenset[str]] = frozenset({
        "change-me-in-production-please-use-strong-secret",
        "change-me-in-production",
        "secret",
        "changeme",
    })
    _MIN_SECRET_BYTES: ClassVar[int] = 32

    @model_validator(mode="after")
    def _enforce_secret_when_enabled(self) -> "AuthConfig":
        if not self.enabled:
            return self
        secret = self.jwt_secret
        if secret is None or secret.strip() == "":
            raise ValueError(
                "auth.enabled=True 但 jwt_secret 未配置。请设置 "
                "LINGXI_AUTH__JWT_SECRET（≥32 字节随机串）。"
            )
        if secret in self._BANNED_SECRETS:
            raise ValueError(
                "auth.jwt_secret 是已知的公开默认值，拒绝用它签发 token。"
            )
        if len(secret.encode("utf-8")) < self._MIN_SECRET_BYTES:
            raise ValueError(
                f"auth.jwt_secret 只有 {len(secret.encode('utf-8'))} 字节，"
                f"HS256 至少需要 {self._MIN_SECRET_BYTES} 字节。"
            )
        if self.admin_password is None or not self.admin_password.get_secret_value():
            raise ValueError(
                "auth.enabled=True 但 admin_password 未配置，"
                "系统将没有任何可登录账号。请设置 LINGXI_AUTH__ADMIN_PASSWORD。"
            )
        return self


class MetricsConfig(BaseModel):
    """Prometheus 指标配置。"""

    enabled: bool = True
    path: str = "/metrics"
    # Prometheus 抓取专用凭据。为空则回退用 security.admin_token。
    # /metrics 会暴露内部端点名与状态码分布，绝不能匿名可读
    # （见 docs/AUDIT-2026-10-10.md #7）。
    scrape_token: SecretStr | None = None


class WebhookConfig(BaseModel):
    """出站 Webhook 签名配置。"""

    hmac_secret: SecretStr | None = None  # None 时不签名（兼容旧规则）
    timeout: int = 5
    max_retries: int = 2
    # 入站 HMAC 验签密钥。None = 入站验签端点整体关闭（404）。
    # 与出站共用一把 secret 即可双向对称；生产建议用不同值。
    inbound_hmac_secret: SecretStr | None = None
    # 入站时间戳容差（防重放窗口），默认 5 分钟
    inbound_max_age_seconds: int = 300


class Settings(BaseSettings):
    """智犀 (ZhiXi) 全局配置。

    加载顺序：默认值 < YAML 文件 < 环境变量（后者覆盖前者）
    """

    model_config = SettingsConfigDict(
        env_prefix="LINGXI_",
        env_file=_env_file_candidates(),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "lingxi"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    data_dir: Path = Path("./data")

    onebot: OneBotConfig = Field(default_factory=OneBotConfig)
    gewechat: GewechatConfig = Field(default_factory=GewechatConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    llm_cache: LLMCacheConfig = Field(default_factory=LLMCacheConfig)
    ratelimit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    mem0: Mem0Config = Field(default_factory=Mem0Config)

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
