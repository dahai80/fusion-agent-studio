<div align="center">

# Fusion-MLX Agent Studio

**Apple Silicon 本地智能体开发平台**

在 Mac 上运行、构建和编排 AI 智能体 — 无需云服务、无需 API 费用、数据不出设备。

[![Version](https://img.shields.io/badge/v0.4.0-blue.svg)]()
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-1644-success.svg)](tests/)

**[English](README.md)** · [快速开始](#快速开始) · [架构](#架构) · [文档](docs/) · [示例](examples/)

</div>

---

## 快速开始

### 前置条件

- macOS Apple Silicon (M1–M5)
- Python 3.11+
- [fusion-mlx](https://github.com/dahai80/fusion-mlx)（模型服务）

### 安装

```bash
# 克隆
git clone https://github.com/dahai80/fusion-agent-studio.git
cd fusion-agent-studio

# 安装
pip install -e .

# 运行测试
pip install -e ".[test]"
pytest tests/
```

### 最小示例

```python
import asyncio
from agent_runtime import AgentRuntime, AgentGraph, NodeConfig
from tools import create_default_registry
from server.fusion_mlx_client import FusionMLXClient

async def main():
    # 1. 连接 fusion-mlx（需先启动）
    mlx = FusionMLXClient(base_url="http://localhost:11434/v1")

    # 2. 构建简单智能体图
    graph = AgentGraph(name="我的第一个智能体")
    graph.add_node("start", NodeConfig(type="start", label="开始"))
    graph.add_node("llm", NodeConfig(type="llm", label="思考", model="qwen3.5-9b"))
    graph.add_node("end", NodeConfig(type="end", label="结束"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")

    # 3. 执行
    registry = create_default_registry()
    runtime = AgentRuntime(mlx, registry)
    async for event in runtime.execute_graph(graph, "你好！"):
        print(f"[{event.type.value}] {event.content[:100]}")

asyncio.run(main())
```

### 启动 fusion-mlx（必须）

```bash
# 终端 1：启动模型服务
fusion-mlx serve --model qwen3.5-9b --port 11434

# 终端 2：运行智能体
python my_agent.py
```

---

## 架构

```
┌───────────────────────────────────────────────────────────────┐
│  fusion-studio (SwiftUI GUI)                                  │
│  IPCClient ──UDS JSON-RPC──> /tmp/fusion-studio.sock         │
│  AgentBridge ──IPCClient──> graph.* / mlx.* / planner.* ...  │
└──────────────────────────┬────────────────────────────────────┘
                           │ UDS JSON-RPC 2.0
┌──────────────────────────▼────────────────────────────────────┐
│  fusion-agent-studio (Python 守护进程)                        │
│                                                               │
│  ┌─────────────────────┐   ┌─────────────────┐               │
│  │  智能体运行时        │   │  工具系统         │               │
│  │  ┌───────────────┐  │   │  ┌───────────┐  │               │
│  │  │ 状态机        │  │   │  │ 31 个工具 │  │               │
│  │  │ 图执行器      │  │   │  │ 注册表    │  │               │
│  │  │ 编排器        │  │   │  │ 插件      │  │               │
│  │  │ 调试器        │  │   │  └───────────┘  │               │
│  │  │ 持久化        │  │   │                 │               │
│  │  └───────┬───────┘  │   └─────────────────┘               │
│  └──────────┼──────────┘                                     │
│             │ HTTP API                                        │
│  ┌──────────▼──────────────────────────────────────────────┐  │
│  │  FusionMLX Client (httpx → localhost:11434)             │  │
│  │  不导入 MLX/engine/pool — 纯 HTTP 通信                   │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  守护进程服务器 (UDS JSON-RPC 2.0)                            │
│  11 个子分发器: agent · chat · deploy · infra ·               │
│  knowledge · marketplace · memory · planner · safety ·        │
│  team · workflow + 40 个核心 RPC                              │
└──────────────────────────┬────────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼────────────────────────────────────┐
│  fusion-mlx (模型服务)                                        │
│  /v1/chat/completions  /v1/models  /admin/api/*               │
│  MLX 运行时 · EnginePool · 40+ 量化格式                       │
└───────────────────────────────────────────────────────────────┘
```

### 核心模块

| 模块 | 描述 | 文件数 |
|------|------|--------|
| `agent_runtime/` | 核心引擎：图、状态机、编排器、调试器、持久化、API 服务器、守护进程服务器 (UDS JSON-RPC)、11 个子分发器、模板、桥接、编辑器、指标、市场、数据导入、沙箱、感知、FMP、知识、网关、集群、广场、规划器、RAG 管道、连接器、API Key 管理器、样式管理器、工作流引擎、会话管理器、遥测 | 54 |
| `agent_runtime/dispatchers/` | 11 个子分发器 — 从 DaemonServer 提取：agent、chat、deploy、infra、knowledge、marketplace、memory、planner、safety、team、workflow | 13 |
| `agent_runtime/sdk/` | 智能体 SDK：Agent、Tool、AgentClient，通过 JSON-RPC 编程访问 | 3 |
| `agent_runtime/plugins/` | 内置工作流插件：code_review、feature_dev、security_scan、pr_review、agent_builder | 5 |
| `tools/` | 内置工具系统：31 个工具 + 插件系统 | 11 |
| `server/` | fusion-mlx HTTP 客户端 + 进程管理器 | 2 |

---

## 功能特性

### 智能体运行时
- ✅ **状态机引擎** — LLM → 工具 → 观察 → 决策循环
- ✅ **9 种节点类型** — Start、LLM、Tool、Condition、Loop、End、Error Handler、RAG、Planner
- ✅ **多智能体编排** — 串行、并行、主从模式
- ✅ **步进调试器** — 断点、暂停/恢复、步过
- ✅ **变量管理器** — 跨节点变量传递与插值
- ✅ **JSON Schema** — 结构化输出约束
- ✅ **子图** — 可复用组合工作流
- ✅ **检查点/恢复** — SQLite 持久化，支持长时间运行智能体
- ✅ **Python 导出** — 将图导出为独立脚本
- ✅ **模板系统** — 8 个预设模板（代码审查、文件整理等）
- ✅ **Fusion-code 桥接** — 子进程桥接到 fusion-code CLI 智能体
- ✅ **API 服务器** — FastAPI + WebSocket，图管理与流式执行
- ✅ **v1 API 版本化** — 所有端点在 /v1 前缀下，支持分页 (page/limit/sort)
- ✅ **标准错误响应** — 30 个错误码，中文 user_message，对齐 Anthropic API 格式
- ✅ **认证中间件** — x-api-key 头认证，API Key 验证含 IP 白名单和智能体限制
- ✅ **速率限制器** — 按 Key 和按智能体的令牌桶 QPS 限制
- ✅ **守护进程服务器** — UDS JSON-RPC 2.0 服务器，用于 fusion-studio GUI 集成，11 个子分发器 + 40 个核心 RPC
- ✅ **子分发器架构** — DaemonServer 从 191 个 RPC 分解为 11 个独立子分发器 (agent、chat、deploy、infra、knowledge、marketplace、memory、planner、safety、team、workflow)，`__getattr__` 代理保持向后兼容
- ✅ **智能体生命周期** — draft → published → archived 状态流，版本追踪、API 端点生成、克隆、调试 execute_stream
- ✅ **智能体版本/快照** — VersionRecord 存储，快照/恢复/复制智能体版本
- ✅ **知识库实体** — 一等 KB CRUD、文件上传、智能体绑定、ETL 管道
- ✅ **审计日志** — SQLite 支持的管理员操作审计追踪，查询/导出
- ✅ **提示注入检测** — 14 模式正则检测器，防御越狱/注入攻击
- ✅ **仪表盘端点** — 聚合今日请求、Token 用量、活跃智能体、错误数
- ✅ **连接器安全** — 已移除 to_dict_full()，仅内部 _get_full_config()
- ✅ **连接器管理器** — OAuth2/API Key/Webhook 外部集成生命周期 (CRUD、连接/断开、测试)
- ✅ **API Key 管理器** — API Key 创建 (fk-* 前缀)、轮换、吊销、权限、智能体访问、IP 白名单
- ✅ **样式管理器** — 5 种内置输出样式 (formal-report、technical-doc、creative-writing、json-structured、concise-summary) + 自定义样式
- ✅ **仪表盘概览** — 聚合统计：智能体数量、日请求量、Token 用量、错误率、告警
- ✅ **分析** — 按智能体的使用追踪，支持时间范围 (日/周/月)
- ✅ **告警系统** — 预算警告、会话错误告警、确认
- ✅ **知识注入** — 运行时知识库上下文注入，支持 RAG 策略选择 (hybrid/keyword/semantic)
- ✅ **SwiftUI 端到端** — IPCClient (29 个便捷方法) + AgentBridge (8 个模块) + 4 个新视图 (PlannerView、MemoryView、SafetyView、DeployView) + RAGPipelineView/TemplateMarketView 桥接集成 + AgentStudioView
- ✅ **图编辑器** — DAG 验证、自动布局、可视化编辑器后端 (CRUD + 复制)
- ✅ **指标引擎** — SQLite 支持的推理/会话指标，聚合查询
- ✅ **智能体市场** — 导入/导出 .fusion-agent 包、搜索、分类、安装
- ✅ **数据导入** — 文档阅读器 (txt/md/json/csv/html)、ETL 管道、分块 (fixed-size、sentence、markdown-heading)
- ✅ **集群管理器** — 已迁移至 [fusion-multi-node](../fusion-multi-node/) — Apple Silicon 独立多节点集群
- ✅ **代码沙箱** — AST 安全分析、diff 预览、macOS sandbox-exec 隔离执行
- ✅ **三级感知引擎** — Debounce → AST diff → LLM gate 级联，文件变更显著性检测
- ✅ **FMP 路由 v2** — @Mention 路由、轮询调度、每智能体熔断器、消息去重
- ✅ **知识引擎** — SQLite-vec + FTS5 混合搜索、RRF 融合、作用域命名空间、自动嵌入
- ✅ **LLM 网关** — 统一模型代理，优先路由、能力匹配、回退链、每模型熔断器
- ✅ **集群路由** — 智能体切换，hop_count 限制 (最大 3)、任务委托、自动升级
- ✅ **广场广播** — 多智能体共享日志流，@Mention 触发、3 轮熔断器、人工介入、监督者指定
- ✅ **HITL L1/L2/L3 治理** — 自主 (L1)、diff 预览 (L2)、网关审批 (L3) 安全级别，分类策略
- ✅ **RAG 管道** — KnowledgeEngine 检索 → 上下文组装 → LLM 生成，集成为 DAG 节点类型
- ✅ **Fusion-RAG 集成** — `FusionRAGClient` (HTTP 代理到 fusion-rag `:11436/kb/*`) 语义搜索、混合 BM25+Vector (RRF)、上下文检索、重排序、RAG 问答、目录扫描/监控、项目 KB 映射；守护进程 `kb.search/ask/scan/health` RPC；REST `POST /v1/knowledge-bases/{kb_id}/search|ask|scan`、`GET /v1/knowledge-bases/rag-status`；fusion-rag 不可用时优雅降级
- ✅ **记忆自动压缩** — 分层记忆 (short_term/long_term/archive)，LLM 摘要与年龄/重要性提升
- ✅ **规划器节点** — OpenDevin 风格 "规划-确认-执行" 工作流，风险评估 (low/medium/high)
- ✅ **数据阅读器** — Web、GitHub、Notion、PDF、目录阅读器，LlamaIndex 风格文档导入
- ✅ **AgentPackage 工作区** — 快照/恢复工作区目录、.git 快照、源码管理、技能 DAG 导入/导出
- ✅ **智能体循环 (内生回灌)** - `loop_mode="agent"` LLM 节点在每轮工具调用后重新调用自身，直到 end_turn；每节点 `max_loop_iterations` 上限，stop_reason 驱动终止
- ✅ **上下文压缩** - 4 阶段管道 (microcompact → smart-truncate → hard-compact) + `reactive_strip` 413 恢复；确定性优先，MLX 可选；接入智能体循环每轮；`LLMGateway` 中反应式 413 自动重试
- ✅ **Hooks 生命周期** - `HookEngine` 10 个事件 (PRE/POST_TOOL_USE、SESSION_START/END、STOP、PRE_COMPACT、SUBAGENT_*、USER_PROMPT_SUBMIT)；回调 + 命令钩子，正则匹配，block/approve 决策
- ✅ **工作流引擎** — 6 种执行模式 (pipeline、parallel_barrier、loop_until_dry、loop_until_budget、adversarial_verify、judge_panel)；WorkflowConfig + WorkflowRun 生命周期
- ✅ **会话管理器** — 分叉会话 (异步后台任务)、attach/detach 事件流、background_list/kill
- ✅ **遥测引擎** — OTLP 兼容的 spans/traces/metrics；自动计数器 (llm_calls、tool_calls、tokens)；延迟追踪 (avg/p99)
- ✅ **智能体 SDK** — `Agent` + `Tool` + `AgentClient` 编程访问；JSON-RPC 2.0 over UDS；scaffold_agent 模板 (basic/coder/reviewer/researcher)
- ✅ **内置插件** — 5 个工作流清单：code_review (5 智能体 parallel→judge)、feature_dev (3 智能体 7 阶段 pipeline)、security_scan (3 智能体 parallel→adversarial→pipeline)、pr_review (6 智能体 parallel→adversarial→judge)、agent_builder (pipeline→pipeline→adversarial)
- ✅ **安全分类器** — 关键词风险评分 → auto_approve/preview/human_approve 分类
- ✅ **对抗性验证** — N 个怀疑者多数投票模式；可配置 voter_count + threshold
- ✅ **团队限制** — 并发智能体 + 深度限制
- ✅ **AX 无障碍** — 语义标注 + 屏幕阅读器描述
- ✅ **轮中模型切换** — 对话中切换 LLM
- ✅ **工具 Schema 懒加载** — 按需 OpenAI 兼容 Schema 生成

### 工具 (19 个内置)
| 分类 | 工具 |
|------|------|
| **文件** | `file_read`、`file_write`、`file_list` |
| **终端** | `terminal`（Shell 执行） |
| **Git** | `git`（status、log、diff、commit、branch、pull） |
| **文本** | `text_process`、`text_search` |
| **HTTP** | `http_request`（GET/POST/PUT/DELETE/PATCH） |
| **代码** | `code_execute`（子进程沙箱） |
| **数据** | `json_parse`、`csv_parse`、`base64` |
| **工具** | `date_time`、`uuid`、`hash`、`path_ops`、`zip` |
| **数据库** | `sqlite_query` |
| **标注** | `annotation`（文档注释） |

### 触发器
- ✅ **Webhook** — 外部事件触发
- ✅ **Cron** — 定时执行 (cron 表达式)

### 插件系统
- ✅ 动态加载用户自定义 Python 工具
- ✅ **Artifact FC 工具** — 5 个 artifact 工具 (get_source, create, update, create_snapshot, list_all)，支持上下文注入和主动裁剪
- ✅ 插件目录 `~/.fusion-agent-studio/plugins/`
- ✅ 新插件模板生成器

### 集成
- ✅ **fusion-mlx** — Apple Silicon 优化模型服务
- ✅ **OpenAI 兼容 API** — 兼容任何 OpenAI 兼容后端
- ✅ **macOS 原生** — SwiftUI 应用 + WKWebView 画布集成
- ✅ **i18n** — 中英双语 UI 字符串

---

## 项目结构

```
fusion-agent-studio/
├── agent_runtime/          # 核心引擎
│   ├── graph.py            # AgentGraph 数据模型
│   ├── context.py          # 执行上下文
│   ├── runtime.py          # 状态机引擎
│   ├── executor.py         # 节点执行器
│   ├── orchestrator.py     # 多智能体编排
│   ├── persistence.py      # SQLite 持久化
│   ├── exporter.py         # Python 脚本导出
│   ├── templates.py        # 预设模板 (8)
│   ├── api_server.py       # FastAPI + WebSocket 服务器
│   ├── daemon_server.py    # UDS JSON-RPC 2.0 守护进程 (40 核心 RPC)
│   ├── dispatchers/        # 11 个子分发器
│   │   ├── base.py         # SubDispatcher ABC
│   │   ├── agent.py        # 智能体生命周期处理
│   │   ├── chat.py         # 聊天引擎处理
│   │   ├── deploy.py       # 部署/导出处理
│   │   ├── infra.py        # 基础设施处理
│   │   ├── knowledge.py    # 知识/RAG 处理
│   │   ├── marketplace.py  # 市场处理
│   │   ├── memory.py       # 记忆管理处理
│   │   ├── planner.py      # 规划器处理
│   │   ├── safety.py       # 安全/验证处理
│   │   ├── team.py         # 团队/编排处理
│   │   └── workflow.py     # 工作流引擎处理
│   ├── fusion_code_bridge.py # fusion-code 子进程桥接
│   ├── agent_templates.py  # 8 个智能体配置模板
│   ├── graph_editor.py     # DAG 编辑器后端
│   ├── metrics_engine.py   # 推理指标
│   ├── agent_marketplace.py# 智能体市场
│   ├── data_ingestion.py   # 文档阅读器 + ETL + 分块
│   ├── code_sandbox.py     # AST 检查 + diff + sandbox-exec
│   ├── aware_engine.py     # 三级感知级联
│   ├── fmp_router.py       # FMP v2 (@Mention + 轮询 + 去重)
│   ├── knowledge_engine.py # SQLite-vec + FTS5 混合搜索 + RRF
│   ├── llm_gateway.py      # 统一模型代理 + 回退链
│   ├── swarm_router.py     # 智能体切换 + hop 限制 + 委托
│   ├── undo_manager.py     # 画布撤销/重做
│   ├── variable_manager.py # 变量管理
│   ├── json_schema.py      # 结构化输出
│   ├── debugger.py         # 步进调试器
│   ├── prompt_templates.py # 可复用提示模板
│   ├── sub_graph.py        # 子图支持
│   ├── triggers.py         # Webhook + Cron
│   ├── i18n.py             # 国际化
│   ├── deployer.py         # 一键部署
│   ├── connectors.py       # 外部连接器管理 (OAuth2/API Key/Webhook)
│   ├── apikey_manager.py   # API Key 生命周期 (创建/轮换/吊销)
│   ├── style_manager.py    # 输出样式模板 (5 内置 + 自定义)
│   ├── workflow_engine.py  # 6 模式工作流执行引擎
│   ├── session_manager.py  # 分叉/后台会话管理器
│   ├── telemetry.py        # OTLP 兼容遥测 (spans/traces/metrics)
│   ├── verifier.py         # 对抗性 N 怀疑者验证
│   └── safety.py           # 安全网关 + 风险分类器
├── sdk/                    # 智能体 SDK (编程访问)
│   ├── __init__.py         # Agent、Tool、AgentClient 公共 API
│   ├── agent.py            # Agent 数据类 + run/stream/fork
│   ├── tool.py             # Tool 数据类 + OpenAI Schema
│   └── client.py           # JSON-RPC 2.0 AgentClient
├── plugins/                # 内置工作流插件
│   ├── code_review/        # 5 智能体代码审查 (manifest.json)
│   ├── feature_dev/        # 3 智能体功能开发
│   ├── security_scan/      # 3 智能体安全扫描
│   ├── pr_review/          # 6 智能体 PR 审查
│   └── agent_builder/      # 智能体脚手架向导
├── tools/                  # 工具系统
│   ├── base.py             # BaseTool 抽象类
│   ├── registry.py         # 工具注册表
│   ├── file_tools.py       # 文件操作
│   ├── terminal_tools.py   # Shell 执行
│   ├── git_tools.py        # Git 操作
│   ├── text_tools.py       # 文本处理
│   ├── http_tools.py       # HTTP 请求
│   ├── code_tools.py       # 代码执行
│   ├── data_tools.py       # JSON/CSV/Base64
│   ├── utility_tools.py    # Date/UUID/Hash/Path/Zip
│   ├── db_tools.py         # SQLite + 标注 + 性能监控
│   └── plugin_manager.py   # 动态插件加载器
├── server/                 # fusion-mlx 通信
│   ├── fusion_mlx_client.py# HTTP 客户端
│   └── process_manager.py  # 进程生命周期
├── tests/                  # 1591 个测试
│   ├── test_runtime.py     # 运行时引擎测试
│   ├── test_graph.py       # 图模型测试
│   ├── test_tools.py       # 工具测试
│   ├── test_business_scenarios.py # 端到端业务场景测试
│   ├── test_agent_handlers.py    # 智能体/市场处理器测试
│   ├── test_workflow_engine.py   # 工作流引擎测试
│   ├── test_session_manager.py   # 会话管理器测试
│   ├── test_telemetry.py         # 遥测引擎测试
│   └── ...                 # 18+ 测试文件
└── examples/               # 示意图
    ├── code_assistant.json
    ├── file_organizer.json
    └── terminal_automation.json
```

---

## 与竞品对比

| 维度 | Dify.AI | Coze | n8n | LangFlow | **Agent Studio (我们)** |
|------|---------|------|-----|----------|------------------------|
| **推理引擎** | Ollama/外部 API | 云 API | 外部 API | 外部 API/Ollama | **fusion-mlx (MLX 原生)** |
| **Apple Silicon 性能** | ❌ | ❌ | ❌ | ❌ | **✅ 2-bit 量化，连续批处理** |
| **本地离线** | ⚠️ 部分 | ❌ | ✅ | ✅ | **✅ 100%** |
| **隐私** | ⚠️ 自托管 | ❌ 云端 | ✅ 自托管 | ✅ 自托管 | **✅ 数据不出设备** |
| **桌面原生** | ❌ Web | ❌ Web | ❌ Web | ❌ Web | **✅ macOS SwiftUI** |
| **系统工具** | ❌ | ❌ | ⚠️ 部分 | ❌ | **✅ 终端/文件/Git/Xcode** |
| **多模型** | ❌ (Ollama 单一) | ✅ 云 | ❌ | ❌ | **✅ EnginePool + MemoryEnforcer** |
| **量化** | ❌ (有限 GGUF) | ❌ | ❌ | ❌ | **✅ 40+ 格式，2-bit 极致** |
| **成本** | 云 API 费用 | 云 API 费用 | 免费 | 免费 | **零 API 费用，无限调用** |

---

## 开发

```bash
# 安装开发依赖
pip install -e ".[test]"

# 运行所有测试
pytest tests/

# 运行带覆盖率
pytest tests/ --cov=agent_runtime --cov=tools --cov=server

# 创建新工具
python -c "from tools.plugin_manager import PluginManager; from tools.registry import ToolRegistry; pm = PluginManager(ToolRegistry()); pm.create_plugin_template('my_tool')"
```

### 测试统计
- **1591 个测试**，0 失败
- **94%+ 语句覆盖率**
- **Python 3.11+** 兼容
- **16 个业务场景集成测试** 覆盖：智能体生命周期 (创建→配置→执行→删除)、技能管理、灵魂管理、市场 (发布→搜索→安装)、记忆 (存储→召回→删除)、安全 (检查→评估→策略)、规划器、部署导入/导出、模板、图 CRUD、智能体过滤、环境健康、RAG、ping

---

## 许可证

Apache License 2.0

## 致谢

- [fusion-mlx](https://github.com/dahai80/fusion-mlx) — Apple Silicon 模型服务
- [MLX](https://github.com/ml-explore/mlx) — Apple 机器学习框架
- [Dify.AI](https://github.com/langgenius/dify) — 可视化智能体编排参考
