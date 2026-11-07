---
doc_id: fusion-agent-studio/config-reference
product: fusion-agent-studio
class: platform
entry: service
doc_type: config-reference
title: {zh: 配置参考, en: Configuration Reference}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - start.sh
  - pyproject.toml
  - agent_runtime/paths.py
  - agent_runtime/api_server.py
---

# 配置参考 / Configuration Reference

中文：配置有三个来源：环境变量（运行时）、`~/.fusion-agent-studio/` 下的数据/密钥文件（持久化）、可选的 `~/.fusion-agent-studio/env.local`（start.sh 以 `set -a` 加载）。API key 解析收敛在 `fusion_core.config.client_key()` 单点（`FUSION_CLIENT_KEY` 优先，弃用别名带告警，dev 匿名兜底）；`settings.json` 的 `auth.api_key` 经 `resolve_api_key()` 加载。

English: configuration comes from three places: environment variables (runtime), data/key files under `~/.fusion-agent-studio/` (persistent), and an optional `~/.fusion-agent-studio/env.local` sourced by start.sh with `set -a`. API key resolution is centralized in `fusion_core.config.client_key()` (`FUSION_CLIENT_KEY` first, deprecated aliases warn, dev anonymous fallback); `settings.json` `auth.api_key` loads via `resolve_api_key()`.

## 服务与网络 / Service and network

| Variable | Default (verified) | Purpose (zh/en) |
| --- | --- | --- |
| FUSION_STUDIO_SOCKET | unset (uses /tmp/fusion-studio.sock) | UDS 完整路径 / full UDS socket path |
| FUSION_SOCKET_DIR | unset | 私有 socket 目录（0700），优于 /tmp / private socket dir (0700), beats /tmp |
| FUSION_ENABLE_WS | 0 | WebSocket 事件开关 / WebSocket events switch |
| FUSION_WS_PORT | 11433 | WS 端口 / WS port |
| FUSION_WS_TOKEN | unset | WS 鉴权令牌 / WS auth token |
| FUSION_API_CORS_ORIGINS | unset | REST CORS 允许来源 / REST CORS allowed origins |
| FUSION_AUTH_ENFORCE | unset | 强制 UDS 认证 / enforce UDS auth |
| FUSION_REGISTRY | ~/fusion/architecture/port-registry.yaml | 端口登记表路径 / port registry path |
| FUSION_SKIP_REGISTRY_CHECK | unset | 旁路端口自检（带 warn） / bypass registry self-check (warns) |

## 模型与上游 / Model and upstream

| Variable | Default (verified) | Purpose (zh/en) |
| --- | --- | --- |
| FUSION_MLX_PORT | 11432 | 上游目标端口：默认经 fusion-gateway，直连 fusion-mlx 时设 11434 / upstream target port: gateway by default, set 11434 to go direct |
| FUSION_GATEWAY_URL | unset | 完整 LLM base URL 覆盖（优先于 FUSION_MLX_PORT） / full LLM base URL override (takes priority over FUSION_MLX_PORT) |
| FUSION_SHELL_GATEWAY_URL | http://127.0.0.1:11432 | union-web 注册目标 / union-web registration target |
| FUSION_LLM_TIMEOUT | unset | LLM HTTP 超时 / LLM HTTP timeout |
| FUSION_RAG_BACKEND | sqlite-vec | RAG 后端；fusion_store 需另行构建安装 / RAG backend; fusion_store must be built separately |
| FUSION_MEMORY_API_KEY | unset | fusion-memory 调用密钥 / fusion-memory call key |

## 安全 / Safety

| Variable | Default (verified) | Purpose (zh/en) |
| --- | --- | --- |
| FUSION_SAFETY_INJECTION | 1（start.sh 注入） / set to 1 by start.sh | 注入检测开关 / injection detection switch |
| FUSION_SAFETY_LEVEL | L2（start.sh 注入） / set to L2 by start.sh | 审批分级；L2=注入 block + 危险操作 preview / approval tier; L2 = inject block + dangerous preview |
| FUSION_SAFETY_BACKEND | unset | 安全后端覆盖 / safety backend override |
| FUSION_PLUGINS_ENABLE | 1（start.sh 注入） / set to 1 by start.sh | 插件自动加载 / plugin auto-load |
| FUSION_MCP_ALLOWLIST | unset | MCP 工具白名单 / MCP tool allowlist |
| FUSION_CLIENT_KEY | unset | 客户端 key 单点解析 / single-point client key |
| FUSION_ADMIN_KEY | unset 或 ~/.fusion-agent-studio/gateway_admin_key / or the file | union-web 注册管理密钥 / union-web registration admin key |
| FUSION_AGENT_STUDIO_API_KEY | 自动生成于 ~/.fusion-agent-studio/web_module_key（0600） / auto-generated at that file (0600) | REST/模块 Bearer key / REST and module Bearer key |

## 扩展与运行时 / Extensions and runtime

| Variable | Default (verified) | Purpose (zh/en) |
| --- | --- | --- |
| FUSION_IDENTITY_ENABLED | unset | 多租户身份（需 extras [identity]） / multi-tenant identity (needs the [identity] extra) |
| FUSION_FSB_ENABLED | unset | FSB chat 集成 / FSB chat integration |
| FUSION_CRON_TZ | unset | 定时任务时区 / cron timezone |
| FUSION_TOOLSEARCH | unset | 工具检索开关 / tool search switch |
| FUSION_SUBAGENT_TOOLS | unset | 子代理工具集 / subagent toolset |
| FUSION_SUB_GRAPH_MAX_DEPTH | unset | 子图最大深度 / max sub-graph depth |
| FUSION_PARALLEL_BRANCH_CONCURRENCY | unset | 并行分支并发 / parallel branch concurrency |
| FUSION_WORKFLOW_CONCURRENCY | unset | 工作流并发 / workflow concurrency |
| FUSION_TASK_DB / FUSION_TASK_TTL / FUSION_TASK_CONCURRENCY | unset | 任务存储控制 / task store controls |
| FUSION_LOG_MAX_SIZE | 10485760 (start.sh) | 日志滚动阈值（字节） / log rotation threshold (bytes) |
| FUSION_LOG_KEEP_COUNT | 5 (start.sh) | 保留归档数 / archives kept |

## 数据目录 / Data directory

中文：`~/.fusion-agent-studio/`：`store.db`（图/会话/检查点 SQLite）、`plugins/`（drop-in 工具插件）、`web_module_key`（union-web 模块 key，0600）、`gateway_admin_key`（注册管理 key）、`env.local`（本地秘密，不入库不入 git）、`seats/`（席位配置，随包分发 `agent_runtime/seats/*.yaml`）。`FUSION_PREFIX` 可重定位部分路径（以 `agent_runtime/paths.py` 为准）。

English: `~/.fusion-agent-studio/`: `store.db` (graphs/sessions/checkpoints SQLite), `plugins/` (drop-in tool plugins), `web_module_key` (union-web module key, 0600), `gateway_admin_key` (registration admin key), `env.local` (local secrets, never committed), and `seats/` (seat configs shipped as `agent_runtime/seats/*.yaml`). `FUSION_PREFIX` relocates some paths (authoritative source: `agent_runtime/paths.py`).

## 约束 / Constraints

中文：改端口必须先改 `architecture/port-registry.yaml` 再改配置并跑 `python scripts/check-ports.py`；`0.0.0.0` 绑定需在登记表 `purpose` 字段写明理由。密钥一律放 `~/.fusion-agent-studio/` 或 env.local，不要写进仓库。未在表中列出的 `FUSION_*` 变量（如 `FUSION_TASK_EXECUTOR_DISABLED`、`FUSION_SAFETY_TIMEOUT`）以源码 grep 为准，取值待补充 / TBD。

English: port changes must update `architecture/port-registry.yaml` first, then the project config, then run `python scripts/check-ports.py`; binding `0.0.0.0` requires a documented reason in the registry `purpose` field. Keep secrets in `~/.fusion-agent-studio/` or env.local, never in the repo. `FUSION_*` variables not listed here (e.g. `FUSION_TASK_EXECUTOR_DISABLED`, `FUSION_SAFETY_TIMEOUT`) are documented by source grep; values remain TBD.
