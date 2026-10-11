# 智犀 ZhiXi

> 智辨已知，犀照善为。

智犀 (ZhiXi) v0.2.0 — 7×24 小时全栈数字员工引擎，框架代号 **lingxi**。
部署在自有服务器，通过 QQ/微信触达用户，具备深度人格、长期记忆、多步推理与高权限工具调用能力。

## 核心能力

### 基础架构 (Layer 0-6)

| 模块 | 状态 | 说明 |
|---|---|---|
| Layer 0 基础地基 | ✅ | pyproject + 协议模型 + structlog + Pydantic Settings |
| Layer 1 IM 桥接 | ✅ | NapCat (QQ) + Gewechat (微信) → 统一 IMEvent |
| Layer 2 人格引擎 | ✅ | 13 模块 Persona + 注入管线 + 防注入护栏 |
| Layer 3 记忆系统 | ✅ | 5 段式管线 (编码→存储→召回→更新→遗忘) + Lorebook + 实体图谱 |
| Layer 4 Agent 编排 | ✅ | LangGraph ReAct + MCP 工具协议 + L0-L4 分级权限 |
| Layer 5 业务管线 | ✅ | 工单/队列/升级 + 规则引擎 + SLA + 审计日志 |
| Layer 6 控制台 | ✅ | Next.js 9 路由生产经营面板 |

### v0.2.0 新增模块

| 模块 | 状态 | 说明 |
|---|---|---|
| Webhook HMAC-SHA256 出站签名 | ✅ | `security/hmac.py` — 防重放、常量时间比较，业界标准 |
| JWT RBAC 多租户 | ✅ | `auth/` — 4 角色 (Admin/Operator/Viewer/API) × 10 细粒度权限，Bearer JWT + API Key 双认证 |
| A/B 测试框架 | ✅ | `experiment/` — MD5-salt 确定性分流 + z-test 胜率判定，7 REST 端点 |
| Prometheus 指标 | ✅ | `metrics/` — 12 指标覆盖 HTTP/Agent/业务/Knowledge，ASGI 中间件自动采集，`/metrics` 端点 |
| SSE 流式推理 | ✅ | `streaming/sse.py` — Agent 推理链 7 事件实时推送 (agent_start → knowledge_recall → thinking → tool_call/result → message → agent_end) |
| 可插拔持久化 | ✅ | `flow/persistence_factory.py` + `flow/postgres.py` — 三后端 (SQLite/PostgreSQL/Memory)，asyncpg 连接池，原子出队 FOR UPDATE SKIP LOCKED |

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
Layer 3 ✅ 记忆系统  (5 段式管线 + Lorebook + 实体图谱)        → ~18 tests
Layer 4 ✅ Agent 编排 (LangGraph ReAct + MCP + 分级权限)       → ~35 tests
Layer 5 ✅ 业务管线  (工单/队列/升级 + 规则引擎 + SLA + 审计)  → ~35 tests
Layer 6 ✅ 控制台    (Next.js 9 routes)

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
| 测试 | `pytest tests/ -q` | ✅ 395 passed |
| Lint | `ruff check src tests` | ✅ 无问题 |
| 类型 | `mypy src`（strict） | ✅ 30 文件 0 错误 |
| 前端类型 | `cd web && npx tsc --noEmit` | ✅ 通过 |
| 构建 | `npm run build` | ✅ 9 routes clean |

**已知待办**：微信回调 HTTP 服务未接（Gewechat adapter 已就绪，缺 FastAPI callback 路由）；控制台部分导航页功能待丰富；会话/工单状态目前以 SQLite 持久化为主，Postgres 后端已就绪可按需切换。

## 许可证

MIT
