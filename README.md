# 智犀 (ZhiXi)

> 智辨已知，犀照善为。二十四小时在线的全栈数字员工引擎。

智犀不是聊天机器人 —— 它是一个部署在自有服务器上、7×24 小时在线、
通过 QQ/微信触达用户的高权限全栈数字员工引擎。

## 核心能力

- **全渠道 IM**：通过 NapCat（QQ）+ Gewechat（微信）同时服务两个平台的用户
- **深度人格**：13 模块人格引擎（Hermes Persona 风格），角色锚不可被 prompt 注入篡改
- **长期记忆**：Mem 5 段式管线（编码→存储→检索→更新→遗忘）+ 实体关系图谱
- **多步推理**：LangGraph ReAct 状态机，支持链式工具调用 + 人工介入断点
- **分级权限**：L0-L4 五级权限管控，从只读到高风险 Shell 执行各有审批链
- **生产经营控制台**：Next.js 实时监控面板，坐席可断点接管、实时注入上下文

## 复用哪些开源组件（不重复造轮子）

| 部件 | 开源方案 |
|---|---|
| QQ 桥 | NapCat (OneBot v11) |
| 微信桥 | Gewechat (OneBot 兼容) |
| 工具协议 | MCP (Model Context Protocol) |
| Agent 编排 | LangGraph |
| 长期记忆 | Mem0 |
| 知识图谱 | Neo4j (或 NetworkX 起步) |
| 多模型路由 | LiteLLM |
| 角色卡标准 | SillyTavern Character Card V3 |
| 人格引擎参考 | Hermes Persona 13 模块 |

## 快速开始

```bash
# 1. 安装依赖（需要 Python 3.11+）
pip install -e ".[dev]"

# 2. 运行测试
pytest tests/ -v          # 151 tests

# 3. 启动（开发模式，需已运行 NapCat）
LINGXI_ONEBOT__WS_URL=ws://127.0.0.1:3001 python -m lingxi
# PowerShell：$env:LINGXI_ONEBOT__WS_URL="ws://127.0.0.1:3001"; python -m lingxi

# 4. 控制台（另开终端）
cd web && npm run dev      # http://localhost:3000
```

## 项目结构

```
lingxi/
├── docs/BLUEPRINT.md          # 完整架构蓝图 + 路线图
├── src/lingxi/
│   ├── __main__.py            #   引擎入口（装配 + 启动序列）
│   ├── core/                  # Layer 0: 协议模型 + 日志 + trace_id
│   │   ├── protocol.py        #   OneBot v11 → 统一 IMEvent
│   │   ├── logging.py         #   structlog + 链路追踪
│   │   └── settings.py        #   Pydantic Settings 配置
│   ├── im/                    # Layer 1: IM 桥接
│   │   ├── adapter.py         #   IMAdapter 基类 + CQ 码解析
│   │   ├── napcat.py          #   NapCat (QQ) WebSocket 适配器
│   │   └── gewechat.py        #   Gewechat (微信) HTTP 回调适配器
│   ├── persona/               # Layer 2: 人格引擎
│   │   ├── schema.py          #   13 模块 Persona 配置
│   │   ├── injector.py        #   System prompt 注入管线
│   │   └── guardrails.py      #   注入检测 + L0-L4 权限
│   ├── memory/                # Layer 3: 记忆系统
│   │   ├── pipeline.py        #   Mem0 5 段式（编码→存储→召回→更新→遗忘）
│   │   ├── lorebook.py        #   关键词触发的世界观按需注入
│   │   └── graph_store.py     #   实体图谱抽象 + 内存实现
│   ├── agent/                 # Layer 4: LangGraph 编排
│   │   ├── state.py           #   AgentState TypedDict
│   │   ├── nodes.py           #   reason / execute_tool / escalate / respond
│   │   ├── tools.py           #   MCP 工具注册表 + 分级权限总线
│   │   └── graph.py           #   状态机组装 + Checkpoint 持久化
│   ├── flow/                  # Layer 5: 业务管线
│   │   ├── pipeline.py        #   IM 事件 → Agent → 回复/升级 主循环
│   │   ├── ticket.py          #   工单系统 + 状态机 + 自动分类
│   │   ├── queue.py           #   人工坐席升级队列（优先级调度）
│   │   └── dashboard_api.py   #   FastAPI Dashboard 接口
├── web/                       # Layer 6: Next.js 控制台
│   ├── app/page.tsx           #   实时监控总览
│   ├── components/            #   MetricsGrid / QueuePanel / TicketsTable
│   └── lib/api.ts             #   Dashboard API 客户端
├── tests/                     # 每层独立测试（151 tests）
│   ├── test_layer0.py         #   19 tests ✅
│   ├── test_layer1.py         #   18 tests ✅
│   ├── test_layer2.py         #   18 tests ✅
│   ├── test_layer3.py         #   18 tests ✅
│   ├── test_layer4.py         #   35 tests ✅
│   ├── test_layer5.py         #   32 tests ✅
│   ├── test_e2e_smoke.py      #   11 tests ✅（全链路 + 入口冒烟）
│   └── conftest.py            #   共享 fixtures
└── pyproject.toml             #   uv/hatch + ruff + mypy + pytest
```

## 品牌规范

| 项 | 定义 |
|---|---|
| **名称** | 智犀 ZhiXi |
| **Tagline** | 通心 · 知事 · 善为 |
| **主色** | 深靛青 `#1B2A4A` + 琥珀橙 `#E8963E` + 翠玉绿 `#2ECC71` |
| **图形符号** | 犀角（智犀之角 = 智照善行的天线）× 电路纹 |
| **字体** | 中文：思源宋体 / 英文：Inter |

## 分层开发

每层独立可运行、引用前序层符号、绝不重复导入。

```
Layer 0 ✅ 基础地基 (pyproject + 协议 + 日志 + 测试)        → 19 tests
Layer 1 ✅ IM 桥接  (NapCat + Gewechat + 统一事件流)       → 18 tests
Layer 2 ✅ 人格引擎 (13 模块 + 注入管线 + 护栏)            → 18 tests
Layer 3 ✅ 记忆系统 (Mem0 5段式 + 图谱 + Lorebook)          → 18 tests
Layer 4 ✅ Agent 编排 (LangGraph ReAct + MCP + 分级权限)   → 35 tests
Layer 5 ✅ 业务管线 (工单/队列/升级 + Dashboard API)       → 32 tests
Layer 6 🔄 控制台 (Next.js 总览已跑通，工单/队列/会话/日志页待补)
```

## 质量门禁

| 门禁 | 命令 | 当前状态 |
|---|---|---|
| 测试 | `pytest tests/ -q` | ✅ 151 passed |
| Lint | `ruff check src tests` | ✅ 无问题 |
| 类型 | `mypy src`（strict） | ✅ 30 文件 0 错误 |
| 前端类型 | `cd web && npx tsc --noEmit` | ✅ 通过 |

**已知待办**：微信回调 HTTP 服务未接（Gewechat adapter 已就绪，缺 FastAPI callback 路由）；控制台四个导航页未实现；队列/工单仍为进程内内存态（重启即失），接 Redis/Postgres 后可跨重启。

## 许可证

MIT
