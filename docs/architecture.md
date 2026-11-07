---
doc_id: fusion-agent-studio/architecture
product: fusion-agent-studio
class: platform
entry: service
doc_type: architecture
title: {zh: 架构说明, en: Architecture}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - agent_runtime/runtime.py
  - agent_runtime/graph.py
  - agent_runtime/daemon_server.py
  - agent_runtime/api_server.py
---

# 架构说明 / Architecture

## 总览 / Overview

中文：平台由一个 Python 进程承载多个服务面：守护进程（UDS JSON-RPC，fusion-studio 主 API）、HTTP REST（:11442）、可选 WS 事件（:11433）、可选 MCP server（fusion-mcp）。所有 LLM 调用经 `FusionMLXClient`/`llm_gateway` 发往 fusion-gateway :11432（可覆盖直连 fusion-mlx :11434），不直接 import MLX。

English: one Python process hosts multiple surfaces: the daemon (UDS JSON-RPC, the primary API of fusion-studio), HTTP REST (:11442), optional WS events (:11433), and an optional MCP server (fusion-mcp). All LLM calls go through `FusionMLXClient`/`llm_gateway` to fusion-gateway :11432 (overridable to fusion-mlx :11434 direct); MLX is never imported directly.

```
fusion-studio (GUI) --UDS JSON-RPC--> DaemonServer --> AgentRuntime --> AgentGraph (walks nodes)
                                        |                  |                        |
HTTP clients --:11442 REST--> api_server.py               |                   FusionMLXClient (HTTP)
                                        |                  |                        |
                                    SubDispatchers    ToolRegistry (38 tools) --> fusion-gateway :11432
                                    (16 modules)          |                    --> fusion-mlx :11434
                                                          v
                                                    plugin system / MCP inbound
```

## 核心数据流 / Core data flow

中文：1) 构建 `AgentGraph`（`NodeConfig` 节点 + `Edge` 连接，`agent_runtime/graph.py`）；2) `AgentRuntime.execute_graph(graph, input)` 是异步状态机，逐节点执行并以 `AsyncIterator[AgentEvent]` 输出事件，`execute_stream()` 提供逐 token 流式；3) LLM 调用走 `FusionMLXClient`（HTTP）；4) 工具调用经 `ToolRegistry` 分发到 `BaseTool` 子类，结果回填 LLM 上下文，支持并行工具调用；5) RPC 进入 `DaemonServer._dispatch()` 后路由到核心 handler 或 16 个 SubDispatcher。约束：`AgentRuntime` 是 4 个 mixin 的组合（`_SafetyApprovalMixin`、`_CheckpointMixin`、`_DynamicToolsMixin`、`_NodeExecutorsMixin`），新增节点逻辑只改 `_runtime_nodes.py`，不改 `runtime.py` 公共接口。

English: 1) build an `AgentGraph` (`NodeConfig` nodes joined by `Edge`, `agent_runtime/graph.py`); 2) `AgentRuntime.execute_graph(graph, input)` is an async state machine that walks nodes and yields `AgentEvent` as an `AsyncIterator`; `execute_stream()` adds per-token streaming; 3) LLM calls go through `FusionMLXClient` (HTTP); 4) tool calls dispatch via `ToolRegistry` to `BaseTool` subclasses and feed results back into the LLM context, with parallel tool calls supported; 5) RPC lands in `DaemonServer._dispatch()` and routes to core handlers or the 16 SubDispatchers. Constraint: `AgentRuntime` is a composition of 4 mixins (`_SafetyApprovalMixin`, `_CheckpointMixin`, `_DynamicToolsMixin`, `_NodeExecutorsMixin`); new node logic goes in `_runtime_nodes.py` only, without changing the public interface of `runtime.py`.

## 包结构 / Package layout

| Package | Responsibility (zh/en) |
| --- | --- |
| agent_runtime/ | 核心引擎：图模型、运行时、上下文、事件、持久化、daemon_server + 16 dispatchers、api_server、llm_gateway、safety、memory_engine、telemetry、triggers、templates、workflow_engine、task_store、SDK / core engine: graph model, runtime, context, events, persistence, daemon server + dispatchers, REST API, LLM gateway, safety, memory, telemetry, triggers, templates, workflow engine, task store, SDK |
| tools/ | 38 个内置工具（`BaseTool` 子类）+ `ToolRegistry`；fusion-browser 可用时为 39 / 38 built-in tools plus the registry; 39 when fusion-browser tools are available |
| server/ | 兄弟服务 HTTP 桥：`FusionMLXClient`、`FusionRAGClient`、`FusionMLXProcessManager` / HTTP bridges to sibling services |
| web/ | vite 单页控制台，经 gateway 代理接入 / vite console SPA, accessed through the gateway |
| sdk/ | TypeScript SDK / TypeScript SDK |
| docs/guides/ | 14+ 篇场景指南 / scenario guides |

## 节点类型与持久化 / Node types and persistence

中文：`NodeType` 共 11 种：`start`、`llm`、`tool`、`condition`、`loop`、`parallel`、`end`、`error_handler`、`rag`、`planner`、`verify`。持久化为 SQLite：`AgentStore`（图/会话/检查点）与 `TaskStore` 落在 `~/.fusion-agent-studio/store.db`；数据模型全部是 `@dataclass` + `to_dict()`/`from_dict()`，无 ORM。安全层（`agent_runtime/safety.py`）默认注入检测 + L2 审批，事件经 `telemetry.py` 与 `audit_logger.py` 记录。

English: `NodeType` has 11 values: `start`, `llm`, `tool`, `condition`, `loop`, `parallel`, `end`, `error_handler`, `rag`, `planner`, `verify`. Persistence is SQLite: `AgentStore` (graphs/sessions/checkpoints) and `TaskStore` live in `~/.fusion-agent-studio/store.db`; all data models are `@dataclass` with `to_dict()`/`from_dict()`, no ORM. The safety layer (`agent_runtime/safety.py`) defaults to injection detection plus L2 approval; events are recorded via `telemetry.py` and `audit_logger.py`.

## 集成与扩展面 / Integration and extension surfaces

中文：env 门控的可选集成（缺失时优雅降级）：`FUSION_IDENTITY_ENABLED`（fusion-identity 多租户）、`FUSION_FSB_ENABLED`（FSB chat）、`FUSION_RAG_BACKEND=fusion_store`（HNSW 向量后端）、`FUSION_ENABLE_WS`（WS 事件）。扩展面：drop-in 插件（`~/.fusion-agent-studio/plugins/` 下放置含 `BaseTool` 子类的 .py 文件，`PluginManager` 自动加载）、MCP 入站工具（`FUSION_MCP_ALLOWLIST` 白名单）、ACP 外部 agent CLI。约束：插件在守护进程同进程内执行，安全依赖 L2 审批与注入检测，不要加载不受信任来源的插件。

English: env-gated optional integrations (degrade gracefully when absent): `FUSION_IDENTITY_ENABLED` (fusion-identity multi-tenancy), `FUSION_FSB_ENABLED` (FSB chat), `FUSION_RAG_BACKEND=fusion_store` (HNSW vector backend), `FUSION_ENABLE_WS` (WS events). Extension surfaces: drop-in plugins (put .py files with `BaseTool` subclasses into `~/.fusion-agent-studio/plugins/`; `PluginManager` auto-loads them), MCP inbound tools (allowlist via `FUSION_MCP_ALLOWLIST`), and external agent CLIs via ACP. Constraint: plugins run inside the daemon process; safety relies on L2 approval and injection detection — do not load plugins from untrusted sources.
