---
doc_id: fusion-agent-studio/product-page
product: fusion-agent-studio
class: platform
entry: service
doc_type: product-page
title: {zh: 产品页, en: Product Page}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
  - agent_runtime/api_server.py
  - agent_runtime/daemon_server.py
  - start.sh
---

# Fusion-MLX Agent Studio 产品页

中文：fusion-agent-studio 是 fusion 生态的本地智能体编排平台（class=platform）。它把图执行引擎、38 个内置工具、插件/MCP/ACP 三个扩展面和一套持久化存储收敛为一个守护进程，GUI（fusion-studio）与 HTTP 客户端都通过这个守护进程构建和运行智能体，模型推理全部经由本地 fusion-mlx 完成，无云端调用。

English: fusion-agent-studio is the local agent orchestration platform of the fusion ecosystem (class=platform). It packages a graph execution engine, 38 built-in tools, three extension surfaces (plugins / MCP / ACP) and persistent storage into one daemon; the GUI (fusion-studio) and HTTP clients both build and run agents through it. All inference goes through the local fusion-mlx server, with no cloud calls.

## 平台定位 / Platform Positioning

```yaml
platform:
  serves: local agent development and serving for the fusion ecosystem (agent editor, runtime, publishing)
  builds_on:
    - fusion-harness (agent kernel: funnels / hooks / permissions / compaction, hard dependency >= 0.3.0)
    - fusion-core (config / key resolution / LLM client base, hard dependency >= 0.8.0)
    - fusion-mlx (model inference via HTTP, :11434 through gateway :11432)
    - fusion-memory / fusion-rag / fusion-store (optional backends, env-gated)
  hosts:
    - JSON-RPC 2.0 daemon over Unix domain socket (default /tmp/fusion-studio.sock)
    - HTTP REST API on :11442 (/v1 and /api/v1 aliases, /health, /metrics)
    - WebSocket event stream on :11433 (only with FUSION_ENABLE_WS=1)
    - MCP server entry (fusion-mcp, agent_runtime/mcp_server.py)
    - cron triggers, plugin auto-load, SQLite persistence
  integration_surface:
    - fusion-studio GUI talks UDS JSON-RPC (module.action methods, e.g. task.submit)
    - fusion-gateway web-shell modules proxy REST with the module key (FUSION_AGENT_STUDIO_API_KEY)
    - external agent CLIs join via ACP (agent-client-protocol)
    - union-web menu registration + heartbeat via shell_registry to the gateway
  extension_points:
    - drop-in Python plugins in ~/.fusion-agent-studio/plugins/ (BaseTool subclasses)
    - MCP inbound tools (FUSION_MCP_ALLOWLIST gate)
    - optional fusion-identity multi-tenancy (FUSION_IDENTITY_ENABLED)
    - optional RAG backends (sqlite-vec default, fusion_store HNSW via FUSION_RAG_BACKEND)
```

## 消费方不再需要自己写什么 / What consumers no longer write

中文：

- 智能体图执行引擎（11 种节点类型：start/llm/tool/condition/loop/parallel/end/error_handler/rag/planner/verify）——消费方只提交 `AgentGraph` 定义，不再实现调度循环。约束：节点逻辑在 `_NodeExecutorsMixin` 内，新增节点类型需改本仓库。
- 工具执行与安全审批（38 个内置工具 + `FUSION_SAFETY_LEVEL` 分级审批）——消费方不再自己封装终端/文件/git/computer-use 工具，也不自己实现审批流。约束：默认策略为注入检测开 + L2，危险操作会被拦截或要求确认。
- 会话/检查点/任务持久化——`AgentStore` 与 `TaskStore` 落在 `~/.fusion-agent-studio/store.db`，消费方不再写自己的存储层。约束：SQLite 单文件，无网络存储。
- 进程托管——start.sh 提供生命周期、日志轮转（默认 10MB 滚动、保留 5 个归档）与 launchd 自启。约束：仅 macOS。

English:

- Agent graph execution engine (11 node types) — consumers submit an `AgentGraph` definition and do not implement the scheduling loop. Constraint: node execution logic lives in `_NodeExecutorsMixin` in this repo; new node types require changes here.
- Tool execution and safety approval (38 built-in tools plus `FUSION_SAFETY_LEVEL` tiers) — consumers do not wrap terminal/file/git/computer-use tools or build approval flows themselves. Constraint: the default policy is injection detection on plus L2; dangerous operations are blocked or require confirmation.
- Session / checkpoint / task persistence — `AgentStore` and `TaskStore` live in `~/.fusion-agent-studio/store.db`; consumers do not build their own storage. Constraint: single-file SQLite, no network storage.
- Process hosting — start.sh covers lifecycle, log rotation (default 10MB, 5 archives kept) and launchd auto-start. Constraint: macOS only.

## 边界 / Boundaries

中文：本平台不加载 MLX 模型（推理走 fusion-mlx HTTP API），不做云端 LLM 调用，不替代 fusion-cowork 的工作流 DAG 引擎。GUI 交付物在 fusion-studio 仓库，本仓库的 web/ 控制台是 vite 单页，经 gateway 代理接入。

English: this platform does not load MLX models (inference uses the fusion-mlx HTTP API), makes no cloud LLM calls, and does not replace the fusion-cowork workflow DAG engine. The GUI deliverable lives in the fusion-studio repo; the web/ console here is a vite single page accessed through the gateway.
