# Claude AI Agent 对标整改方案 & 落地计划

> 生成日期: 2026-07-28
> 范围: fusion-agent-studio (核心引擎) + fusion-studio (前端GUI)
> 原则: 只修改这两个项目，其他组件提 issue/PR

---

## 一、对标差距分析

### 1.1 Claude AI Agent 核心能力矩阵

| 能力维度 | Claude 现状 | Fusion 现状 | 差距等级 |
|---------|------------|------------|---------|
| **流式执行** | SSE token级流式，runtime全链路流式 | execute_graph()单次调用，runtime内无流式 | 🔴 严重 |
| **安全网关** | 3层安全分类器 + 自动降级路由 + CVP | SafetyGateway已实现但未接入runtime | 🟡 中等 |
| **检查点恢复** | 1M上下文 + prompt cache + 自主内存管理 | AgentStore存在但无自动checkpoint，无resume | 🔴 严重 |
| **真实嵌入** | 原生embedding API | LLMGateway.embed()返回hash伪向量 | 🔴 严重 |
| **MCP工具** | 原生MCP协议支持 | FusionMLXClient有MCP方法但未接入ToolRegistry | 🟡 中等 |
| **推理力度** | effort级别 (low/medium/high/xhigh/max) | 不支持 | 🟡 中等 |
| **动态工具** | 运行中可增删工具，不破坏prompt cache | 静态工具列表 | 🟡 中等 |
| **自主验证** | 自检+自纠+迭代直到成功 | Planner存在但无验证循环 | 🔴 严重 |
| **记忆管理** | 上下文即活文档，自动整理、过期、校正 | MemoryEngine存在但未接入runtime上下文 | 🔴 严重 |
| **多Agent编排** | Managed Agents + 子Agent委派 + @提及路由 | 6种模式存在但未生产化 | 🟡 中等 |
| **统一对话** | 单一Chat体验，分支、编辑、历史 | 4个独立聊天系统(CodeAgent/DesignBridge/Artifact/Agent) | 🔴 严重 |
| **DAG可视化** | 实时执行动画 + 事件流 | Canvas存在但断连，硬编码示例数据 | 🔴 严重 |
| **工具管理UI** | 完整工具浏览器/配置/开关 | 无任何工具管理界面 | 🔴 严重 |
| **安全审批UI** | 可视化审批/拒绝流程 | SafetyView存在但未连接SafetyGateway | 🟡 中等 |
| **Computer Use** | OS级桌面自动化 | 不存在 | 🟠 较大 |
| **定时任务** | Cowork产品级调度(每日定时执行) | CronManager存在但只支持分钟级 | 🟡 中等 |
| **连接器市场** | 完整安装/卸载/执行流程 | 仅展示卡片，无安装流程 | 🟡 中等 |
| **知识库** | 原生向量检索 | fusion-kb独立存在但未接入RAG | 🟡 中等 |

差距等级: 🔴严重(阻塞) > 🟠较大(影响体验) > 🟡中等(功能缺失)

---

## 二、整改方案

### 2.1 Phase 1: 核心Runtime修复 (Week 1-2)

**目标**: 让AgentRuntime具备Claude级别的流式执行、安全管控、状态恢复能力

#### P1-1: Runtime流式执行引擎
- **现状**: `execute_graph()` 调用 `LLMGateway.chat()` (单次请求)
- **方案**: 新增 `execute_graph_stream()` 方法，内部使用 `FusionMLXClient.chat_stream()`
- **变更**:
  - `agent_runtime/runtime.py`: 新增 `execute_graph_stream()` 异步生成器
  - 新增 `StreamAgentEvent` 类型: `TOKEN`, `THINKING_TOKEN`, `TOOL_CALL_START`, `TOOL_CALL_END`
  - LLM节点yield token级事件，tool节点yield开始/结束事件
  - 保持 `execute_graph()` 向后兼容，内部委托给stream版本

#### P1-2: SafetyGateway接入Runtime
- **现状**: SafetyGateway独立存在，tool执行完全绕过
- **方案**: 在 `_execute_tool_node()` 和 `_execute_llm_node()` 中注入安全检查
- **变更**:
  - `agent_runtime/runtime.py`: 构造函数接受 `safety_gateway: SafetyGateway | None`
  - `_execute_tool_node()`: 调用 `safety_gateway.request_approval()` 前置检查
  - `_execute_llm_node()`: 内容模式匹配后置检查
  - 新增 `SafetyApprovalEvent` 事件类型，yield到前端等待审批

#### P1-3: 自动检查点 + 恢复
- **现状**: `AgentStore.save_checkpoint()` 存在但runtime从不调用
- **方案**: runtime自动在每个节点执行后checkpoint
- **变更**:
  - `agent_runtime/runtime.py`: `_advance_node()` 后自动 `store.save_checkpoint()`
  - 新增 `resume_from_checkpoint(checkpoint_id)` 方法
  - 新增 `CheckpointEvent` 事件类型
  - 可配置: `auto_checkpoint=True/False`, `checkpoint_interval=1`

#### P1-4: 真实Embedding接入
- **现状**: `LLMGateway.embed()` 返回hash伪向量
- **方案**: 调用 fusion-mlx `/v1/embeddings` API
- **变更**:
  - `agent_runtime/llm_gateway.py`: `embed()` 和 `_call_embed()` 改用 `FusionMLXClient.embeddings()`
  - `agent_runtime/rag_pipeline.py`: 向量检索模式使用真实embedding
  - `FusionMLXClient` 新增 `embeddings()` 方法

#### P1-5: 推理力度(Effort Level)支持
- **现状**: LLM调用无effort参数
- **方案**: 在NodeConfig和LLM调用中支持effort参数
- **变更**:
  - `agent_runtime/graph.py`: `NodeConfig` 新增 `effort: str | None` 字段
  - `agent_runtime/llm_gateway.py`: `chat()` 和 `chat_stream()` 传递 `extra_body={"reasoning_effort": effort}`
  - fusion-mlx已支持 `reasoning_effort` 参数

#### P1-6: MCP工具接入ToolRegistry
- **现状**: FusionMLXClient有MCP方法但ToolRegistry不识别
- **方案**: 动态将MCP工具注册为BaseTool子类
- **变更**:
  - 新增 `tools/mcp_tool.py`: `MCPTool(BaseTool)` 动态适配器
  - `ToolRegistry` 新增 `register_mcp_server(server_name)` 方法
  - 调用 `FusionMLXClient.mcp_list_tools()` 获取工具schema，自动注册

---

### 2.2 Phase 2: 统一对话系统 (Week 3-4) ✅ COMPLETED

**目标**: 消除4个碎片化聊天系统，建立Claude式统一对话体验

#### P2-1: 统一ChatEngine (fusion-agent-studio) ✅
- ✅ 新增 `agent_runtime/chat_engine.py`: ChatSession, ChatEngine, ChatEvent, ChatMode
- ✅ `agent_runtime/persistence.py`: chat_sessions表 + 4个CRUD方法
- ✅ 16个单元测试全部通过

#### P2-2: 统一ChatView (fusion-studio) ✅
- ✅ 新增 `FusionStudio/Components/UnifiedChatView.swift`
- ✅ 新增 `FusionStudio/Bridge/ChatSessionStore.swift`
- ✅ daemon_server.py: 7个chat.* JSON-RPC handler

#### P2-3: 流式事件显示 ✅
- ✅ `daemon_server.py`: WebSocket服务器 (port 11435) + broadcast
- ✅ 新增 `FusionStudio/Bridge/StreamingBridge.swift`: TCP NWConnection
- ✅ StreamingBridge + ChatSessionStore 注入FusionStudioApp环境
- ✅ UnifiedChatView引用StreamingBridge显示流式指示器

---

### 2.3 Phase 3: DAG可视化 + 工具管理 (Week 5-6) ✅ COMPLETED

**目标**: DAG Canvas活起来，工具可管理

#### P3-1: DAG Canvas连接后端 ✅
- ✅ `DAGCanvasView.swift`: 连接AgentBridge, loadFromBridge/saveToBridge/executeGraph
- ✅ `AgentBridge.swift`: 新增 updateGraph(), fetchTools(), getTool()
- ✅ `daemon_server.py`: 新增 graph.update, tool.list, tool.get handlers
- ✅ 右键菜单添加节点, Save/Reload/Run按钮

#### P3-2: 工具浏览器 ✅
- ✅ 新增 `ToolBrowserView.swift`: 搜索/分类过滤/参数展示/测试面板
- ✅ 后端 tool.list/tool.get IPC已接入

#### P3-3: 安全审批UI ✅
- ✅ `SafetyView.swift`增强: L2预览展开区域, L3 GATEWAY标签(file_write/shell_exec)
- ✅ 审批队列已有, approve/reject已连接
- **现状**: SafetyView未连接SafetyGateway
- **方案**: 实时安全审批流程
- **变更**:
  - fusion-studio `SafetyView.swift`:
    - L2预览: 显示diff/计划，确认/拒绝按钮
    - L3网关: 每个操作需显式审批
    - 审批队列: 待审批列表
  - AgentBridge: WebSocket推送安全审批请求
  - daemon_server.py: safety.approve/reject IPC

---

### 2.4 Phase 4: 多Agent生产化 (Week 7-8) ✅ COMPLETED

**目标**: 多Agent编排从demo变为生产可用

#### P4-1: 自主验证循环 ✅
- **现状**: Planner分解任务但无验证
- **方案**: 执行→验证→修正循环
- **交付**:
  - `agent_runtime/graph.py`: NodeType增加"verify"
  - `agent_runtime/context.py`: AgentEventType增加VERIFY
  - 新增 `agent_runtime/verifier.py`:
    - `VerificationResult`: passed/score/issues/suggestion + to_dict/from_dict
    - `VerificationEngine`: verify()自动重试循环 + re_verify()二次验证
    - LLM调用解析JSON结果，失败自动重试，最多3次
  - `agent_runtime/runtime.py`: `_execute_verify_node()` 双路径(execute_graph + execute_graph_stream)
  - `agent_runtime/planner.py`: `auto_verify`参数 + `_generate_verify_steps()`自动生成验证步骤
  - `agent_runtime/daemon_server.py`: `verify.verify` IPC handler
  - `tests/test_verifier.py`: 11项测试全部通过

#### P4-2: 记忆管理接入Runtime ✅
- **现状**: MemoryEngine独立，runtime上下文静态
- **方案**: 上下文即活文档
- **交付**:
  - `agent_runtime/runtime.py`:
    - `memory_engine`参数注入
    - 执行前自动 `memory_engine.recall_relevant()` 加载相关记忆
    - 执行后自动 `_auto_store_memory()` 存储结果
    - 双路径(execute_graph + execute_graph_stream)均已接入
  - `agent_runtime/memory_engine.py`:
    - 新增 `recall_relevant(query, limit, scope)` → 格式化字符串返回
    - 新增 `auto_forget(max_entries, min_importance)` → 自动清理低重要度记忆
  - `agent_runtime/daemon_server.py`: `memory.recall_relevant` + `memory.auto_forget` IPC handlers

#### P4-3: Agent执行仪表盘 ✅
- **现状**: 无Agent运行状态可视化
- **方案**: 实时监控面板
- **交付**:
  - 新增 `FusionStudio/Modules/AgentDashboardView.swift`:
    - Agent列表(状态圆点+名称+进度指示)
    - Token用量统计(Prompt/Completion/Total)
    - 事件流实时滚动(按类型着色)
    - 自动5秒刷新+手动刷新
    - Agent详情面板(状态/迭代/启动时间/错误)
  - `AgentBridge.swift`: 新增 `listSessions()` 方法
  - `daemon_server.py`: 新增 `session.list` handler

#### P4-4: 市场完整流程 ✅
- **现状**: 展示卡片，无安装流程
- **方案**: 安装/卸载/实例化完整流程
- **交付**:
  - `TemplateMarketView.swift` 增强:
    - 详情页增加操作反馈(成功/失败消息)
    - 安装/卸载/应用按钮增加loading状态和错误提示
    - 卸载改用正确的`marketplaceUninstall`(而非unpublish)
  - `AgentBridge.swift`: 新增 `marketplaceUninstall()` 方法
  - `daemon_server.py`: 新增 `marketplace.uninstall` handler

---

### 2.5 Phase 5: 生态集成 (Week 9-10) ✅ COMPLETED

**目标**: 跨组件协作，Computer Use，定时任务

#### P5-1: Computer Use工具 ✅
- **交付**:
  - 新增 `tools/computer_use_tools.py`:
    - `ScreenCaptureTool`: 截屏 (macOS CGWindowListCreateImage), 支持全屏/区域/base64/文件输出
    - `MouseTool`: 点击/双击/右键/移动/拖拽/滚动 (CGEvent), 支持拖拽步进动画
    - `KeyboardTool`: 输入文本/按键/快捷键 (CGEvent), 完整macOS虚拟键码映射
    - `ClipboardTool`: 剪贴板读写 (pbpaste/pbcopy)
  - 辅助功能权限检测 + 自动提示授权
  - 平台守卫: 非macOS返回明确错误
  - 依赖: pyobjc-framework-Quartz, pyobjc-framework-ApplicationServices
  - 注册到默认registry: 19→23工具
  - `tests/test_computer_use.py`: 16项测试
  - **待提issue**: fusion-mlx支持 `computer_use` 类型tool (截图→视觉模型→操作)

#### P5-2: fusion-kb接入RAG ✅
- **交付**:
  - `agent_runtime/rag_pipeline.py`:
    - 新增 `VectorRetrievalStrategy`: HTTP调用fusion-kb API (/v1/search)
    - 健康检查 + 可用性缓存 (`is_available()`)
    - RAGConfig.mode="vector"/"hybrid_vector" 走向量检索
    - 降级: fusion-kb不可用时自动回退到FTS
    - `RAGPipeline.__init__` 新增 `vector_strategy` 参数
  - `agent_runtime/daemon_server.py`: `rag.vector_search` IPC handler
  - **待提issue**: fusion-kb暴露HTTP API供远程调用

#### P5-3: 定时任务生产化 ✅
- **交付**:
  - `agent_runtime/triggers.py`:
    - 完整5字段cron解析 (minute/hour/dom/month/dow): 支持 `*`, `*/N`, `N`, `N-M`, `N,M`
    - SQLite持久化 (`cron.db`): jobs + executions 两张表
    - `CronExecution` 数据类: 记录每次执行 (status/error/duration)
    - `CronJob` 新增: input_data, max_retries, retry_count + to_dict/from_dict
    - 执行重试: 失败自动重试直到max_retries
    - `CronManager.close()`: 停止循环+关闭DB
  - `agent_runtime/daemon_server.py`: cron.register/unregister/list/list_executions IPC handlers
  - `FusionStudio/Modules/CronManagerView.swift`:
    - Job列表(状态圆点+名称+下次执行时间)
    - 执行历史(状态+耗时+错误)
    - 新建Job表单(cron表达式+graph_id+input_data)
    - 删除Job(右键菜单)
    - 30秒自动刷新
  - `AgentBridge.swift`: cronRegister/cronUnregister/cronList/cronListExecutions

#### P5-4: 动态工具注册 ✅
- **交付**:
  - `agent_runtime/runtime.py`:
    - `_dynamic_tool_schemas()`: register_tool + unregister_tool OpenAI schema
    - LLM tool_call 拦截: func_name=="register_tool"/"unregister_tool" 走专用处理
    - `_dynamic_register_tool()`: 创建工具实例(terminal/http/custom)并注册到registry
    - `_dynamic_unregister_tool()`: 从registry移除工具
    - 工具schema自动注入: to_openai_schemas()后追加动态工具schema
    - 双路径(execute_graph + execute_graph_stream)均已接入
  - `agent_runtime/daemon_server.py`: tool.dynamic_register/tool.dynamic_unregister IPC handlers

---

### 2.6 Phase 6: Runtime生产化 (Week 11-12) ✅ COMPLETED

**目标**: 安全审批真正生效、Token预算可控、Chat分支可导航、错误自动修复

#### P6-1: SafetyGateway异步审批流 ✅
- **交付**:
  - `agent_runtime/runtime.py`:
    - 新增 `_safety_futures: dict[str, asyncio.Future]` 存储待审批action
    - `_await_safety_approval()`: yield SAFETY_APPROVAL(pending_approval) → await Future(60s超时) → approved/rejected/timeout
    - `_execute_llm_node()/_execute_tool_node()`: L2/L3 requires_approval时走异步等待而非直接block
    - 区分"硬block"(无approval路径)与"需审批block"(L3 gateway)
    - `approve_action(action_id)` / `reject_action(action_id)`: resolve Future
  - `agent_runtime/context.py`: AgentEventType增加 SAFETY_TIMEOUT
  - `agent_runtime/daemon_server.py`: safety.approve/safety.reject IPC handlers

#### P6-2: TokenBudget — 会话级token预算与执行中断 ✅
- **交付**:
  - 新增 `agent_runtime/token_budget.py`:
    - `TokenBudget` 数据类: max_tokens, spent_tokens, prompt_tokens, completion_tokens, pricing
    - `record_usage(prompt, completion)`: 累计token消耗
    - `is_exceeded()` / `remaining()`: 预算检查
    - `estimate_cost(model)`: 基于pricing估算费用
    - `status(model)`: 完整状态快照
    - `to_dict()`/`from_dict()`: 序列化
  - `agent_runtime/runtime.py`:
    - `execute_graph()/execute_graph_stream()` 接受 `token_budget: TokenBudget | None`
    - LLM节点执行后自动record_usage并检查预算
    - 超预算yield TOKEN_BUDGET_EXCEEDED事件并停止执行
  - `agent_runtime/context.py`: AgentEventType增加 TOKEN_BUDGET_EXCEEDED
  - `agent_runtime/daemon_server.py`: budget.set/budget.status IPC handlers

#### P6-3: Chat分支导航 ✅
- **交付**:
  - `agent_runtime/chat_engine.py`:
    - `switch_branch(session_id, message_id)`: 切换active_branch到指定消息
    - `get_branches(session_id, message_id)`: 列出某消息的所有兄弟分支(含is_active/content_preview)
    - `get_message_tree(session_id)`: 返回完整消息树结构(nodes + active_branch + total_messages)
  - `agent_runtime/daemon_server.py`: chat.switch_branch/chat.branches/chat.message_tree IPC handlers

#### P6-4: 迭代自修复循环 ✅
- **交付**:
  - `agent_runtime/graph.py`: NodeConfig新增 `retry_on_error: bool = False` + to_dict/from_dict
  - `agent_runtime/runtime.py`:
    - LLM节点tool_call错误收集(`tool_errors`列表)
    - 当retry_on_error=True且max_retries>0时进入自修复循环
    - 每次重试: 注入错误提示 → 重新调用LLM → 执行新tool_calls → 检查结果
    - yield RETRY事件(含retry_count和errors), 成功yield RETRY_SUCCESS
    - 最多5次重试(安全上限)
  - `agent_runtime/context.py`: AgentEventType增加 RETRY, RETRY_SUCCESS
  - `tests/test_phase6.py`: 26项测试全部通过

---

## 三、落地优先级与依赖关系

```
Phase 1 (核心Runtime) ─────────────────────────────────────┐
  P1-1 流式执行 ──────────┐                                  │
  P1-2 安全网关接入 ──────┤                                  │
  P1-3 检查点恢复 ────────┼──→ Phase 2 (统一对话) ─────────┤
  P1-4 真实Embedding ────┤     P2-1 ChatEngine              │
  P1-5 推理力度 ──────────┤     P2-2 ChatView               │
  P1-6 MCP工具 ───────────┘     P2-3 事件流显示 ────────────┤
                                                            │
                                              Phase 3 (DAG+工具) ──┤
                                                P3-1 DAG连接     │
                                                P3-2 工具浏览器   │
                                                P3-3 安全审批UI ──┤
                                                                 │
                                              Phase 4 (多Agent) ───┤
                                                P4-1 自主验证     │
                                                P4-2 记忆管理     │
                                                P4-3 执行仪表盘   │
                                                P4-4 市场流程 ────┤
                                                                 │
                                              Phase 5 (生态集成) ───┘
                                                P5-1 Computer Use
                                                P5-2 fusion-kb接入
                                                P5-3 定时任务
                                                P5-4 动态工具
```

---

## 四、跨项目Issue/PR清单

以下需求涉及其他fusion组件，需提issue/PR:

| 目标项目 | Issue标题 | 内容 |
|---------|----------|------|
| fusion-mlx | 支持 `reasoning_effort` 参数透传 | 让chat/completions API支持effort级别 |
| fusion-mlx | 支持 `computer_use` 类型tool | 截图→视觉模型→操作循环 |
| fusion-mlx | OpenClaw agent stream事件增强 | 增加TOOL_CALL_START/END, THINKING事件 |
| fusion-kb | 暴露HTTP API供远程调用 | 向量检索/文档入库/状态查询 |
| fusion-core | 新增embeddings()方法 | 调用fusion-mlx /v1/embeddings |
| fusion-core | 新增chat_stream()方法 | SSE流式chat接口 |
| fusion-code | 作为AgentStudio工具集成 | 暴露编程能力为MCP工具 |
| fusion-desk | 自动化能力MCP化 | 桌面自动化暴露为MCP工具 |

---

## 五、关键架构决策

### 5.1 对话模式统一

```
                  ┌──────────────┐
                  │  ChatEngine  │  ← 统一入口
                  │  (singleton) │
                  └──────┬───────┘
                         │ mode
         ┌───────┬───────┼───────┬────────┐
         ▼       ▼       ▼       ▼        ▼
      simple   agent    code   design    rag
         │       │       │       │        │
         ▼       ▼       ▼       ▼        ▼
     单轮对话  图执行  代码编辑  设计生成  知识检索
```

### 5.2 事件流架构

```
AgentRuntime                daemon_server              fusion-studio
    │                           │                          │
    │ execute_graph_stream()    │                          │
    │ ─── StreamAgentEvent ───► │ WebSocket push ────────► │ AgentBridge.onEvent()
    │                           │                          │ │
    │                           │                          │ ├─► ChatView (token显示)
    │                           │                          │ ├─► DAGCanvas (节点动画)
    │                           │                          │ ├─► Inspector (状态更新)
    │                           │                          │ └─► Dashboard (指标采集)
```

### 5.3 安全管控流程

```
LLM输出 → SafetyGateway.pre_check() → 通过?
                                         │
                                    ┌────┴────┐
                                    ▼         ▼
                                   通过     拦截
                                    │         │
                                    ▼         ▼
                              Tool执行    安全审批Event
                              完成后      → 前端审批UI
                              post_check   → approve/reject
                                           → 修正或跳过
```

---

## 六、成功标准

| 指标 | 目标 |
|------|------|
| Runtime流式 | execute_graph_stream() yield token级事件，延迟 <100ms |
| 安全接入 | 所有tool/llm执行经过SafetyGateway，L3操作100%拦截 |
| 检查点 | 每节点自动checkpoint，resume成功率 >99% |
| Embedding | 真实向量检索，RAG recall@5 >80% |
| 统一对话 | 1个ChatView替代4个聊天系统，支持分支/编辑 |
| DAG Canvas | 从graph CRUD到执行动画全链路打通 |
| 工具管理 | 19+内置 + MCP + 插件 可视化管理 |
| 多Agent | 6种模式生产可用，自主验证循环 |
| Computer Use | 截屏+鼠标+键盘工具，基本桌面操作 |
