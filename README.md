# 智犀 ZhiXi

> 智辨已知，犀照善为。

智犀 (ZhiXi) v0.2.0 — 7×24 小时全栈数字员工引擎，框架代号 **lingxi**。
部署在自有服务器，通过 QQ/微信触达用户，具备深度人格、长期记忆与分级管控的工具调用框架。

> ⚠️ **当前版本状态**：框架骨架、IM 桥接、人格引擎、业务管线与控制台可用；
> **Agent 的多步推理与工具调用链路尚未接通**，Mem0/Neo4j/MCP 三原语/按用户分级权限
> 尚未实现。逐条实测依据见下节「[实现状态](#实现状态2026-10-11-实测校正)」。

## 核心能力

### 基础架构 (Layer 0-6)

| 模块 | 状态 | 说明 |
|---|---|---|
| Layer 0 基础地基 | ✅ | pyproject + 协议模型 + structlog + Pydantic Settings |
| Layer 1 IM 桥接 | ✅ | NapCat (QQ) + Gewechat (微信) → 统一 IMEvent |
| Layer 2 人格引擎 | ✅ | 13 模块 Persona + 注入管线 + 防注入护栏 |
| Layer 3 记忆系统 | ⚠️ | 5 段式中「编码/存储/召回」3 段已接入生产；「更新/遗忘」2 段已实现但无调用方 |
| Layer 4 Agent 编排 | ❌ | LangGraph 图已组装，**但工具调用链路未接通**（见下「实现状态」）；权限等级为全局常量，非按用户分级 |
| Layer 5 业务管线 | ✅ | 工单/队列/升级 + 规则引擎 + SLA + 审计日志 |
| Layer 6 控制台 | ⚠️ | Next.js 9 路由；8 个页面真实取数，persona 页为占位 |

### v0.2.0 新增模块

| 模块 | 状态 | 说明 |
|---|---|---|
| Webhook HMAC-SHA256 出站签名 | ⚠️ | `security/hmac.py` 出站签名可用；**入站 `verify_webhook_signature` 无任何调用点**（验签端点未实现） |
| JWT RBAC | ⚠️ | JWT 本体与 4 角色×10 权限已实现，但 `auth.enabled` 默认关闭，Dashboard 业务端点走独立的 `x-admin-token` 单因子；**多租户只有字段、无查询隔离** |
| A/B 测试框架 | ❌ | `experiment/` 7 个 REST 端点已实现，但 `__main__.py` 从不构造 `ExperimentEngine`，真实部署下路由未挂载 → 前端实验页请求 `/api/experiments` 必然 404 |
| Prometheus 指标 | ✅ | 12 指标 + ASGI 中间件 + `/metrics`（注：该端点当前无鉴权） |
| SSE 流式推理 | ⚠️ | `/api/events`（升级/认领/关闭事件流）已挂载可用；但 `streaming/sse.py` 的 7 事件 Agent 推流端点**在真实进程里从未挂载**（见下第 6 条），且其中 `tool_call`/`tool_result` 因工具链路断裂本就不可达 |
| 可插拔持久化 | ⚠️ | SQLite + Memory 后端在用；**PostgreSQL 后端（`flow/postgres.py` 631 行）未接入默认启动路径**，且其 async 接口与 pipeline 的同步调用不兼容 |

## 实现状态（2026-10-11 实测校正）

以下条目此前被描述为「已具备」，经逐条验证后据实修正。**保留本节直至对应项真正完成。**

1. **Agent 不会调用工具**（最严重）。`agent/nodes.py:92` 调用 `litellm.acompletion` 时**不传 `tools=`**（全仓 `src/` 下 `tools=` 零命中），因此 `nodes.py:102/163` 的结构化 `tool_calls` 分支永不触发；文本 JSON 契约只写在 docstring 里，`persona/injector.py` 与 `flow/pipeline.py` 从不把工具清单拼进 prompt。实际路径是 `nodes.py:204` 把模型散文当作 `final_answer` 一步 respond —— **README 宣称的「多步推理与工具调用」在生产中不生效**。修复归属 M1。
2. **Mem0 / Neo4j 零实现**。全仓无 `import mem0` / `import neo4j` / `import networkx`；`settings.py` 的 `memory.backend`（`local|mem0|neo4j`）**没有任何代码读取它**，配置了也无效。`memory/graph_store.py` 只有 `InMemoryGraphStore`，且除 `__init__` 导出外无使用点（重启即丢）。
3. **MCP 只实现 1 个原语**。`mcp/server.py:46,54` 仅 `tools/list` + `tools/call`，无 resources、无 prompts、无 `initialize` 能力协商；Agent 侧也不是 MCP client（进程内直调）。故「MCP 工具协议三原语」表述不准确。
4. **L0-L4 分级权限的等级来源是全局常量**。`__main__.py:65` 写死 `_PERMISSION_LEVEL = 1`，而 `flow/pipeline.py:179` 的 `current_permission` 参数标注 `# noqa: ARG002` 完全未使用 → 不存在按用户/会话分级。
5. **向量检索在默认配置下静默失效**。`knowledge/embedder.py:69-71` 在调用失败时**降级为零向量**，而默认未配 API key（`settings.py:79`）→ 所有向量为零、cosine 无意义，只留一条 warning。另外 `vector_store.py:208,262` 的 BM25 索引与 `_chunk_map` 只存在于内存且无 hydrate，**重启后 hybrid/bm25 召回返回空**；生产默认 `mode="vector"`，因此「RRF/hybrid」在线上从未被走到。

修复优先级与验收标准见 `docs/AUDIT-2026-10-10.md` 与仓库任务列表（M0→M4）。

以下两条由**真实启动 + HTTP 探测**发现（不是读代码推断），已实测复算：

6. **两个横切模块在真实进程里根本没挂载**。`__main__.py:100-106` 调用
   `create_dashboard_app(...)` 时既未传 `agent=`，也未传 `experiment_engine=`，
   而 `dashboard_api.py` 是用 `if agent is not None` /
   `if experiment_engine is not None` 来决定是否注册路由的。
   实跑 `python -m lingxi --persistent --dashboard-port 3921` 后拉 `/openapi.json`，
   挂载的 24 条路由里**没有** `/api/stream/chat`、没有
   `/api/sessions/{chat_id}/stream`、也没有任何 `/api/experiments*`；
   而前端 `web/lib/api.ts:383-450` 有 6 处请求 `/api/experiments`，
   实验页在真实部署下必然 404。单元测试之所以发现不了，是因为它们自己
   用 `create_dashboard_app(...)` 显式传了这些参数。
7. **`/metrics` 无鉴权**。同一实跑中匿名 `GET /metrics` 返回 200，
   可读到内部端点名与状态码分布（含 404 探测痕迹）。

## 开源复用

| 部件 | 开源方案 |
|---|---|
| QQ 桥 | NapCat (OneBot v11) |
| 微信桥 | Gewechat (OneBot 兼容) |
| 工具协议 | MCP (Model Context Protocol) |
| Agent 编排 | LangGraph |
| 长期记忆 | 内置管线（可升级 Mem0） |
| 知识图谱 | 内置图谱（可升级 Neo4j） |
| 多模型路由 | LiteLLM |
| 角色卡标准 | SillyTavern Character Card V3 |
| 人格引擎参考 | Hermes Persona 13 模块 |
| JWT 鉴权 | PyJWT |
| 指标采集 | prometheus_client |
| 流式推送 | sse-starlette |
| PostgreSQL 驱动 | asyncpg |

> 不重复造轮子。仅针对 IM 桥接、人格护栏、记忆管线、分级权限等核心差异化部件自研。

## 快速开始

```bash
# 1. 安装依赖（需要 Python 3.11+）
pip install -e ".[dev]"

# 2. 运行测试（395+ tests）
pytest tests/ -v

# 3. 启动引擎（开发模式，需已运行 NapCat）
LINGXI_ONEBOT__WS_URL=ws://127.0.0.1:3001 python -m lingxi
# PowerShell: $env:LINGXI_ONEBOT__WS_URL="ws://127.0.0.1:3001"; python -m lingxi

# 4. 控制台（另开终端）
cd web && npm run dev      # http://localhost:3000
```

## 项目结构

```
lingxi/
├── docs/BLUEPRINT.md              # 完整架构蓝图 + 路线图
├── src/lingxi/
│   ├── __main__.py                # 引擎入口（装配 + 启动序列）
│   ├── core/                      # Layer 0: 协议模型 + 日志 + trace_id
│   │   ├── protocol.py            #   OneBot v11 → 统一 IMEvent
│   │   ├── logging.py             #   structlog + 链路追踪
│   │   └── settings.py            #   Pydantic Settings 配置
│   ├── im/                        # Layer 1: IM 桥接
│   │   ├── adapter.py             #   IMAdapter 基类 + CQ 码解析
│   │   ├── napcat.py              #   NapCat (QQ) WebSocket 适配器
│   │   └── gewechat.py            #   Gewechat (微信) HTTP 回调适配器
│   ├── persona/                   # Layer 2: 人格引擎
│   │   ├── schema.py              #   13 模块 Persona 配置
│   │   ├── injector.py            #   System prompt 注入管线
│   │   └── guardrails.py          #   注入检测 + L0-L4 权限
│   ├── memory/                    # Layer 3: 记忆系统
│   │   ├── pipeline.py            #   5 段式（编码→存储→召回→更新→遗忘）
│   │   ├── lorebook.py            #   关键词触发的世界观按需注入
│   │   └── graph_store.py         #   实体图谱抽象 + 内存实现
│   ├── agent/                     # Layer 4: LangGraph 编排
│   │   ├── state.py               #   AgentState TypedDict
│   │   ├── nodes.py               #   reason / execute_tool / escalate / respond
│   │   ├── tools.py               #   MCP 工具注册表 + 分级权限总线
│   │   └── graph.py               #   状态机组装 + Checkpoint 持久化
│   ├── flow/                      # Layer 5: 业务管线
│   │   ├── pipeline.py            #   IM 事件 → Agent → 回复/升级 主循环
│   │   ├── ticket.py              #   工单系统 + 状态机 + 自动分类
│   │   ├── queue.py               #   人工坐席升级队列（优先级调度）
│   │   ├── rules.py               #   规则引擎
│   │   ├── sla.py                 #   SLA 监控
│   │   ├── audit.py               #   审计日志
│   │   ├── dashboard_api.py       #   FastAPI Dashboard 接口
│   │   ├── persistence.py          #   持久化抽象接口
│   │   ├── persistence_factory.py  #   三后端工厂（SQLite/Postgres/Memory）
│   │   └── postgres.py           #   asyncpg 连接池 + 原子出队
│   ├── knowledge/                 # 知识库
│   │   ├── embedder.py            #   向量化嵌入
│   │   ├── service.py             #   知识检索服务
│   │   ├── splitter.py            #   文档切片
│   │   └── vector_store.py        #   向量存储
│   ├── auth/                      # v0.2.0: JWT RBAC
│   │   ├── jwt.py                 #   Bearer JWT 签发/验证
│   │   ├── models.py              #   用户/角色/权限模型
│   │   ├── deps.py                #   FastAPI 依赖注入
│   │   └── router.py              #   /auth/* REST 端点
│   ├── metrics/                   # v0.2.0: Prometheus 指标
│   │   ├── collector.py           #   12 指标定义
│   │   └── middleware.py          #   ASGI 自动采集中间件
│   ├── experiment/                # v0.2.0: A/B 测试
│   │   ├── engine.py              #   MD5-salt 分流 + z-test 判定
│   │   ├── models.py              #   实验/变体/结果模型
│   │   └── router.py              #   /experiment/* REST 端点
│   ├── streaming/                 # v0.2.0: SSE 流式
│   │   └── sse.py                 #   7 事件类型 SSE 推送
│   └── security/                  # 安全
│       ├── hmac.py                #   Webhook HMAC-SHA256 出站签名
│       └── pii.py                 #   PII 检测与脱敏
├── web/                           # Layer 6: Next.js 控制台
│   ├── app/                       #   9 routes (index, audit, knowledge, logs, persona, queue, sessions, tickets, _not-found)
│   ├── components/                #   MetricsGrid / QueuePanel / TicketsTable / Sidebar / Clock
│   └── lib/api.ts                 #   Dashboard API 客户端
├── tests/                         # 395 tests 全部通过
│   ├── conftest.py                #   共享 fixtures
│   ├── test_layer0.py             #   ~19 tests ✅
│   ├── test_layer1.py             #   ~18 tests ✅
│   ├── test_layer2.py             #   ~18 tests ✅
│   ├── test_layer3.py             #   ~18 tests ✅
│   ├── test_layer4.py             #   ~35 tests ✅
│   ├── test_layer5.py             #   ~35 tests ✅
│   ├── test_layer6.py             #   ~10 tests ✅
│   ├── test_knowledge.py          #   ~19 tests ✅
│   ├── test_phase2.py             #   ~21 tests ✅
│   ├── test_phase3.py             #   ~51 tests ✅
│   ├── test_e2e_smoke.py          #   全链路 + 入口冒烟 ✅
│   ├── test_hmac.py              #   13 tests ✅
│   ├── test_auth.py              #   27 tests ✅
│   ├── test_metrics.py           #   11 tests ✅
│   ├── test_experiment.py        #   22 tests ✅
│   └── test_streaming.py         #    7 tests ✅
└── pyproject.toml                 # uv/hatch + ruff + mypy + pytest
```

## 品牌规范

| 项 | 定义 |
|---|---|
| **名称** | 智犀 ZhiXi |
| **Tagline** | 智辨已知，犀照善为 |
| **版本** | v0.2.0 |
| **主色** | 深靛青 `#1B2A4A` + 琥珀橙 `#E8963E` + 翠玉绿 `#2ECC71` |
| **辅助色** | 浅灰 `#F5F5F5`（背景）+ 深灰 `#333333`（正文） |
| **图形符号** | 犀角（智犀之角 = 智照善行的天线）× 电路纹 |
| **字体** | 中文：思源宋体 Serif / 英文：Inter Sans |
| **风格** | 精密、温暖、专业 |

## 分层开发

每层独立可运行、引用前序层符号、绝不重复导入。

```
Layer 0 ✅ 基础地基 (pyproject + 协议 + 日志 + 配置)         → ~19 tests
Layer 1 ✅ IM 桥接   (NapCat + Gewechat + 统一 IMEvent)      → ~18 tests
Layer 2 ✅ 人格引擎  (13 模块 + 注入管线 + 护栏)              → ~18 tests
Layer 3 ⚠️ 记忆系统  (5 段式管线 + Lorebook + 实体图谱)        → ~18 tests
Layer 4 ❌ Agent 编排 (LangGraph ReAct + MCP + 分级权限)       → ~35 tests
Layer 5 ✅ 业务管线  (工单/队列/升级 + 规则引擎 + SLA + 审计)  → ~35 tests
Layer 6 ⚠️ 控制台    (Next.js 9 routes)

# ⚠️/❌ 的含义（详见「实现状态」）：
#   Layer 3  编码/存储/召回已接入；更新/遗忘已实现但无调用方；图谱是内存实现
#   Layer 4  图与权限校验代码在，但 acompletion 未传 tools= → 工具链路未接通；
#            权限等级取全局常量而非按用户分级。测试把 LLM 全 AsyncMock，
#            所以这条断链可以完全躲在 395 个绿灯后面。
#   Layer 6  8 页真实取数；persona 页是占位；admin token 被编译进浏览器 bundle

v0.2.0 横切模块:
  security/hmac.py          Webhook HMAC-SHA256        → 13 tests
  auth/                     JWT RBAC 多租户            → 27 tests
  metrics/                  Prometheus 指标            → 11 tests
  experiment/               A/B 测试框架               → 22 tests
  streaming/sse.py          SSE 流式推理               →  7 tests
  flow/ + postgres.py       可插拔持久化               → 回归覆盖
```

## 质量门禁

| 门禁 | 命令 | 当前状态 |
|---|---|---|
| 测试 | `pytest tests/ -q` | ✅ 395 passed（连跑 3 次结果一致） |
| Lint | `ruff check src tests` | ✅ All checks passed |
| 类型 | `mypy src`（strict） | ✅ no issues found in 72 source files |
| 前端类型 | `cd web && npx tsc --noEmit` | ✅ exit 0 |
| 引擎启动 | `python -m lingxi --persistent` | ✅ 挂载 24 条路由，`/api/dashboard/summary` 无 token 401 / 带 token 200 |
| 强制点 | `pre-commit run --all-files` | ✅ 3 hooks Passed（已 `pre-commit install`） |

实测环境：Python 3.13.9 / ruff 0.16.8 / mypy 2.3.1 / pytest 8.4.2。
工具版本已锁上界并与 `.pre-commit-config.yaml` 的 `rev` 对齐 —— 此前两者不一致
（0.7.0 报 3 处、0.16.8 报 0 处），会让同一道门禁有两套答案。

> 门禁绿只代表代码自洽，**不代表上节「实现状态」里列出的功能真的通**：
> LLM 与 Embedding 在 `tests/test_layer4.py:381-487` 等处全部被 `AsyncMock` 替换，
> 因此「Agent 不会调工具」这类缺陷可以完全躲在绿灯后面。

**已知待办**：见上节「实现状态」1-5 条（M1 工具链路 / 记忆后端 / MCP 原语 /
分级权限来源 / RAG 静默失效）；微信回调 HTTP 路由未接（Gewechat adapter 已就绪）；
`persona` 页为占位（后端无 `/api/persona`）；`/metrics` 无鉴权；
PostgreSQL 后端与 Mem0/Neo4j 配置项属未接线状态。

## 许可证

MIT
