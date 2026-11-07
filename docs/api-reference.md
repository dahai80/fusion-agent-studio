---
doc_id: fusion-agent-studio/api-reference
product: fusion-agent-studio
class: platform
entry: service
doc_type: api-reference
title: {zh: API 参考, en: API Reference}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - agent_runtime/api_server.py
  - agent_runtime/daemon_server.py
  - agent_runtime/cli.py
  - agent_runtime/mcp_server.py
---

# API 参考 / API Reference

中文：本仓库暴露四个 API 面。本文件列出端点清单与认证方式；请求/响应字段以 `agent_runtime/api_server.py` 的 Pydantic 模型为准。

English: this repo exposes four API surfaces. This file lists endpoints and auth; request/response fields are defined by the Pydantic models in `agent_runtime/api_server.py`.

## 1. HTTP REST（:11442）/ HTTP REST (:11442)

中文：FastAPI 应用（`agent_runtime/api_server.py`），`/v1/*` 为主前缀，`/api/v1/*` 为兼容别名；`/health` 与 `/metrics` 无需认证。配置了 API key 时（`FUSION_AGENT_STUDIO_API_KEY` 或 apikeys 管理），其余端点要求 Bearer 认证（`_require_auth_if_configured`）。CORS 由 `FUSION_API_CORS_ORIGINS` 控制。

English: FastAPI app (`agent_runtime/api_server.py`); `/v1/*` is the primary prefix with `/api/v1/*` as compatibility aliases; `/health` and `/metrics` need no auth. When an API key is configured (`FUSION_AGENT_STUDIO_API_KEY` or the apikeys manager), all other endpoints require Bearer auth (`_require_auth_if_configured`). CORS is controlled by `FUSION_API_CORS_ORIGINS`.

| Method | Path | Purpose (zh/en) |
| --- | --- | --- |
| GET | /v1/health | 服务健康 / service health |
| GET | /v1/graphs | 列出图 / list graphs |
| POST | /v1/graphs | 保存图 / save a graph |
| GET | /v1/graphs/{graph_id} | 读取图 / read a graph |
| DELETE | /v1/graphs/{graph_id} | 删除图 / delete a graph |
| POST | /v1/graphs/{graph_id}/execute | 执行图并返回事件 / execute a graph, return events |
| GET/POST/PUT/DELETE | /v1/flows, /v1/flows/{flow_id} | 流程 CRUD（含 /restore） / flow CRUD (with /restore) |
| GET/POST | /v1/agents, /v1/agents/published | 智能体列表与已发布 / agents and published agents |
| GET | /v1/agents/{agent_id}/definition, /preview, /status, /history, /logs, /artifacts, /versions | 智能体子资源 / agent sub-resources |
| POST | /v1/agents/{agent_id}/test, /duplicate, /snapshot | 测试/复制/快照 / test, duplicate, snapshot |
| POST | /v1/agents/{agent_id}/versions/{version_id}/restore | 恢复版本 / restore a version |
| GET/POST | /v1/runs | 运行列表/创建运行 / list and create runs |
| GET | /v1/runs/{run_id}, /events, /result, /stream, /branches | 运行查询与流 / run queries and streaming |
| GET/POST | /v1/sessions, /v1/sessions/{session_id}/messages | 会话与消息 / sessions and messages |
| GET/POST/DELETE | /v1/knowledge-bases, .../files | 知识库与文件 / knowledge bases and files |
| GET | /v1/knowledge-bases/rag-status | RAG 后端状态 / RAG backend status |
| GET/POST/DELETE | /v1/cron, /v1/cron/{job_id}, GET /v1/cron/executions | 定时任务 / cron jobs |
| GET/POST/DELETE | /v1/api-keys | API key 管理 / API key management |
| GET | /v1/audit-logs | 审计日志 / audit logs |
| GET | /v1/dashboard, /v1/usage/summary | 面板与用量 / dashboard and usage |
| GET/POST/DELETE | /v1/connectors, /v1/webhooks | 连接器与决策 webhook / connectors and decision webhooks |
| GET | /v1/search, /v1/notifications/runs | 搜索与运行通知 / search and run notifications |
| GET | /health, /metrics | 健康/指标（无认证） / health and metrics (no auth) |

中文（约束）：`limit` 类分页参数有上限；`/v1/graphs/{graph_id}/execute` 的事件流与 SDK 的 `execute_stream()` 同源。字段命名遵守 OpenAI 工具调用格式。

English (constraints): pagination `limit` parameters are capped; the execute event stream shares the source with the SDK's `execute_stream()`. Tool schemas follow the OpenAI function-calling format.

## 2. UDS JSON-RPC 2.0（守护进程）/ UDS JSON-RPC 2.0 (daemon)

中文：`agent_runtime/daemon_server.py` 在 `/tmp/fusion-studio.sock`（或 `FUSION_SOCKET_DIR` 私有目录，0700；`FUSION_STUDIO_SOCKET` 可指定完整路径）提供 JSON-RPC 2.0，方法名 `module.action`（如 `task.submit`、`agent.create`）。分发到 16 个 SubDispatcher（agent/artifact/chat/deploy/infra/knowledge/marketplace/mcp/memory/planner/plugin/safety/team/trainer/workflow）。socket 权限 0600 且校验对端 UID；`FUSION_AUTH_ENFORCE` 可开启认证。这是 fusion-studio GUI 的主 API。

English: `agent_runtime/daemon_server.py` serves JSON-RPC 2.0 on `/tmp/fusion-studio.sock` (or a private dir via `FUSION_SOCKET_DIR`, mode 0700; full path via `FUSION_STUDIO_SOCKET`). Methods are named `module.action` (e.g. `task.submit`, `agent.create`) and dispatch to 16 SubDispatcher modules (agent/artifact/chat/deploy/infra/knowledge/marketplace/mcp/memory/planner/plugin/safety/team/trainer/workflow). The socket is 0600 with peer-UID checks; `FUSION_AUTH_ENFORCE` enables auth. This is the primary API of the fusion-studio GUI.

## 3. WebSocket 事件（:11433）/ WebSocket events (:11433)

中文：默认关闭；`FUSION_ENABLE_WS=1` 时启动，端口可用 `FUSION_WS_PORT` 覆盖（默认 11433），令牌 `FUSION_WS_TOKEN`。注意 11433 与 fusion-memory fm-server、fusion-cowork sync 的默认端口相邻，start.sh 会在冲突时告警。

English: off by default; starts with `FUSION_ENABLE_WS=1`, port overridable via `FUSION_WS_PORT` (default 11433), token via `FUSION_WS_TOKEN`. Note 11433 is adjacent to the fusion-memory fm-server and fusion-cowork sync defaults; start.sh warns on conflicts.

## 4. MCP 与 CLI / MCP and CLI

中文：`fusion-mcp`（`agent_runtime/mcp_server.py`）把运行时能力以 MCP server 形式暴露，工具白名单由 `FUSION_MCP_ALLOWLIST` 控制。`fusion-agent` CLI 子命令：`submit` / `attach` / `resume` / `fork` / `inject` / `list` / `show` / `cancel` / `approvals`（create/list/approve/reject + webhooks add/list/remove）/ `apikeys`。ACP 客户端（`agent-client-protocol`）用于接入外部 agent CLI（如 claude-code-acp）。

English: `fusion-mcp` (`agent_runtime/mcp_server.py`) exposes runtime capabilities as an MCP server; the tool allowlist is controlled by `FUSION_MCP_ALLOWLIST`. The `fusion-agent` CLI subcommands: `submit` / `attach` / `resume` / `fork` / `inject` / `list` / `show` / `cancel` / `approvals` (create/list/approve/reject plus webhooks add/list/remove) / `apikeys`. The ACP client (`agent-client-protocol`) connects external agent CLIs (e.g. claude-code-acp).

## 错误与鉴权约定 / Error and auth conventions

中文：认证未配置时本地调用放行（本地优先）；配置后统一 401/403。错误响应体遵循 FastAPI `{"detail": ...}` 形态。审批流通过 `approvals` CLI 或 `/v1/webhooks` 注册决策回调。

English: without auth configured, local calls pass through (local-first); once configured, endpoints return 401/403 consistently. Error bodies follow the FastAPI `{"detail": ...}` shape. Approval flows use the `approvals` CLI or `/v1/webhooks` to register decision callbacks.
