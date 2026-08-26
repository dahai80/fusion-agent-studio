<div align="center">

# Fusion-MLX Agent Studio

**Local Agent Development Platform for Apple Silicon**

Run, build, and orchestrate AI agents entirely on your Mac — no cloud, no API fees, no data leaving your device.

[![Version](https://img.shields.io/badge/v0.3.49-blue.svg)]()
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-2246-success.svg)](tests/)

**[中文文档](README_CN.md)** · [Quick Start](#quick-start) · [Architecture](#architecture) · [Documentation](docs/) · [Examples](examples/)

</div>

---

## Quick Start

### Prerequisites

- macOS with Apple Silicon (M1–M5)
- Python 3.11+
- [fusion-mlx](https://github.com/dahai80/fusion-mlx) (for model serving)

### Install

```bash
# Clone
git clone https://github.com/dahai80/fusion-agent-studio.git
cd fusion-agent-studio

# Install
pip install -e .

# With local vector search (sqlite-vec + FTS5 + RRF hybrid)
pip install -e ".[rag]"

# With fusion-plugins-ecosystem bridge (PluginManifest/MCP/Claude gateway)
# NOTE: fusion-plugins-ecosystem is not on PyPI — install from the fusion monorepo
pip install -e ".[plugins]"

# Run tests
pip install -e ".[test]"
pytest tests/
```

### Minimal Example

```python
import asyncio
from agent_runtime import AgentRuntime, AgentGraph, NodeConfig
from tools import create_default_registry
from server.fusion_mlx_client import FusionMLXClient

async def main():
    # 1. Connect to fusion-mlx (must be running)
    mlx = FusionMLXClient(base_url="http://localhost:11434/v1")

    # 2. Build a simple agent graph
    graph = AgentGraph(name="My First Agent")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("llm", NodeConfig(type="llm", label="Think", model="qwen3.5-9b"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")

    # 3. Execute
    registry = create_default_registry()
    runtime = AgentRuntime(mlx, registry)
    async for event in runtime.execute_graph(graph, "Hello!"):
        print(f"[{event.type.value}] {event.content[:100]}")

asyncio.run(main())
```

### Start fusion-mlx (required)

```bash
# Terminal 1: Start the model server
fusion-mlx serve --model qwen3.5-9b --port 11434

# Terminal 2: Run your agent
python my_agent.py
```

---

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│  fusion-studio (SwiftUI GUI)                                  │
│  IPCClient ──UDS JSON-RPC──> /tmp/fusion-studio.sock         │
│  AgentBridge ──IPCClient──> graph.* / mlx.* / planner.* ...  │
└──────────────────────────┬────────────────────────────────────┘
                           │ UDS JSON-RPC 2.0
┌──────────────────────────▼────────────────────────────────────┐
│  fusion-agent-studio (Python daemon)                          │
│                                                               │
│  ┌─────────────────────┐   ┌─────────────────┐               │
│  │  Agent Runtime      │   │  Tool System     │               │
│  │  ┌───────────────┐  │   │  ┌───────────┐  │               │
│  │  │ State Machine │  │   │  │ 37 tools  │  │               │
│  │  │ Graph Executor│  │   │  │ Registry  │  │               │
│  │  │ Orchestrator  │  │   │  │ Plugin    │  │               │
│  │  │ Debugger      │  │   │  └───────────┘  │               │
│  │  │ Persistence   │  │   │                 │               │
│  │  └───────┬───────┘  │   └─────────────────┘               │
│  └──────────┼──────────┘                                     │
│             │ HTTP API                                        │
│  ┌──────────▼──────────────────────────────────────────────┐  │
│  │  FusionMLX Client (httpx → localhost:11434)             │  │
│  │  Never imports MLX/engine/pool — pure HTTP               │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  Daemon Server (UDS JSON-RPC 2.0)                             │
│  11 Sub-Dispatchers: agent · chat · deploy · infra ·          │
│  knowledge · marketplace · memory · planner · safety ·        │
│  team · workflow + 40 core RPCs                               │
└──────────────────────────┬────────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼────────────────────────────────────┐
│  fusion-mlx (model serving)                                   │
│  /v1/chat/completions  /v1/models  /admin/api/*               │
│  MLX runtime · EnginePool · 40+ quant formats                 │
└───────────────────────────────────────────────────────────────┘
```

### Key Modules

| Module | Description | Files |
|--------|-------------|-------|
| `agent_runtime/` | Core engine: graph, state machine, orchestrator, debugger, persistence, API server, daemon server (UDS JSON-RPC), 11 sub-dispatchers, templates, bridge, editor, metrics, marketplace, data ingestion, sandbox, aware, FMP, knowledge, gateway, swarm, plaza, planner, RAG pipeline, connectors, apikey manager, style manager, workflow engine, session manager, telemetry | 54 files |
| `agent_runtime/dispatchers/` | 11 Sub-Dispatchers extracted from DaemonServer — agent, chat, deploy, infra, knowledge, marketplace, memory, planner, safety, team, workflow | 13 files |
| `agent_runtime/sdk/` | Agent SDK: Agent, Tool, AgentClient for programmatic access over JSON-RPC | 3 files |
| `agent_runtime/plugins/` | Built-in workflow plugins: code_review, feature_dev, security_scan, pr_review, agent_builder | 5 manifests |
| `tools/` | Built-in tool system: 37 tools + plugin system (file, terminal, git, http, code, db, computer-use, artifact, plan, utility) | 17 files |
| `server/` | fusion-mlx HTTP client + process manager | 2 files |

---

## Features

### Agent Runtime
- ✅ **State machine engine** — LLM → tool → observe → decide loop
- ✅ **9 node types** — Start, LLM, Tool, Condition, Loop, End, Error Handler, RAG, Planner
- ✅ **Multi-agent orchestration** — Sequential, parallel, master-worker
- ✅ **Step debugger** — Breakpoints, pause/resume, step-over
- ✅ **Variable manager** — Cross-node variable passing with interpolation
- ✅ **JSON Schema** — Structured output enforcement
- ✅ **Sub-graphs** — Reusable composed workflows
- ✅ **Checkpoint/resume** — SQLite persistence for long-running agents
- ✅ **Python export** — Export graphs as standalone scripts
- ✅ **Template system** — 8 preset templates (code review, file organizer, etc.)
- ✅ **Fusion-code bridge** — Subprocess bridge to fusion-code CLI agent
- ✅ **API server** — FastAPI + WebSocket for graph management and streaming execution
- ✅ **v1 API versioning** — All endpoints under /v1 prefix with pagination (page/limit/sort)
- ✅ **Standard error responses** — 30 error codes with Chinese user_message, aligned with Anthropic API format
- ✅ **Auth middleware** — x-api-key header auth, API key validation with IP whitelist and agent restrictions
- ✅ **Rate limiter** — Token bucket per-key and per-agent QPS limiting
- ✅ **Daemon server** — UDS JSON-RPC 2.0 server for fusion-studio GUI integration, 11 sub-dispatchers + 40 core RPCs
- ✅ **Sub-Dispatcher architecture** — DaemonServer decomposed from 191 RPCs into 11 independent sub-dispatchers (agent, chat, deploy, infra, knowledge, marketplace, memory, planner, safety, team, workflow) with backward-compatible `__getattr__` proxy
- ✅ **Agent lifecycle** — draft → published → archived status flow with version tracking, API endpoint generation, clone, debug execute_stream, and `agent.unpublish` RPC (#159) to revert published/archived back to draft
- ✅ **Agent version/snapshot** — VersionRecord store, snapshot/restore/duplicate agent versions
- ✅ **Knowledge Base entity** — First-class KB CRUD, file upload, agent binding, ETL pipeline
- ✅ **Audit logging** — SQLite-backed admin action audit trail with query/export
- ✅ **Prompt injection detection** — 14 pattern regex detector for jailbreak/injection attempts
- ✅ **Dashboard endpoint** — Aggregated today requests, token usage, active agents, errors
- ✅ **Connector security** — Removed to_dict_full(), internal _get_full_config() only
- ✅ **Connector manager** — OAuth2/API Key/Webhook external integration lifecycle (CRUD, connect/disconnect, test)
- ✅ **API Key manager** — API key creation (fk-* prefix), rotation, revocation, permissions, agent access, IP whitelist
- ✅ **Style manager** — 5 builtin output styles (formal-report, technical-doc, creative-writing, json-structured, concise-summary) + custom styles
- ✅ **Dashboard overview** — Aggregated stats: agent counts, daily requests, token usage, error rates, alerts
- ✅ **Analytics** — Per-agent usage tracking by time range (day/week/month)
- ✅ **Alert system** — Budget warnings, session error alerts, acknowledgement
- ✅ **Knowledge injection** — Runtime knowledge base context injection with RAG strategy selection (hybrid/keyword/semantic)
- ✅ **SwiftUI end-to-end** — IPCClient (29 convenience methods) + AgentBridge (8 modules) + 4 new Views (PlannerView, MemoryView, SafetyView, DeployView) + RAGPipelineView/TemplateMarketView bridge integration + AgentStudioView (agent CRUD + configure + execute + BackendAgentDetailView + ConfigureAgentSheet)
- ✅ **Graph editor** — DAG validation, auto-layout, visual editor backend (CRUD + duplicate)
- ✅ **Metrics engine** — SQLite-backed inference/session metrics with aggregation queries
- ✅ **Agent marketplace** — Import/export .fusion-agent packages, search, categories, install
- ✅ **Data ingestion** — Document readers (txt/md/json/csv/html), ETL pipeline, chunking (fixed-size, sentence, markdown-heading)
- ✅ **Cluster manager** — Moved to [fusion-multi-node](../fusion-multi-node/) — standalone multi-node cluster for Apple Silicon
- ✅ **Code sandbox** — AST safety analysis, diff preview, macOS sandbox-exec isolation for code execution; multi-language support (#161) — python/shell/bash/javascript/swift/go (interpreted) + cpp/c (compiled via clang), with `agent.code_languages` RPC returning the environment's actually-available languages so the frontend renders the selector dynamically
- ✅ **3-Tier aware engine** — Debounce → AST diff → LLM gate cascade for file-change significance detection
- ✅ **FMP router v2** — @Mention routing, round-robin turns, per-agent circuit breaker, message dedup
- ✅ **Knowledge engine** — SQLite-vec + FTS5 hybrid search, RRF fusion, scoped namespaces, auto-embedding
- ✅ **LLM gateway** — Unified model proxy with priority routing, capability matching, fallback chain, per-model circuit breaker
- ✅ **Swarm router** — Agent handoff with hop_count limit (max 3), task delegation, auto-escalation
- ✅ **Plaza broadcast** — Multi-agent shared log stream with @Mention triggers, 3-round circuit breaker, human break-in, supervisor designate
- ✅ **HITL L1/L2/L3 governance** — Autonomous (L1), diff preview (L2), gateway approval (L3) safety levels with category-based policies; **`SafetyGateway` wired into the runtime (#174)** — the daemon now passes `safety_gateway` to `AgentRuntime`, so `evaluate_action` fires on tool calls in the loop and approve/reject RPCs operate against the live runtime; env `FUSION_SAFETY_LEVEL` (default L1) / `FUSION_SAFETY_INJECTION` (default off) preserve prior behavior
- ✅ **RAG pipeline** — KnowledgeEngine retrieval → context assembly → LLM generation, integrated as DAG node type
- ✅ **Fusion-RAG integration** — `FusionRAGClient` (HTTP proxy to fusion-rag at `:11436/kb/*`) with semantic search, hybrid BM25+Vector (RRF), contextual retrieval, reranking, RAG Q&A, directory scan/watch, project KB mapping; daemon `kb.search/ask/scan/health` RPC; REST `POST /v1/knowledge-bases/{kb_id}/search|ask|scan`, `GET /v1/knowledge-bases/rag-status`; graceful fallback when fusion-rag unavailable
- ✅ **Memory auto-compression** — Tiered memory (short_term/long_term/archive) with LLM-based summarization and age/importance promotion; **wired into the runtime execution loop (#174)** — `MemoryEngine` (auto-recall at session start, auto-store at session end, compaction summaries) is now constructed by the daemon and passed to `AgentRuntime`, so memory recall/store actually fires; cross-thread SQLite writes serialized via RLock + `check_same_thread=False`, `memory.db` co-located with `store.db` (test-isolated alongside `store_path`)
- ✅ **Planner node** — OpenDevin-style "plan-confirm-execute" workflow with risk assessment (low/medium/high)
- ✅ **Data readers** — Web, GitHub, Notion, PDF, Directory readers for LlamaIndex-style document ingestion
- ✅ **AgentPackage workspace** — Snapshot/restore workspace dirs, .git snapshots, source management, skill DAG import/export
- ✅ **Skill terminal action (#152, #156)** — `skill.execute` step supports `action="terminal"` (runs `command` via TerminalTool, `cwd`/`timeout` passthrough) with `capture_to` variable interpolation. Interpolation works all three directions (#156): `terminal→generate` (into prompt), `terminal→terminal` (into command), and `generate→terminal` (generate step `capture_to` captures LLM output for a later terminal command); enables self-contained `terminal(fetch) → generate(score) → terminal(publish)` pipelines in one skill
- ✅ **Agent Loop (内生回灌)** - `loop_mode="agent"` LLM node re-invokes itself after each tool round until end_turn; per-node `max_loop_iterations` cap, stop_reason-driven termination, Compaction/Hooks 接入点
- ✅ **Per-node model unload (optional, #149)** - Env `FUSION_AGENT_UNLOAD_MODEL_AFTER_NODE=1` asks fusion-mlx to unload the served model after an LLM node advances, lowering peak memory on multi-model workflow chains. Default OFF (preserves model reuse across consecutive same-model nodes); non-fatal — a failed/already-evicted unload never aborts the workflow; never fires during tool-call re-entry on the same node
- ✅ **Per-node model override (#176)** — LLM nodes with an explicit `model` now route to that model instead of the graph-level first-LLM fallback; nodes without a model inherit the graph model. Multi-LLM-node workflows run each node on its configured model
- ✅ **Context compaction** - 4-stage pipeline (microcompact → smart-truncate → hard-compact) + `reactive_strip` 413 recovery; deterministic-first, MLX optional; wired into Agent Loop each round; reactive 413 auto-retry wired into `LLMGateway` (strip + retry same model before fallback); compaction summaries persisted to `memory_engine` (auto-summary, scope=`compaction`)
- ✅ **Hooks lifecycle** - `HookEngine` with 10 events (PRE/POST_TOOL_USE, SESSION_START/END, STOP, PRE_COMPACT, SUBAGENT_*, USER_PROMPT_SUBMIT); callback + command hooks, regex matcher, block/approve decisions, `~/.fusion-agent-studio/hooks.json` config; exposed via daemon `hooks.list/register/test`; **all 7 lifecycle events now fire in the execution loop (#175)** — SESSION_START/USER_PROMPT_SUBMIT at session open, SESSION_END on clean completion, STOP on iteration cap (graph + agent-loop), PRE_COMPACT before every compaction site, SUBAGENT_START/STOP around sub-graph execution
- ✅ **Workflow engine** — 6 execution patterns (pipeline, parallel_barrier, loop_until_dry, loop_until_budget, adversarial_verify, judge_panel); WorkflowConfig + WorkflowRun lifecycle; daemon `workflow.*` RPC (9 methods)
- ✅ **Session manager** — Fork sessions (async background tasks), attach/detach event streams, background_list/kill; daemon `session.*` RPC (5 methods)
- ✅ **Telemetry engine** — OTLP-compatible spans/traces/metrics; **runtime-instrumented** (graph.execute/llm.call/tool.call spans — counters/latencies/tokens populated live); export to JSON/OTLP/console with HTTP-JSON push; daemon `telemetry.*` RPC (5 methods)
- ✅ **Agent SDK** — `Agent` + `Tool` + `AgentClient` classes for programmatic access; JSON-RPC 2.0 over UDS; scaffold_agent templates (basic/coder/reviewer/researcher); daemon `sdk.*` RPC (3 methods)
- ✅ **Built-in plugins** — 5 workflow manifests: code_review (5-agent parallel→judge), feature_dev (3-agent 7-phase pipeline), security_scan (3-agent parallel→adversarial→pipeline), pr_review (6-agent parallel→adversarial→judge), agent_builder (pipeline→pipeline→adversarial)
- ✅ **Safety classifier** — Keyword-based risk scoring → auto_approve/preview/human_approve classification; daemon `safety.classify_action` RPC
- ✅ **Adversarial verify** — N-skeptic majority vote pattern; configurable voter_count + threshold; daemon `verify.adversarial_verify` RPC
- ✅ **Team limits** — Concurrent agent + depth limits; daemon `team.set_limits/get_limits` RPC
- ✅ **AX accessibility** — Semantic annotations + screen-reader descriptions for agent outputs; daemon `session.accessibility` RPC
- ✅ **Mid-turn model switch** — Switch LLM mid-conversation; daemon `mlx.switch_model` RPC
- ✅ **Tool schema lazy load** — On-demand OpenAI-compatible schema generation; daemon `tool.get_schema` RPC
- ✅ **True streaming (#182)** — Per-token delivery across all boundary layers, no buffering. `execute_graph_stream` (stream=True) emits a `TOKEN` event per LLM delta; the WS path `/ws/execute/{graph_id}` now calls it (pushes `TOKEN` events instead of one buffered `THINK`); new SSE endpoint `GET /v1/graphs/{graph_id}/execute/stream?input=...` returns `text/event-stream` with per-event `data:` lines (TOKEN/THINK/TOOL_CALL/TOOL_RESULT/.../done); `agent.execute_stream` RPC yields TOKEN events. For incremental push-as-you-go use SSE or WS; the SDK `Agent.stream()` collects the full RPC event list
- ✅ **Soul unified loading + memory type classification (#200, C16)** — `resolve_soul_prompt(agent_id, fallback)` loads `~/.fusion-agent-studio/agents/<id>/.fusion-agent/soul.md` (soul.md takes precedence over `manifest.system_prompt`) and is now injected across **all execution paths**, not just `agent.execute`: chat (`_inject_soul` prepends soul as a system message, merging an existing one), workflow (`_run_agent` soul override + start-node backfill), and background sessions (`_run_background`). Memory gains a semantic `memory_type` column (user/feedback/project/reference, default project) on `MemoryEntry` + SQLite, with `store`/`recall`/`list_recent`/`count`/`recall_relevant` filtering by type, old-db ALTER migration, and a `classify_memory_type` heuristic auto-applied in `_auto_store_memory` (user-classified before feedback — "I prefer X" is identity, not a correction). RPC `memory.*` passes `memory_type` through. langgraph is a structural runner (delegates actual LLM calls to workflow_engine, already covered)
- ✅ **stop_on_tool_error graph flag (#202)** — Opt-in `AgentGraph.stop_on_tool_error: bool` (default `False`, backward compatible). When `True`, a direct tool node that errors (raises, `"Error:"` prefix, or `{"error":...}` JSON) sets `ctx.error`, emits a tagged `ERROR` event (`metadata={"tool_error": True, ...}`), and **returns before `output_mapping`** so no downstream variable is written — `execute_graph` stops the cascade the same way the LLM-driven tool path already does. Previously a tool error was silently flattened into a normal result and the DAG kept going, finishing `status=success` with 0 real output (cron-driven fan-growth graphs burned GPU past a gate then reported success). `daemon_server._handle_graph_execute` now counts `tool_errors` from event metadata and returns them; `_cron_default_handler` surfaces the count + first error into the cron execution `result_preview`, so `cron.list_executions` no longer hides a silent partial-success
- ✅ **AgentClient RPC timeout (#207)** — `AgentClient.call` now takes an optional `timeout` (per-call) and `default_timeout` (`__init__`) so a daemon that accepts the request but never responds cannot hang the caller forever. The `reader.readline()` is wrapped in `asyncio.wait_for`; a timeout returns `{"error": "rpc timeout <s>s for <method>"}` instead of awaiting permanently. `None` everywhere (default) preserves the legacy hang-forever path for in-process cron handlers that call the daemon directly. Connection errors (`ConnectionRefusedError`/`FileNotFoundError`) are unified into a friendly `"Daemon not running at <path>"` message. Prevents CLI/cron-trigger scripts from wedging on a daemon with a stuck event loop or long graph task
- ✅ **UDS socket security hardening (#209)** — The central router socket is no longer world-writable: `os.chmod` changed `0o666` → `0o600` (daemon + team dispatcher) so only the same-UID caller can connect. `_handle_client` now verifies the peer UID via `LOCAL_PEERCRED` (SOL_LOCAL, macOS) and closes non-owner connections — defense-in-depth against a widened-permission or `/tmp` TOC-TOU bind. Optional `FUSION_SOCKET_DIR` env moves the socket into a `0o700` private dir (roots out the `/tmp` race entirely); the default path `/tmp/fusion-studio.sock` is unchanged so downstream callers (fusion-cli) keep working without coordinated changes. `start.sh` resolves the socket path with the same env precedence (`FUSION_STUDIO_SOCKET` > `FUSION_SOCKET_DIR` > default)
- ✅ **Graph correctness bundle (#211–#215)** — Five downstream-driven graph-engine fixes: **#211** `variable_manager.interpolate` now emits valid JSON for complex types (dict/list/tuple → `json.dumps` double-quoted, `None`→`null`, bool→`true`/`false`) instead of Python `repr` (single quotes, bare `True`) that broke downstream `json.loads`; **#212** `ConditionEngine._resolve_literal` normalizes bool literals case-insensitively and `_coerce_pair` aligns cross-type operands before `_compare` so `True == "true"` and `5 == "5"` route correctly instead of silently mis-routing; **#213** `AgentGraph.stable_id()` hashes name+nodes+edges+start (md5[:16], sort_keys) for a content-stable graph_id; `graph.create` opt-in `stable_id=true` upserts so cron/cache/GUI bindings survive sync/rebuild; **#214** `FUSION_GRAPH_CONCURRENCY=N` env creates an `asyncio.Semaphore(N)` (in `start()`'s loop) throttling concurrent `graph.execute` — over-limit requests queue (not rejected), `0`/empty = unlimited (backward compatible); **#215** `AgentGraph.validate(tool_registry)` adds content-schema checks — unknown `tool_name`, unparseable `condition_expr` are hard errors; `output_mapping`/`tool_params` unknown keys are soft `warning:`; `graph.create` logs issues always, opt-in `strict_validate=true` rejects hard errors. Condition-expr checks are gated behind a registry so the runtime's pre-execute structural validate (no registry) doesn't block a condition node's own exception path

### Tools (36 built-in)
| Category | Tools |
|----------|-------|
| **File** | `file_read`, `file_write`, `file_edit` (in-place old→new), `file_delete`, `file_list`, `file_grep` (recursive content search), `file_glob` (recursive pattern find) |
| **Terminal** | `terminal` (shell execution) |
| **Git** | `git` (status, log, diff, commit, branch, pull, push, fetch, checkout, merge, rebase, reset, stash, show) |
| **Text** | `text_process`, `text_search` |
| **HTTP** | `http_request` (GET/POST/PUT/DELETE/PATCH) |
| **Code** | `code_execute` (subprocess sandbox), `code_sandbox` (sandbox-exec isolation + AST check, 8 languages) |
| **Data** | `json_parse`, `csv_parse`, `base64` |
| **Utility** | `date_time`, `uuid`, `hash`, `path_ops`, `zip` |
| **Database** | `sqlite_query` |
| **Annotation** | `annotation` (documentation notes) |

### Triggers
- ✅ **Webhook** — External event triggers
- ✅ **Cron** — Scheduled execution (cron expressions)
- ✅ **Task store** — Generic Task persistence (SQLite `~/.fusion-agent-studio/tasks.db`); status machine pending→running→completed/failed/canceled; triggers immediate/cron/run_at; daemon `task.*` RPC (8 methods: submit/list/get/status/cancel/rerun/delete/add_artifacts); cron-triggered tasks auto-register a CronJob and write back `cron_job_id`
- ✅ **Project aggregation** — Multi-Task board container via `project_id` label on tasks; daemon `project.*` RPC (project.list aggregates task counts/status distribution, project.tasks filters by project+optional status) for TaskBoardView

### Plugin System
- ✅ Dynamic loading of user-defined Python tools
- ✅ **Artifact FC tools** — 5 artifact tools (get_source, create, update, create_snapshot, list_all) with context injection and proactive pruning
- ✅ Plugin directory at `~/.fusion-agent-studio/plugins/`
- ✅ Template generator for new plugins

### MCP (Model Context Protocol) — inbound
- ✅ **Three transports** — `http` (JSON-RPC POST), `stdio` (spawn MCP server subprocess, JSON-RPC over stdin/stdout), `sse` (Server-Sent Events + POST)
- ✅ **Tool discovery** — `MCPRegistry.register_server()` discovers MCP tools via `tools/list` and registers each as an `MCPTool` (BaseTool) into the ToolRegistry, callable by agents
- ✅ **Resources & prompts** — `resources/list` and `prompts/list` discovery
- ✅ **Daemon RPC** — `mcp.register_server` / `mcp.list_servers` / `mcp.unregister_server` / `mcp.list_resources` / `mcp.list_prompts` (lazy registry, no idle spin until a server is registered)
- Usage: `mcp.register_server {"server_url": "http://localhost:3000/rpc"}` or `{"stdio_cmd": ["npx", "mcp-server-fs"]}` or `{"sse_url": "...", "post_url": "..."}`

### Plan-as-Mode (C6) — read-only explore + human approval gate
- ✅ **`graph.plan_mode` flag** — when `True` the graph runs in a read-only explore phase. Write tools (`file_write`, `file_edit`, `file_delete`, `terminal`, …) are gated off with a `plan_mode_blocked` tool result; read tools (`file_read`, `file_list`, `file_grep`, `file_glob`, `text_search`, `text_process`, `exit_plan_mode`, `register_tool`, `unregister_tool`) execute normally.
- ✅ **`exit_plan_mode` tool** — the transition primitive. The agent calls `exit_plan_mode {plan: "…"}` once a complete plan is presented. The runtime detects the `__EXIT_PLAN_MODE__` sentinel, flips `plan_mode=False`, emits a `PLAN_MODE_EXIT` event, and unlocks write tools for the rest of the run. Tool stays a pure `BaseTool` (no runtime coupling — sentinel is detected in the tool-call loop).
- ✅ **Planner node in-graph block** — a `planner` node with `tool_params={"await_approval": True}` blocks execution via an `asyncio.Future` keyed by `plan_id`, emitting `PLAN_APPROVAL` events (`pending_approval` → `approved`/`rejected`). RPC `planner.approve_plan` / `planner.reject_plan` resolve the in-graph future in addition to the `PlannerEngine` status flag (solves the two-`PlannerEngine`-instance problem — dispatcher's vs node's).
- Events: `PLAN_MODE_EXIT`, `PLAN_APPROVAL`. Tool count: 37.

### Parallel Tool Calls + tool_choice (C1)
- ✅ **`tool_choice` passthrough (end-to-end)** — `NodeConfig.tool_choice` (string, empty = omitted) accepts OpenAI values: `"auto"` | `"none"` | `"required"` | a JSON-encoded `{"type":"function","function":{"name":"…"}}`. The runtime threads it through all 3 LLM call sites (stream, non-stream, self-repair retry) → `LLMGateway.chat`/`chat_stream` (`**kwargs`) → `_call_model_async`/`_call_default_client` → `FusionMLXClient.chat`/`chat_stream` → HTTP payload (`payload.update(kwargs)`). Verified the value lands in the POST body.
- ✅ **`parallel_tool_calls` (asyncio.gather)** — `NodeConfig.parallel_tool_calls: bool` (default `False`, zero behavior change). When `True` and `plan_mode` is off and **no control-flow tool** is present, the LLM's batch of tool calls executes concurrently via `asyncio.gather` (results yielded in input order — deterministic). Control-flow tools (`__sub_graph__`, `register_tool`, `unregister_tool`, `exit_plan_mode`) and `plan_mode`-active runs fall back to the existing sequential loop (sequential side-effects + gating required). Default `False` → no regression.

### Parallel Graph Node + Workflow Persistence (C5)
- ✅ **`parallel` node real fan-out/gather** — a `parallel` node's N outgoing edges = N branches; each branch runs concurrently via `asyncio.gather` as an independent sub-runtime over its sub-graph (branch target → merge node). `_find_merge_node` computes the common successor via intersection of per-branch reachable sets (deterministic BFS discovery order — fixes set-iteration nondeterminism). `_build_branch_subgraph` isolates branch-reachable nodes excluding the merge/parallel node. Branch events carry `[parallel:label]` tags; merged outputs (edge order) land in the parent context as an assistant message. **Fallbacks (zero behavior change):** single outgoing edge → direct first-edge traversal; `plan_mode` active → sequential first-edge (read-only explore, no write side-effects); no outgoing edges → terminal. Default graphs with no parallel node → no regression.
- ✅ **Workflow SQLite persistence** — `WorkflowEngine` accepts an `AgentStore`; all write ops (create/get/list/delete + run execute/pause/resume/cancel) dual-write memory + disk; read ops fall back memory → disk. Two new tables (`workflows`, `workflow_runs`) + 8 CRUD methods on `AgentStore`. `_restore_run` resets the non-serializable `asyncio.Event`/cancel flag after a restart. `cancel_run` now sets `status=CANCELLED` + `finished_at` (was flag-only — lost on restart). No-`store` path stays pure in-memory for backward compatibility.

### SDK Programmatic Agent Surface (C12)
- ✅ **`Agent.query()` unified entry** — Claude-SDK-style: `query(client, input, stream=True)` returns an async generator (yield events for `async for`); `stream=False` returns a coroutine (full result dict). Idempotent `_ensure_created` builds the agent + applies config on first call; subsequent calls reuse `agent_id`. `run()`/`stream()` record the per-call `graph_id` returned by `agent.execute`/`execute_stream`.
- ✅ **Programmatic config fields** — `Agent` dataclass gains `hooks`, `memory`, `context_window`, `tools`, `max_iterations`, `temperature`. `_apply_config` wires them via `agent.configure` + `hooks.register` + `memory.store` RPCs. `configure(**kwargs)` sets known fields, routes unknown keys to `metadata` (round-trips via `to_dict`/`from_dict`).
- ✅ **Tool daemon registration** — `Tool.to_daemon_dict()` serializes a Python handler via `inspect.getsource` (fallback `terminal` if no handler); `register_to_daemon(client)` routes to `tool.register_python` (Python handler) or `tool.dynamic_register` (schema-only).
- ✅ **`tool.register_python` RPC** — daemon execs the dedented source in an isolated namespace, detects `async def` via regex, builds a concrete `BaseTool` subclass via `types.new_class` with `exec_body` (`execute` in the namespace satisfies `@abstractmethod` at class-creation; `name`/`description`/`parameters` set post-create to avoid the `__init_subclass__` kwarg error). `_SAFE_TOOL_NAME_RE` guards the name. Registered in both dispatch dicts. Example: `examples/sdk_programmatic.py`.

### Telemetry Instrumentation + OTLP Export (C13)
- ✅ **Runtime span instrumentation** — `TelemetryEngine` had complete span/counter/latency structures but the runtime never called `start_span`/`end_span` (all dashboard metrics stayed zero — dead code). Now wired on 3 hot paths in `AgentRuntime`: `graph.execute` (wraps `_run_with_trajectory`, reuses trajectory `trace_id` for correlation), `llm.call` (attributes `prompt_tokens`/`completion_tokens` from `usage`; error status on LLM failure), `tool.call` (around `tool.execute`, ok/error status). All span calls are `try/except` log-only — telemetry never blocks the main path.
- ✅ **OTLP/HTTP JSON export** — `export(fmt="otlp", push=True)` POSTs `resourceSpans` JSON to `config.endpoint` via stdlib `urllib` (**no `opentelemetry-sdk` dependency**), 5s timeout, failures log-only. Compatible with Jaeger/Tempo/OTel-collector HTTP-JSON ingesters. `telemetry.export` RPC passes the `push` param through (returns `{format, push, data}`).
- Tests: `tests/test_c13_telemetry.py` (13) — instrumentation fires on all 3 paths (counters non-zero after a graph run), token recording, latency, error status, no-engine no-crash, OTLP payload + push mock + failure log-only, `telemetry.export`/`telemetry.metrics` RPC.

### GitTool Extended Actions (C15)
- ✅ **14 git actions** — `GitTool` (`tools/git_tools.py`) extended from 6 (`status, log, diff, commit, branch, pull`) to **14**: added `push`, `fetch`, `checkout` (`-b` via `create_new`), `merge`, `rebase`, `reset` (`soft`/`mixed`/`hard` modes), `stash` (save / `pop`), `show`. All reuse the async `_git_cmd` subprocess helper (30s timeout, STDERR merged). Write actions (`push`/`merge`/`rebase`/`reset`) already covered by `SafetyGateway` L3 policies (`git_push`/`git_*`). Unified params: `branch`, `remote` (default `origin`), `target` (commit/ref), `mode`, `create_new`, `pop`. (Audit's "no file delete" gap was already closed in an earlier release via `FileDeleteTool`.)
- Tests: `tests/test_c15_git_tools.py` (15) — each new action exercised in a real temp git repo (local bare remote for `push`/`fetch`; branch + commit + merge/rebase; `reset` keeps changes staged on `soft`); invalid-mode + requires-branch guards; enum registered in default registry; existing actions still pass.

### Audit 0825 Hardening (v0.3.45) — P0-P3 全量修复
- ✅ **P0 fatal (D-1..D-7)** — WebSocket default-off + token auth; LLM path `evaluate_action` safety gate; sub-runtime inherits `safety_gateway`/`plan_mode`; self-repair path gated; `register_tool`/`unregister_tool` removed from readonly set; HTTP graph-execute auth + CORS tightened (default localhost origin, no `allow_origins=*`+credentials); PRE_TOOL_USE/POST_TOOL_USE hooks fail-closed (timeout/non-JSON block, other events fail-open).
- ✅ **A-3 tool sink hardening** — secure-by-default + env opt-out across 5 sinks: terminal catastrophic-denylist (`rm -rf /`, `mkfs`...; `FUSION_TERMINAL_UNRESTRICTED=1`), db read-only-by-default + ATTACH always blocked (`FUSION_DB_ALLOW_WRITE=1`), file write-blocks system/sensitive paths (`.ssh`/`.aws`/`/etc`...; `FUSION_FILE_ALLOW_SYSTEM=1`, `FUSION_FILE_ROOTS` allowlist), `code_execute` rerouted from bare `exec()` to `CodeSandbox` (macOS sandbox-exec + AST checks), plugin auto-load opt-in (`FUSION_PLUGINS_ENABLE=1`).
- ✅ **A-2 memory source labeling** — LLM-sourced content never classified `user` type (reserved for human input); reclassified user→project, tagged `source=llm`, injection-suspect detection.
- ✅ **L-1/L-2** — `AgentGraph.validate()` rejects unknown `node.type`; LLM path honors `stop_on_tool_error`.
- ✅ **P-1/P-2/P-3/P-4** — MLX stop lifecycle correct sequence; MLX start locked; `chat_stream` closes stream on timeout; `TrajectoryWriter` eviction cap (256) for abandoned SSE sessions.
- ✅ **L-3/L-4/M-1/M-2/M-3** — silent JSON-swallow logging; dead `Task.session_id` field deleted; MLX list/health logging; checkpointer log escalation after 3 consecutive fails.
- Tests: `tests/test_audit_0825_fixes.py` (21) + `tests/test_hooks.py` D-7 fail-closed/fail-open.

### Integration
- ✅ **fusion-mlx** — Apple Silicon optimized model serving
- ✅ **OpenAI-compatible API** — Works with any OpenAI-compatible backend
- ✅ **HTTP REST API** — FastAPI on `127.0.0.1:11455` (launched by `start.sh` via the daemon). Routes under both `/v1/*` and `/api/v1/*` (alias) so external clients using either convention resolve. Agent index auto-rebuilt from on-disk manifests on startup.
- ✅ **fusion-projects** — project_service binds agents via `GET /api/v1/agents` (set `FUSION_AGENT_STUDIO_URL=http://127.0.0.1:11455`)
- ✅ **macOS native** — SwiftUI app with WKWebView canvas integration
- ✅ **i18n** — English and Chinese UI strings

---

## Project Structure

```
fusion-agent-studio/
├── agent_runtime/          # Core engine
│   ├── graph.py            # AgentGraph data model
│   ├── context.py          # Execution context
│   ├── runtime.py          # State machine engine
│   ├── executor.py         # Node executor
│   ├── orchestrator.py     # Multi-agent orchestration
│   ├── persistence.py      # SQLite persistence
│   ├── exporter.py         # Python script export
│   ├── templates.py        # Preset templates (8)
│   ├── api_server.py       # FastAPI + WebSocket server
│   ├── daemon_server.py    # UDS JSON-RPC 2.0 daemon (40 core RPCs)
│   ├── dispatchers/        # 11 Sub-Dispatchers
│   │   ├── base.py         # SubDispatcher ABC
│   │   ├── agent.py        # Agent lifecycle handlers
│   │   ├── chat.py         # Chat engine handlers
│   │   ├── deploy.py       # Deploy/export handlers
│   │   ├── infra.py        # Infrastructure handlers
│   │   ├── knowledge.py    # Knowledge/RAG handlers
│   │   ├── marketplace.py  # Marketplace handlers
│   │   ├── memory.py       # Memory management handlers
│   │   ├── planner.py      # Planner handlers
│   │   ├── safety.py       # Safety/verify handlers
│   │   ├── team.py         # Team/orchestration handlers
│   │   └── workflow.py     # Workflow engine handlers
│   ├── fusion_code_bridge.py # fusion-code subprocess bridge
│   ├── agent_templates.py  # 8 agent config templates
│   ├── graph_editor.py     # DAG editor backend
│   ├── metrics_engine.py   # Inference metrics
│   ├── agent_marketplace.py# Agent marketplace
│   ├── data_ingestion.py   # Document readers + ETL + chunking
│   ├── code_sandbox.py     # AST check + diff + sandbox-exec
│   ├── aware_engine.py     # 3-Tier aware cascade
│   ├── fmp_router.py       # FMP v2 (@Mention + turns + dedup)
│   ├── knowledge_engine.py # SQLite-vec + FTS5 hybrid search + RRF
│   ├── llm_gateway.py      # Unified model proxy + fallback chain
│   ├── swarm_router.py     # Agent handoff + hop limit + delegation
│   ├── undo_manager.py     # Canvas undo/redo
│   ├── variable_manager.py # Variable management
│   ├── json_schema.py      # Structured output
│   ├── debugger.py         # Step debugger
│   ├── prompt_templates.py # Reusable prompt templates
│   ├── sub_graph.py        # Sub-graph support
│   ├── triggers.py         # Webhook + Cron
│   ├── i18n.py             # Internationalization
│   ├── deployer.py         # One-click deploy
│   ├── connectors.py       # External connector manager (OAuth2/API Key/Webhook)
│   ├── apikey_manager.py   # API key lifecycle (create/rotate/revoke)
│   ├── style_manager.py    # Output style templates (5 builtin + custom)
│   ├── workflow_engine.py  # 6-pattern workflow execution engine
│   ├── session_manager.py  # Fork/background session manager
│   ├── telemetry.py        # OTLP-compatible telemetry (spans/traces/metrics)
│   ├── verifier.py         # Adversarial N-skeptic verification
│   └── safety.py           # Safety gateway + risk classifier
├── sdk/                    # Agent SDK (programmatic access)
│   ├── __init__.py         # Agent, Tool, AgentClient public API
│   ├── agent.py            # Agent dataclass + run/stream/fork
│   ├── tool.py             # Tool dataclass + OpenAI schema
│   └── client.py           # JSON-RPC 2.0 AgentClient
├── plugins/                # Built-in workflow plugins
│   ├── code_review/        # 5-agent code review (manifest.json)
│   ├── feature_dev/        # 3-agent feature development
│   ├── security_scan/      # 3-agent security scanning
│   ├── pr_review/          # 6-agent PR review
│   └── agent_builder/      # Agent scaffold wizard
├── tools/                  # Tool system
│   ├── base.py             # BaseTool abstract class
│   ├── registry.py         # Tool registry
│   ├── file_tools.py       # File operations
│   ├── terminal_tools.py   # Shell execution
│   ├── git_tools.py        # Git operations
│   ├── text_tools.py       # Text processing
│   ├── http_tools.py       # HTTP requests
│   ├── code_tools.py       # Code execution
│   ├── data_tools.py       # JSON/CSV/Base64
│   ├── utility_tools.py    # Date/UUID/Hash/Path/Zip
│   ├── db_tools.py         # SQLite + annotation + perf monitor
│   └── plugin_manager.py   # Dynamic plugin loader
├── server/                 # fusion-mlx communication
│   ├── fusion_mlx_client.py# HTTP client
│   └── process_manager.py  # Process lifecycle
├── tests/                  # 1591 tests
│   ├── test_runtime.py     # Runtime engine tests
│   ├── test_graph.py       # Graph model tests
│   ├── test_tools.py       # Tool tests
│   ├── test_business_scenarios.py # End-to-end business scenario tests
│   ├── test_agent_handlers.py    # Agent/marketplace handler tests
│   ├── test_workflow_engine.py   # Workflow engine tests
│   ├── test_session_manager.py   # Session manager tests
│   ├── test_telemetry.py         # Telemetry engine tests
│   └── ...                 # 18+ test files total
└── examples/               # Example graphs
    ├── code_assistant.json
    ├── file_organizer.json
    └── terminal_automation.json
```

---

## Comparison with Alternatives

| Dimension | Dify.AI | Coze | n8n | LangFlow | **Agent Studio (us)** |
|-----------|---------|------|-----|----------|----------------------|
| **Inference engine** | Ollama/external API | Cloud API | External API | External API/Ollama | **fusion-mlx (MLX native)** |
| **Apple Silicon perf** | ❌ | ❌ | ❌ | ❌ | **✅ 2-bit quant, continuous batching** |
| **Local offline** | ⚠️ Partial | ❌ | ✅ | ✅ | **✅ 100%** |
| **Privacy** | ⚠️ Self-host | ❌ Cloud | ✅ Self-host | ✅ Self-host | **✅ Data never leaves device** |
| **Desktop native** | ❌ Web | ❌ Web | ❌ Web | ❌ Web | **✅ macOS SwiftUI** |
| **System tools** | ❌ | ❌ | ⚠️ Some | ❌ | **✅ Terminal/File/Git/Xcode** |
| **Multi-model** | ❌ (Ollama single) | ✅ Cloud | ❌ | ❌ | **✅ EnginePool + MemoryEnforcer** |
| **Quantization** | ❌ (limited GGUF) | ❌ | ❌ | ❌ | **✅ 40+ formats, 2-bit extreme** |
| **Cost** | Cloud API fees | Cloud API fees | Free | Free | **Zero API cost, unlimited calls** |

---

## Development

```bash
# Install dev dependencies
pip install -e ".[test]"

# Run all tests
pytest tests/

# Run with coverage
pytest tests/ --cov=agent_runtime --cov=tools --cov=server

# Create a new tool
python -c "from tools.plugin_manager import PluginManager; from tools.registry import ToolRegistry; pm = PluginManager(ToolRegistry()); pm.create_plugin_template('my_tool')"
```

### Test Stats
- **2144 tests**, 0 failures
- **94%+ statement coverage**
- **Python 3.11+** compatible
- **16 business scenario integration tests** covering: agent lifecycle (create→configure→execute→delete), skill management, soul management, marketplace (publish→search→install), memory (store→recall→delete), safety (check→evaluate→policy), planner, deploy export/import, templates, graph CRUD, agent filtering, env health, RAG, ping

---

## License

Apache License 2.0

## Acknowledgments

- [fusion-mlx](https://github.com/dahai80/fusion-mlx) — Apple Silicon model serving
- [MLX](https://github.com/ml-explore/mlx) — Apple's machine learning framework
- [Dify.AI](https://github.com/langgenius/dify) — Reference for visual agent orchestration
