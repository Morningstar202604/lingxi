# 智犀 ZhiXi

> 智辨已知，犀照善为。

智犀 (ZhiXi) v0.2.0 — 7×24 小时全栈数字员工引擎，框架代号 **lingxi**。
部署在自有服务器，通过 QQ/微信触达用户，具备深度人格、长期记忆与分级管控的工具调用框架。

> **当前版本状态**：全部 7 层可用，47 条路由在真实进程里挂载，管理面全部需凭据。
> 逐条实测依据见「[实现状态](#实现状态实测校正)」，
> 完整修复清单见 [`docs/AUDIT-2026-10-12.md`](docs/AUDIT-2026-10-12.md)。

## 核心能力

### 基础架构 (Layer 0-6)

| 模块 | 状态 | 说明 |
|---|---|---|
| Layer 0 基础地基 | ✅ | pyproject + 协议模型 + structlog + Pydantic Settings |
| Layer 1 IM 桥接 | ✅ | NapCat (QQ) + Gewechat (微信) → 统一 IMEvent |
| Layer 2 人格引擎 | ✅ | 13 模块 Persona + 注入管线 + 防注入护栏 |
| Layer 3 记忆系统 | ✅ | 5 段式管线；编码时抽实体建图，`recall_hybrid` 混合召回（字面 + 图谱） |
| Layer 4 Agent 编排 | ✅ | LangGraph ReAct + 真实工具调用 + 重试退避 + token 计量 + 上下文截断 + 循环护栏 + 逐 token 流式（含工具调用）+ 按 user/chat 分级权限 + L2/L3 人工审批挂起/恢复 |
| Layer 5 业务管线 | ✅ | 工单/队列/升级 + 规则引擎 + SLA + 审计 + 坐席分配 + 工作流 DAG（含真实执行引擎） |
| Layer 6 控制台 | ✅ | Next.js 10 路由全部真实取数；echarts 动态可视化 |

### v0.2.0 新增模块

| 模块 | 状态 | 说明 |
|---|---|---|
| Webhook HMAC-SHA256 | ✅ | 出站签名 + **入站验签端点** `/api/webhook/inbound`（时间戳容差 + 常量时间比较；未配密钥不挂载） |
| JWT RBAC | ⚠️ | 4 角色×12 权限已实现，`auth.enabled` 默认关闭；Dashboard 管理面另走 `x-admin-token` 单因子（fail-closed）；多租户只有字段、无查询隔离 |
| A/B 测试框架 | ✅ | 7 个 REST 端点，生产进程已挂载（`/experiments*`） |
| Prometheus 指标 | ✅ | 12 指标 + ASGI 中间件 + `/metrics`（需 `metrics.scrape_token` 或 admin token） |
| SSE 流式推理 | ✅ | `/api/events` 事件流 + `/api/stream/chat` + `/api/sessions/{chat_id}/stream` 均已挂载 |
| 坐席管理 | ✅ | `/api/seats*` 5 端点，含分配策略与利用率（需 admin token）；升级时消息链路自动尝试分派 |
| 工作流 DAG | ✅ | `/graph*` 4 端点：Mermaid 输出 / YAML / 校验 / 预览；`/api/workflow/execute` 按拓扑+条件边真实执行 |
| MCP / OpenAI 兼容 | ✅ | `/mcp/*` + `/v1/tools*`，凭据校验；三原语齐全（tools / resources / prompts，JSON-RPC 走 `/mcp/rpc`） |
| 可插拔持久化 | ✅ | SQLite / **PostgreSQL**（连接池固定在后台 runner 循环，端到端实测通过）/ Memory 三后端 |
| 语义记忆 / 图谱后端 | ✅ | **Mem0**（Ollama 抽取+向量召回）与 **Neo4j**（双向邻居语义）可选启用，活服务契约测试锁住 |

## 实现状态（实测校正）

2026-10-12 一轮逐文件复查 + 真实启动复验后，以下 7 条已全部修复，
完整修复清单见 [`docs/AUDIT-2026-10-12.md`](docs/AUDIT-2026-10-12.md)。

| # | 历史缺陷 | 现状 |
|---|---|---|
| 1 | `acompletion` 不传 `tools=` → 工具调用链路断裂 | ✅ 已下发 `tools=` + `tool_choice="auto"`，多 tool_calls 全量执行并配对回填 |
| 2 | Mem0 / Neo4j 零实现 | ✅ **均已真实实现**（`mem0.enabled` / `neo4j.enabled` 启用）：Mem0 语义抽取+向量召回、Neo4j 双向邻居图谱，活服务契约测试锁住 |
| 3 | MCP 只实现 tools 一个原语 | ✅ 三原语齐全：`/mcp/rpc` 走 JSON-RPC，`resources/list` `resources/read` `prompts/list` `prompts/get` 全实现，接真实数据源（知识库 / 人格配置 / 实体图谱） |
| 4 | 分级权限来源是进程级全局常量 | ✅ `resolve_permission_level(user, chat)` 按 `security.permission_rules` 给到 |
| 5 | 向量检索默认配置下静默失效 | ⚠️ 未配 embedding key 时降级零向量（见下「已知限制」） |
| 6 | seat / workflow / mcp / experiments / SSE 生产零挂载 | ✅ 路由 24 → **54**（2026-10-13 增入站验签 + 模型状态），并用 `test_full_route_inventory` 锁住 |
| 7 | `/metrics` 匿名可读 | ✅ 需 `metrics.scrape_token`（回退 admin token），未配置则 503 |

新增修掉的三个高危点：

- **MCP 路由无凭据校验**（`X-Miner-Permission` 是等级不是凭据）→ 补 `_require_admin`
- **坐席路由无鉴权**（挂载后才暴露）→ router 级依赖
- **图谱双向遍历断裂**（`add_relation` 只登记 `_adj[source]`）→ 邻接表双向登记

### Agent 运行时能力（2026-10-12 第二轮补齐）

工具调用链路接通后仍实测出 7 个缺口，全部已修并被 `tests/test_agent_runtime.py`（26 条）锁住：

| 能力 | 实现 | 修复前的实测问题 |
|---|---|---|
| 重试与退避 | `agent/llm.py` 指数退避 + jitter | 单次 `RuntimeError` 直接转人工 |
| 异常分类 | 凭据/参数错**不重试** | 对着坏 key 重试白烧配额 |
| token 计量 | `AgentState.token_usage` 跨轮累加 | 全仓零 `usage` 读取，成本不可观测 |
| 上下文控制 | 工具结果按头/中/尾截断 | 30 万字符全量进 messages |
| 循环护栏 | 连续相同工具调用短路转人工 | 模型可空转到 max_steps |
| 逐 token 流式 | `astream(stream_mode="custom")` | SSE 只有 3 个事件，docstring 声称的"逐 token"是假的 |
| 空响应防御 | 空 choices / content-blocks 数组 | 空响应被伪装成"服务不可用" |

流式与非流式**共用同一张图、同一套状态机**（`_build_reason_update` 单一实现），
不存在"流式走另一条逻辑"导致两边漂移的风险；护栏在流式下同样生效。

### 人工审批流（2026-10-12 第三轮补齐）

BLUEPRINT 的「L2 需用户确认 / L3 需管理员审批」此前只有
「放行 / 拒绝」两态。现在用 LangGraph `interrupt()` 实现真正的
挂起-恢复：

1. `execute_tool_node` 发现 L2/L3 且权限不足 → `interrupt(payload)` 挂起整张图
2. 控制台 `GET /api/approvals` 看到待审批清单
3. `POST /api/approvals/{id}/approve|reject` 决策
4. 决策端点调 `agent.resume()`（`Command(resume=...)`）让图从挂起点继续

审批通过 = **临时授予本次调用的权限**，不是给用户提权；
L4 红线无论谁批都不放行。审批记录按 (工具, 参数, 会话) 指纹
幂等（LangGraph 恢复时节点会整体重放），超时（默认 15 分钟）
自动收敛为 expired。`tests/test_approval_flow.py` 逐条锁住
节点重放、thread_id 一致性、黑名单子串误伤等。

### 已知限制（有意保留，不是遗漏）

| 项 | 原因 |
|---|---|
| 向量检索降级零向量 | 未配 embedding key 时的既有降级策略，需配 `LINGXI_EMBEDDING__API_KEY` 才生效 |
| 趋势图只画本次会话采样 | 后端不存时序历史，页面不伪造数据 |
| Mem0 全局记忆计数为估算 | mem0 2.x 的 `get_all` 必须带 entity filter；总数退化为查询底层向量库（chroma）计数 |
| A/B 分流不参与消息链路 | 实验引擎的管理/分配/指标 API 已就绪（`/api/experiments*`），但"哪个实验驱动人格覆盖"属产品定义，未硬编码进消息流 |

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

### 第 1 步：接一个大模型（云端 API 优先）

引擎不内置模型。**推荐云端 API**：成熟稳定、免下载、按量计费成本低。
到下面任一平台申请 Key（国内可直连），把环境变量写进 `.env`：

| 平台 | 申请入口 | 环境变量片段 |
|---|---|---|
| DeepSeek（推荐） | https://platform.deepseek.com/api_keys | `LINGXI_LLM__PROVIDER=deepseek`<br>`LINGXI_LLM__MODEL=deepseek-chat`<br>`LINGXI_LLM__API_KEY=<你的 Key>`<br>`LINGXI_LLM__BASE_URL=https://api.deepseek.com` |
| 通义千问 | https://bailian.console.aliyun.com/ | `LINGXI_LLM__PROVIDER=openai`<br>`LINGXI_LLM__MODEL=qwen-plus`<br>`LINGXI_LLM__API_KEY=<DashScope Key>`<br>`LINGXI_LLM__BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 智谱 GLM（有免费档） | https://open.bigmodel.cn/usercenter/apikeys | `LINGXI_LLM__PROVIDER=openai`<br>`LINGXI_LLM__MODEL=glm-4-flash`<br>`LINGXI_LLM__API_KEY=<Key>`<br>`LINGXI_LLM__BASE_URL=https://open.bigmodel.cn/api/paas/v4` |
| OpenAI | https://platform.openai.com/api-keys | `LINGXI_LLM__PROVIDER=openai`<br>`LINGXI_LLM__MODEL=gpt-4o-mini`<br>`LINGXI_LLM__API_KEY=sk-...` |

> 控制台「模型」页（`/setup`）内置同样的配置卡片，可一键复制变量、
> 实时查看当前模型可用状态。

**本地模型（可选）**：完全离线、数据不出机器，适合隐私场景。
安装 [Ollama](https://ollama.com/download) 后执行
`ollama pull qwen2.5:7b`（默认配置即指向本地 Ollama，无需改任何变量）。

### 第 2 步：安装与启动

```bash
# 1. 安装依赖（需要 Python 3.11+）
pip install -e ".[dev]"

# 2. 运行测试（726 tests）
pytest tests/ -q

# 3. 配置凭据（管理面 / 指标端点 fail-closed，不配就启动不了管理功能）
export LINGXI_SECURITY__ADMIN_TOKEN="<≥16 位随机串>"
export LINGXI_METRICS__SCRAPE_TOKEN="<Prometheus 抓取用>"
# 可选：按用户给分级权限（key 可以是 user_id / chat_id / "@all"）
export LINGXI_SECURITY__PERMISSION_RULES='{"u_vip_001": 2, "wxid_abc": 0}'
# 可选：JWT 鉴权（开启时必须同时配 jwt_secret 与 admin_password）
export LINGXI_AUTH__ENABLED=true
export LINGXI_AUTH__JWT_SECRET="<≥32 字节随机串>"
export LINGXI_AUTH__ADMIN_PASSWORD="<引导管理员口令>"

# 4. 启动引擎（开发模式，需已运行 NapCat）
LINGXI_ONEBOT__WS_URL=ws://127.0.0.1:3001 python -m lingxi
# PowerShell: $env:LINGXI_ONEBOT__WS_URL="ws://127.0.0.1:3001"; python -m lingxi
# 端口 8080 被占用时会自动顺延（8081、8082…），启动日志会打印实际端口

# 5. 控制台（另开终端）
cd web
echo NEXT_PUBLIC_ADMIN_TOKEN=<与上面一致> > .env.local
npm run dev                                                  # http://localhost:3000
```

> `NEXT_PUBLIC_ADMIN_TOKEN` 会被编译进浏览器 bundle，因此**只放只读管理凭据**，
> 不要把 JWT 签名密钥或生产 admin token 放进前端环境变量。

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
│   │   ├── llm.py                 #   重试退避 + token 计量 + 空响应防御 + 流式
│   │   ├── tools.py               #   MCP 工具注册表 + 分级权限总线 + 结果截断
│   │   ├── approval.py            #   L2/L3 人工审批：挂起 / 决策 / 恢复
│   │   └── graph.py               #   状态机组装 + Checkpoint + run/astream/resume
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
│   ├── security/                  # 安全
│   │   ├── hmac.py                #   Webhook HMAC-SHA256 出站签名
│   │   └── pii.py                 #   PII 检测与脱敏
│   ├── seat/                      # 坐席管理（注册/状态/分配策略/利用率）
│   │   ├── models.py              #   Seat 三态机 + SeatStore
│   │   ├── assignment.py          #   round_robin / least_busy / skill_based
│   │   └── router.py              #   /api/seats* 5 端点（需 admin token）
│   ├── workflow/                  # 工作流 DAG
│   │   ├── models.py              #   WorkflowDAG / DAGNode / DAGEdge
│   │   ├── builder.py             #   默认管线 DAG + YAML 互转
│   │   ├── validator.py           #   环检测 / 悬空引用 / 入口出口检查
│   │   ├── executor.py            #   执行引擎：拓扑驱动 + 条件边求值
│   │   ├── mermaid.py             #   Mermaid 代码生成
│   │   └── router.py              #   /graph* 4 端点 + /execute
│   └── mcp/                       # MCP / OpenAI 兼容工具协议
│       ├── server.py              #   MCPToolServer + 单例（三原语）
│       ├── primitives.py          #   resources / prompts 注册表
│       ├── models.py              #   JSON-RPC 请求/响应模型
│       └── router.py              #   /mcp/* + /v1/tools*（需 admin token）
├── web/                           # Layer 6: Next.js 14 控制台
│   ├── app/                       #   10 routes（全部真实取数）
│   ├── components/                #   EChart / DashboardCharts / MetricsGrid / QueuePanel
│   │                              #   / TicketsTable / Sidebar / Clock
│   └── lib/api.ts                 #   Dashboard API 客户端
├── tests/                         # 648 tests 全部通过
│   ├── conftest.py                #   共享 fixtures
│   ├── test_layer0.py             #   19 tests ✅
│   ├── test_layer1.py             #   18 tests ✅
│   ├── test_layer2.py             #   37 tests ✅
│   ├── test_layer3.py             #   18 tests ✅
│   ├── test_layer4.py             #   66 tests ✅
│   ├── test_layer5.py             #   37 tests ✅
│   ├── test_layer6.py             #   10 tests ✅
│   ├── test_knowledge.py          #   19 tests ✅
│   ├── test_phase2.py             #   21 tests ✅
│   ├── test_phase3.py             #   51 tests ✅
│   ├── test_e2e_smoke.py          #   19 tests 全链路 + 入口冒烟 ✅
│   ├── test_hmac.py               #   13 tests ✅
│   ├── test_auth.py               #   25 tests ✅
│   ├── test_metrics.py            #   11 tests ✅
│   ├── test_experiment.py         #   26 tests ✅
│   ├── test_streaming.py          #    7 tests ✅
│   ├── test_security_p0.py        #   57 tests 安全回归 ✅
│   ├── test_wiring_p0.py          #   17 tests 工具链路/挂载/鉴权/分级权限/路由清单 ✅
│   ├── test_agent_runtime.py      #   26 tests 重试/计量/截断/循环护栏/流式 ✅
│   ├── test_memory_graph.py       #   14 tests 实体抽取/图谱建边/混合召回 ✅
│   └── test_seat_workflow.py      #   22 tests 坐席分配 + 工作流 DAG ✅
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
| **字体** | 系统字体栈（PingFang / 微软雅黑 / 思源 / Noto CJK）——**不使用 Google Fonts**，国内网络不可达 |
| **风格** | 精密、温暖、专业 |

## 分层开发

每层独立可运行、引用前序层符号、绝不重复导入。

```
Layer 0 ✅ 基础地基 (pyproject + 协议 + 日志 + 配置)          → 19 tests
Layer 1 ✅ IM 桥接   (NapCat + Gewechat + 统一 IMEvent)      → 18 tests
Layer 2 ✅ 人格引擎  (13 模块 + 注入管线 + 护栏 + 权限解析)    → 47 tests
Layer 3 ✅ 记忆系统  (5 段式 + Lorebook + 实体图谱 + 混合召回) → 32 tests
Layer 4 ✅ Agent 编排 (LangGraph ReAct + 工具调用 + 分级权限) → 79 tests
Layer 5 ✅ 业务管线  (工单/队列/规则/SLA/审计 + 坐席 + 工作流) → 37 tests
Layer 6 ✅ 控制台    (Next.js 10 routes + echarts 可视化)     → 10 tests

v0.2.0 横切模块:
  security/hmac.py + test_security_p0  Webhook 签名 + 安全回归 → 70 tests
  auth/                               JWT RBAC 多租户           → 25 tests
  metrics/                            Prometheus 指标           → 11 tests
  experiment/                         A/B 测试框架              → 26 tests
  streaming/sse.py                    SSE 流式推理              →  7 tests
  seat/ + workflow/                   坐席分配 + DAG 可视化      → 22 tests
  mcp/                                MCP + OpenAI 兼容工具路由  → 见 wiring 回归
  全链路冒烟 + 接线回归                                            → 36 tests
```

## 质量门禁

| 门禁 | 命令 | 当前状态 |
|---|---|---|
| 测试 | `pytest tests/ -q` | ✅ **750 passed** |
| Lint | `ruff check src tests` | ✅ All checks passed |
| 类型 | `mypy src`（strict） | ✅ no issues found in 81 source files |
| 前端类型 | `cd web && npx tsc --noEmit` | ✅ exit 0 |
| 前端 Lint | `cd web && npx next lint` | ✅ No ESLint warnings or errors |
| 前端构建 | `cd web && npm run build` | ✅ Compiled successfully，11 页面静态导出 |
| **真实启动** | `python -m lingxi` + 拉 `/openapi.json` | ✅ **挂载 54 条路由**（端口被占自动顺延） |
| **鉴权探测** | 无 token 打管理面 / 带 token 打管理面 | ✅ 前者 401/503，后者全 200 |
| 强制点 | `pre-commit run --all-files` | ✅ hooks Passed |

实测环境：Python 3.13.9 / ruff 0.16.8 / mypy 2.3.1 / pytest 8.4.2 / Next.js 14.2.35。

> **为什么必须跑「真实启动 + HTTP 探测」这道门禁**
>
> 单测自己调 `create_dashboard_app(...)` 并显式传参，生产入口不传 ——
> 于是「路由没挂载」这类缺陷可以完全躲在绿灯后面。
> `tests/test_wiring_p0.py::test_full_route_inventory` 现在用
> `build_default_engine().dashboard_app()` 断言完整路由集合，
> 把「文档声称的能力」和「实际挂载的端点」绑在一起。

**已知待办**：PostgreSQL 后端接入（需先把 pipeline 改 async）；
`/api/events` SSE 接前端（EventSource 不支持自定义 header，
需 token 走 query 或改 fetch-event-source）。

## 许可证

MIT
