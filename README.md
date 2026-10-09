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
pytest tests/ -v

# 3. 启动（开发模式，需已运行 NapCat）
LINGXI_ONEBOT__WS_URL=ws://127.0.0.1:3001 python -m lingxi
```

## 项目结构

```
lingxi/
├── docs/BLUEPRINT.md          # 完整架构蓝图 + 路线图
├── src/lingxi/
│   ├── core/                  # Layer 0: 协议模型 + 日志 + trace_id
│   │   ├── protocol.py        #   OneBot v11 → 统一 IMEvent
│   │   ├── logging.py         #   structlog + 链路追踪
│   │   └── settings.py        #   Pydantic Settings 配置
│   ├── im/                    # Layer 1: IM 桥接
│   │   ├── adapter.py         #   IMAdapter 基类
│   │   ├── napcat.py          #   NapCat (QQ) 适配器
│   │   └── gewechat.py        #   Gewechat (微信) 适配器
│   ├── persona/               # Layer 2: 人格引擎
│   │   ├── schema.py          #   13 模块 Persona 配置
│   │   ├── injector.py        #   System prompt 注入管线
│   │   └── guardrails.py      #   注入检测 + L0-L5 权限
│   ├── memory/                # Layer 3: 记忆系统 (WIP)
│   ├── agent/                 # Layer 4: LangGraph 编排 (WIP)
│   ├── flow/                  # Layer 5: 业务管线 (WIP)
│   └── console/               # Layer 6: Next.js 控制台 (WIP)
├── tests/                     # 每层独立测试
│   ├── test_layer0.py         #   19 tests ✅
│   ├── test_layer1.py         #   18 tests ✅
│   ├── test_layer2.py         #   18 tests ✅
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
Layer 3 🔄 记忆系统 (Mem0 5段式 + 图谱 + Lorebook)
Layer 4 🔄 Agent 编排 (LangGraph ReAct + MCP + 分级权限)
Layer 5 🔄 业务管线 (工单/队列/升级 + Dashboard API)
Layer 6 🔄 控制台 (Next.js 实时监控面板)
```

## 许可证

MIT
