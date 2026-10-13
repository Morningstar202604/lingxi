"""持久化后端工厂 — 根据配置选择 SQLite / PostgreSQL / Mem0 / Neo4j。

🔗 工程逻辑
    集中创建持久化三件套（tickets / queue / memory），
    避免在 ``__main__.py`` 里重复 if-else。

    通过 ``persistence.backend`` 配置切换：
    - ``"sqlite"`` → 持久化到本地 SQLite（默认，零依赖）
    - ``"postgresql"`` → 连接远程 PostgreSQL（生产）
    - ``"memory"`` → 纯内存（测试）

    工厂返回的三件套可直接替换原有 in-memory 实例，
    pipeline.py 无需任何改动。

    记忆后端通过 ``mem0.enabled`` 切换：
    - True → Mem0MemoryPipeline（Ollama 语义抽取 + Chroma 向量 + 内置图谱）
    - False → 内置管线（SQLite 持久化 或 纯内存 n-gram）

    实体图谱通过 ``neo4j.enabled`` 切换：
    - True → Neo4jGraphStore（Cypher 多跳查询）
    - False → SQLiteGraphStore / InMemoryGraphStore（零运维）

🤖 AI 避坑
    - PostgreSQL 后端在 import 时就要检查 asyncpg 安装
    - DSN 要从 SecretStr 读取，不能硬编码
    - 异步创建方法（``async create_stores``）不能省
    - SQLite 创建是同步的，Postgres 是 async，工厂统一用 async 签名
    - Postgres 连接失败时应显式报错（fail-fast），不静默降级
    - Mem0 / Neo4j 同样 fail-fast：enabled=True 但连不上，启动即报错
"""
from __future__ import annotations

from typing import Any

from lingxi.config import get_settings
from lingxi.core import get_logger

log = get_logger(__name__)


def create_graph_backend(settings: Any = None, graph: Any = None) -> Any:
    """按配置创建实体图谱后端。

    优先级: 显式传入的 graph > neo4j.enabled > 持久化后端类型。
    Mem0 后端自带图谱时传 None（记忆管线与图谱同生命周期）。
    """
    if graph is not None:
        return graph
    if settings is None:
        settings = get_settings()

    if settings.neo4j.enabled:
        from lingxi.memory.neo4j_graph import Neo4jGraphStore

        n4j = settings.neo4j
        graph = Neo4jGraphStore(
            uri=n4j.uri,
            username=n4j.username,
            password=n4j.password.get_secret_value(),
        )
        log.info("graph_backend_selected", backend="neo4j", uri=n4j.uri)
        return graph

    if settings.memory.persistent:
        from lingxi.memory.graph_store import SQLiteGraphStore

        return SQLiteGraphStore(
            str(settings.data_dir / settings.memory.graph_db_path)
        )
    return None


async def create_stores(
    settings: Any = None,
    graph: Any = None,
) -> tuple[Any, Any, Any]:
    """根据配置创建 tickets / queue / memory 三个持久化仓库。

    Args:
        settings: 可选，传入自定义 Settings 实例。默认用全局单例。
        graph: 可选，实体图谱存储。仅 SQLite 后端使用。

    Returns:
        (ticket_store, escalation_queue, memory_pipeline)

    Examples::

        tickets, queue, memory = await create_stores()
    """
    if settings is None:
        settings = get_settings()

    backend: str = settings.persistence.backend

    if backend == "postgresql":
        try:
            return await _create_postgres_stores(settings, graph)
        except Exception as e:
            log.error("postgres_backend_failed", error=str(e))
            raise RuntimeError(
                f"PostgreSQL backend failed to initialize: {e}. "
                f"Check persistence.postgres.dsn and ensure asyncpg is installed."
            ) from e
    elif backend == "memory":
        return _create_memory_stores()
    else:  # "sqlite"
        return _create_sqlite_stores(settings, graph)


def _create_sqlite_stores(settings: Any, graph: Any = None) -> tuple[Any, Any, Any]:
    """创建 SQLite 持久化三件套。"""
    from lingxi.flow.persistence import (
        PersistentEscalationQueue,
        PersistentMemoryPipeline,
        PersistentTicketStore,
        get_or_create_writer,
    )

    db_path: str = settings.memory.db_path
    writer = get_or_create_writer(db_path)

    tickets = PersistentTicketStore(writer)
    esc_queue = PersistentEscalationQueue(writer)
    memory = PersistentMemoryPipeline(writer, graph=graph)

    # hydrate from disk
    tickets._hydrate()
    esc_queue._hydrate()
    memory._hydrate()

    log.info("sqlite_stores_created", db_path=db_path)
    return tickets, esc_queue, memory


def _create_memory_stores() -> tuple[Any, Any, Any]:
    """创建纯内存三件套（写通模式关闭，用于测试）。"""
    from lingxi.flow.queue import EscalationQueue
    from lingxi.flow.ticket import TicketStore
    from lingxi.memory.pipeline import MemoryPipeline

    log.info("memory_stores_created")
    return TicketStore(), EscalationQueue(), MemoryPipeline()


async def _create_postgres_stores(settings: Any, graph: Any = None) -> tuple[Any, Any, Any]:
    """创建 PostgreSQL 持久化三件套（同步包装器，供 pipeline 直接调用）。"""
    from lingxi.flow.postgres import (
        PostgresEscalationQueue,
        PostgresMemoryPipeline,
        PostgresTicketStore,
        PostgresWriter,
        SyncPostgresEscalationQueue,
        SyncPostgresMemoryPipeline,
        SyncPostgresTicketStore,
    )

    pg_cfg = settings.persistence.postgres
    dsn: str = pg_cfg.dsn.get_secret_value()

    # 池必须建在 runner 的后台循环上：同步包装器（SyncPostgres*）都在那个循环
    # 里执行协程，池建在当前调用方的循环上会让每次查询都报
    # "Future attached to a different loop"。
    writer = PostgresWriter.create_on_runner(
        dsn,
        min_size=pg_cfg.pool_min,
        max_size=pg_cfg.pool_max,
    )
    writer.init_schema_on_runner()

    async_tickets = PostgresTicketStore(writer)
    async_queue = PostgresEscalationQueue(writer, ticket_store=async_tickets)
    async_memory = PostgresMemoryPipeline(writer)

    # 包装为同步接口
    tickets = SyncPostgresTicketStore(async_tickets)
    queue = SyncPostgresEscalationQueue(async_queue)
    memory = SyncPostgresMemoryPipeline(async_memory, graph=graph)

    # 关联 queue 与 tickets（同步包装器层面）
    queue.attach_store(tickets)

    log.info("postgres_stores_created")
    return tickets, queue, memory


# Public aliases (convenience wrappers)
create_memory_stores = _create_memory_stores


__all__ = [
    "create_stores",
    "create_memory_stores",
    "_create_sqlite_stores",
    "_create_memory_stores",
    "_create_postgres_stores",
]
