# 智犀 (ZhiXi) — 架构蓝图

> 智辨已知，犀照善为。二十四小时在线的数字员工，通心、知事、善为。

> **⚠️ 本文档是设计蓝图，不是现状说明书。**
> 下文「复用清单」「分层路线图」描述的是**目标态**。截至 2026-10-11 的实现进度与
> 逐条实测差异见 `README.md` 的「实现状态」节，其中最主要的三点：
> **Mem0 / Neo4j 零实现**（本蓝图第 26、39、40、88 行的 Mem0/Neo4j 均为规划项，
> 当前记忆是内置 SQLite 管线 + 字符级 n-gram 召回，图谱是 InMemoryGraphStore）；
> **MCP 只实现 tools 一个原语**（第 37 行的「三原语」未达成）；
> **Agent 工具调用链路未接通**（第 38 行 LangGraph 图已组装但 `tools=` 从未传给模型）。
> 请以 README 的实现状态为准判断"现在能跑什么"。

---

## 品牌定义

| 项 | 定义 |
|---|---|
| **名称** | 智犀 ZhiXi |
| **Tagline** | 通心 · 知事 · 善为 |
| **图形符号** | 犀角 + 电路纹路融合标识 |
| **主色** | 深靛青 `#1B2A4A`（信任/专业）+ 琥珀橙 `#E8963E`（温度/活力）+ 翠玉绿 `#2ECC71`（正常/通过） |
| **字体** | 中文：思源宋体 Heavy / 英文：Inter Bold |
| **Slogan** | 不是聊天机器人，是你的数字同事 |

---

## 产品定位

一个部署在自有服务器上、7×24 小时在线的全栈数字员工引擎。它：
- 通过 **NapCat（QQ）** 和 **Gewechat（微信）** 直接触达用户
- 拥有经过分级管控的 **高权限操作能力**（Shell/文件/浏览器/API）
- 通过 **LangGraph** 状态机编排复杂的多步推理与工具调用
- 通过 **Mem0** 长期记忆和 **Neo4j** 实体图谱持续积累用户洞察
- 通过 **13 模块人格引擎** 呈现稳定、可信、有温度的角色形象

---

## 复用清单（不重复造轮子）

| 部件 | 开源方案 | 角色 |
|---|---|---|
| QQ 桥 | **NapCat** (OneBot v11) | 标准 QQ 协议收发 |
| 微信桥 | **Gewechat** (OneBot 兼容) | 标准微信协议收发 |
| 工具协议 | **MCP** (Model Context Protocol) | 工具注册/调用/资源/提示词 三原语 |
| Agent 编排 | **LangGraph** | 有向图状态机 + Checkpoint + 人工介入 |
| 长期记忆 | **Mem0** | 编码→存储→检索→更新→遗忘 全流水线 |
| 知识图谱 | **Neo4j** (或 NetworkX 起步) | 实体-关系-事实结构化存储 |
| 多模型路由 | **LiteLLM** | OpenAI/Anthropic/DeepSeek/Ollama 一行切换 |
| 角色卡标准 | **SillyTavern Character Card V3** | PNG 嵌入元数据、单文件携带人格 |
| 人格引擎 | **Hermes Persona 13 模块** | 时间感知/对话阶段/关键词变体/情绪向量/固定信号... |
| 世界观注入 | **Lorebook / World Info** | 关键词触发的按需上下文注入 |
| Web 框架 | **FastAPI** (复用 Zhike 经验) | API 服务层 |
| 前端 | **Next.js** + shadcn/ui | 控制面板 |
| 编排辅助 | **ngrok** 或直连 | 开发期隧道（如需） |

**唯一不使用**：自家 CatPaw Skills、Skills-market、Ontology、spark 等模板型资产。方法论（分层积木式）沿用自家 agent-builder。

---

## 分层积木路线图（Node 0 → Node N）

### Layer 0：基础地基

```
Node 0.1  pyproject.toml + uv 工作区（pydantic v2 / pytest / ruff / mypy）
Node 0.2  config/  — Pydantic Settings，YAML/ENV 双源配置
Node 0.3  protocol/ — OneBot v11 数据模型（Message / Event / Segments）
Node 0.4  logging/   — structlog 结构化日志，统一 trace_id
Node 0.5  tests/conftest.py — 共享 fixtures
```

### Layer 1：IM 桥接

```
Node 1.1  adapter/napcat.py — WebSocket OneBot 客户端，接收 QQ 事件
Node 1.2  adapter/gewechat.py — 同上接微信
Node 1.3  protocol/unified_event.py — 统一事件类型：IMEvent {channel, chat_id, user_id, segments, raw}
Node 1.4  protocol/send.py — 回复发送（文本/图片/文件）
```

### Layer 2：人格引擎

```
Node 2.1  persona/schema.py — 13 模块 Persona 配置模型
Node 2.2  persona/identity.py — 身份指纹（不可篡改锚）
Node 2.3  persona/injector.py — 每层注入管线（system → context → messages）
Node 2.4  persona/guardrails.py — 护栏：角色锚不可覆盖 + 权限检查
```

### Layer 3：记忆系统

```
Node 3.1  memory/mem0_pipeline.py — 编码→存储→检索→更新→遗忘 5 段式
Node 3.2  memory/lorebook.py — 关键词触发的世界观按需注入
Node 3.3  memory/graph_store.py — Neo4j/NetworkX 实体图谱
```

### Layer 4：Agent 编排

```
Node 4.1  agent/state.py — LangGraph State 定义
Node 4.2  agent/nodes/reason.py — ReAct 推理节点
Node 4.3  agent/nodes/execute_tool.py — MCP 工具调用节点（含分级权限）
Node 4.4  agent/nodes/escalate.py — 升级/人工介入节点
Node 4.5  agent/graph.py — 组装状态机 + Checkpoint 持久化
```

### Layer 5：业务层

```
Node 5.1  flow/intake.py — 消息接入分发
Node 5.2  flow/ticket.py — 工单/队列/升级逻辑
Node 5.3  api/server.py — FastAPI API + Dashboard 接口
```

### Layer 6：控制台

```
Node 6.1  console/ — Next.js 项目骨架（shadcn/ui + Tailwind）
Node 6.2  console/pages/sessions.tsx — 实时会话监控
Node 6.3  console/pages/agents.tsx — 人格调试 + 注入干预
```

---

## 架构拓扑

```
              ┌──────────── OneBot v11 ────────────┐
              │   NapCat (QQ)   Gewechat (微信)     │
              └────────────┬───────────────────────┘
                           │ IMEvent
              ┌────────────▼───────────────────────┐
              │   lingxi.im.UnifiedIMAdapter       │
              └────────────┬───────────────────────┘
                           │
              ┌────────────▼───────────────────────┐
              │   lingxi.agent.LangGraph ReAct     │  ◄── State + Checkpoint
              │   ┌──────────┐  ┌───────────────┐  │
              │   │ Persona  │  │ Memory Pipe   │  │
              │   │ Injector │  │ (Mem0 + Graph)│  │
              │   └──────────┘  └───────────────┘  │
              └────────────┬───────────────────────┘
                           │ MCP
              ┌────────────▼───────────────────────┐
              │   lingxi.agent.PermissionedToolBus  │
              │   [Auto | Confirm | Approve | Deny] │
              └────────────┬───────────────────────┘
                           │
              ┌────────────▼───────────────────────┐
              │   Shell │ File │ Browser │ API      │
              │        (分级权限管控)               │
              └────────────────────────────────────┘

              ┌────────────────────────────────────┐
              │   lingxi.console (Next.js + WS)    │
              │   监控│介入│调试│票据│统计│配置     │
              └────────────────────────────────────┘
```

---

## 权限分级模型

| 级别 | 操作 | 策略 |
|---|---|---|
| L0 — 只读 | 搜索/读取/知识检索 | 自动放行 |
| L1 — 读+低风险写 | 发消息/建工单/写备注 | 自动放行 |
| L2 — 中风险 | 写文件/调外部 API | **需用户确认**（群内@确认） |
| L3 — 高风险 | Shell 执行/删文件/系统指令 | **需管理员审批**（坐席批准） |
| L4 — 红线 | 改配置/提权/访问凭据 | **始终拒绝**，仅代码层操作 |

---

## 开发顺序

1. Layer 0 → 跑通测试/配置/协议基础
2. Layer 1 → NapCat 连通，能收发 QQ 消息
3. Layer 2 → 人格引擎跑通，能按配置回复
4. Layer 3 → 记忆挂接，能跨会话持久
5. Layer 4 → LangGraph ReAct 跑通，能多步推理 + 工具调用
6. Layer 5 → 业务管线完整（工单/队列/升级）
7. Layer 6 → 控制台达到可监控可干预水平

每层内部按 Node 编号递增，前序 Node 导出的符号被后续 Node 直接引用，**绝不重复导入**。
