<div align="center">

# Fusion-MLX Agent Studio

**Local Agent Development Platform for Apple Silicon**

Run, build, and orchestrate AI agents entirely on your Mac — no cloud, no API fees, no data leaving your device.

[![Version](https://img.shields.io/badge/v0.13.0-blue.svg)]()
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-3135-success.svg)](tests/)

**[中文文档](README_CN.md)** · [Quick Start](#quick-start) · [Architecture](#architecture) · [Documentation](docs/) · [Examples](examples/) · **[Guided Tutorials](docs/guides/README.md)**

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

# With the shared harness kernel (fusion-harness: funnels/hooks/permissions/compactor)
# NOTE: fusion-harness is not on PyPI — install from the fusion monorepo (`pip install -e fusion-harness`)
# Without it, in-tree kernel fallbacks are used automatically (#357)
pip install -e ".[harness]"

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
| `tools/` | Built-in tool system: 38 tools + plugin system (file, terminal, git, http, code, db, computer-use, artifact, plan, utility, cowork) | 18 files |
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
- ✅ **Standing reviewer (#353/#349)** — deterministic review on the event stream flags `graph_mismatch` (undeclared transitions / unknown nodes) and `untraceable_conclusion` (conclusion markers sharing no distinctive token with tool/RAG evidence). Node-level resident mode (`verify` node `resident=true`) takes precedence; otherwise the runtime-level reviewer attaches automatically (`FUSION_RUNTIME_REVIEW=0` disables). No LLM calls.
- ✅ **List dedupe & residue purge (#335)** — `graph.list` and `agent.list` dedupe identical names for display (newest kept, `duplicate_count` reported; `dedupe=false` returns the raw inventory). `graph.purge_test` and `agent.purge_test` batch-remove test residue by exact name or prefix with `dry_run`.
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
- ✅ **Soul unified loading + memory type classification (#200, C16)** — `resolve_soul_prompt(agent_id, fallback)` loads `~/.fusion-agent-studio/agents/<id>/.fusion-agent/soul.md` (soul.md takes precedence over `manifest.system_prompt`) and is now injected across **all execution paths**, not just `agent.execute`: chat (`_inject_soul` prepends soul as a system message, merging an existing one), workflow (`_run_agent` soul override + start-node backfill), and background sessions (`_run_background`). Memory gains a semantic `memory_type` column (user/feedback/project/reference, default project) on `MemoryEntry` + SQLite, with `store`/`recall`/`list_recent`/`count`/`recall_relevant` filtering by type, old-db ALTER migration, and a `classify_memory_type` heuristic auto-applied in `_auto_store_memory` (user-classified before feedback — "I prefer X" is identity, not a correction). RPC `memory.*` passes `memory_type` through.
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

### MCP — outbound (local servers, third-party client reachable)
- ✅ **Launch entrypoints** — `fusion-mcp` console script and `python -m agent_runtime.mcp_server` serve a local toolset as a standard MCP stdio server (JSON-RPC 2.0, newline-delimited, protocol `2024-11-05`). Verified with a standard `initialize`/`tools/list` client handshake.
- Registries: `--registry mlx` (default; gateway chat + status, `FUSION_MLX_MCP_BASE`/`FUSION_MLX_MCP_KEY`), `--registry rag` (fusion-rag `:11436` kb_search/kb_ask/kb_list_bases, `FUSION_RAG_MCP_BASE`), `--registry model-hub` (fusion-model-hub `:11444` modelhub_list/modelhub_search, `FUSION_MODEL_HUB_MCP_BASE`), `--registry comfyui` (ComfyUI `:11445` comfy_status/comfy_nodes, `FUSION_COMFYUI_MCP_BASE`). `--base-url` overrides per invocation.
- Example: `fusion-mcp --registry mlx` — point any standard MCP client at it via stdio (e.g. `{"stdio_cmd": ["fusion-mcp", "--registry", "mlx"]}` in `mcp.register_server`).

### Plan-as-Mode (C6) — read-only explore + human approval gate
- ✅ **`graph.plan_mode` flag** — when `True` the graph runs in a read-only explore phase. Write tools (`file_write`, `file_edit`, `file_delete`, `terminal`, …) are gated off with a `plan_mode_blocked` tool result; read tools (`file_read`, `file_list`, `file_grep`, `file_glob`, `text_search`, `text_process`, `exit_plan_mode`, `register_tool`, `unregister_tool`) execute normally.
- ✅ **`exit_plan_mode` tool** — the transition primitive. The agent calls `exit_plan_mode {plan: "…"}` once a complete plan is presented. The runtime detects the `__EXIT_PLAN_MODE__` sentinel, flips `plan_mode=False`, emits a `PLAN_MODE_EXIT` event, and unlocks write tools for the rest of the run. Tool stays a pure `BaseTool` (no runtime coupling — sentinel is detected in the tool-call loop).
- ✅ **Planner node in-graph block** — a `planner` node with `tool_params={"await_approval": True}` blocks execution via an `asyncio.Future` keyed by `plan_id`, emitting `PLAN_APPROVAL` events (`pending_approval` → `approved`/`rejected`). RPC `planner.approve_plan` / `planner.reject_plan` resolve the in-graph future in addition to the `PlannerEngine` status flag (solves the two-`PlannerEngine`-instance problem — dispatcher's vs node's).
- Events: `PLAN_MODE_EXIT`, `PLAN_APPROVAL`. Tool count: 38.

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

### Migration 1018 Hardening (v0.9.0) — #381-#391
- ✅ **#381 parallel tool-call context** — every parallel tool branch now receives the real `ExecutionContext`; plan mode and role rules can no longer be bypassed by `ctx=None`.
- ✅ **#382 single LLM entry** — self-heal retries, `chat_engine`, and `workflow_engine` route through `LLMFunnel`; `llm_gateway` direct calls remain only inside the funnel.
- ✅ **#383 hook budget fail-closed** — when the `max_fire_ms` budget is exhausted, any hook skipped at `PRE_TOOL_USE`/`POST_TOOL_USE` denies the action instead of falling through.
- ✅ **#384 permissions** — plan mode is an independent mode (`FUSION_PLAN_MODE`), graph/node rules persist in SQLite, A3 rule noise is metered, and rule checks are armed by default.
- ✅ **#385 compaction** — real 45-minute session proof (`tests/proof/prove_long_session_compaction.py`) and summaries produced through the local MLX server.
- ✅ **#386 canvas** — JSON topology editing removed from `WorkflowsPage`; the React Flow canvas gains undo/redo, connect-end node search, breakpoints, pinned nodes, and `from_node`/`stop_after` debug runs.
- ✅ **#387 SLO consumption** — `telemetry.export_slo` appends schema-versioned JSONL (`FUSION_SLO_BASELINE_PATH`) for `fusion-bench` ingestion; RPC surface `telemetry.session_slo_export`.
- ✅ **#388 MCP funnel** — `mcp.list_resources`/`mcp.list_prompts` pass through `ToolFunnel` like every other tool path.
- ✅ **#389 subagents** — explicit `MAX_SUBAGENT_DEPTH` guard, durable roster + mailbox (`agent_runtime/durable_roster.py`, SQLite WAL), wired to team RPCs.
- ✅ **#390 memory/persona/protocol** — tenant/team isolation, decay sweep + cleanup suggestions + conflict detection, memory write auditing, agents.md import/export, frontmatter declarations, persona hot reload (`PERSONA_RELOADED`), `x-fusion-*` extension fields, strict protocol plane, plugin package/install/uninstall.
- ✅ **#391 hooks/budget** — `ERROR`, `CHECKPOINT`, `TEAM_MESSAGE`, `PERSONA_RELOADED` events (26 total), day-level and graph-level budgets, and an explicit deny when `permissions=None` while enforcement is armed.

### Langflow Parity Canvas (v0.10.0) — #393-#398
Audit `audit/fusion-agent-studio-vs-langflow-audit-1020.md` closed end-to-end; single graph JSON schema remains the only source of truth (no view fork).
- ✅ **#393 schema-driven inspector** — generated config forms from `FieldSpec` (`web/src/lib/graphSchema.ts`): basic + advanced sections per node type, tool-name dropdown fed by `tool.list`, tags/kv/number/select/boolean widgets, collapsed raw-JSON escape hatch. No more free-form JSON editing for node config.
- ✅ **#394 sidebar drag-onto-canvas** — grouped palette (execution-flow vs intelligence), click-to-add and HTML5 drag with `screenToFlowPosition` drop at cursor; nodes drop straight onto the canvas like Langflow's sidebar.
- ✅ **#395 pre-save validation** — client `validateGraphClient()` blocks hard errors before the RPC; `graph.update` now validates server-side (soft by default — issues returned in the response and rendered as clickable notes that locate the problem node; `strict_validate=true` rejects hard errors, opt-in toggle on the canvas).
- ✅ **#396 import/export + templates** — export the current graph as JSON, import via file picker into a new graph, and 7 bundled `examples/*.json` templates rendered as one-click starters.
- ✅ **#397 orchestration patterns GUI** — new Orchestrations section on Workflows: create multi-phase orchestrations (6 patterns: pipeline / parallel_barrier / loop_until_dry / loop_until_budget / adversarial_verify / judge_panel) with agent multi-select, execute with input, 5s run-status polling, cancel, delete; `workflow.*` RPCs added to the web bridge allowlist.
- ✅ **#398 single-node run + edge labels + grouping** — `only_node` SSE param executes exactly one node via a start-wired subgraph + `stop_after` ("Run this node only"); edge selection with condition branch-label editing; multi-select visual grouping via a dashed `fusionGroup` container persisted through the additive `NodeConfig.parent` field (runtime ignores it).
- Tests: `tests/test_issue_393_398_langflow_parity.py` (23) — incl. a regression that invalid `from_node`/`only_node` return HTTP 400 (they previously raised a nonexistent `ErrorCode.PARAM_INVALID` attribute).

### Cowork Workflow Tool (v0.11.0) — #402
- ✅ **`cowork_workflow_run` tool** — agent graphs can invoke fusion-cowork workflows (DAG runs) as a tool step: start by `workflow_id` or natural-language `prompt` (cowork generates the DAG server-side via `/api/ai/generate-workflow`), optional `inputs` forwarded verbatim in the run body, `wait=true` polls to terminal state and returns run status + final node outputs + artifacts; `wait=false` fire-and-forget returns the `run_id` for later polling (`action=status`). Human-gated runs never block: `paused` runs and runs stalled on node-level CONFIRM approvals return `pending`/`pending_approval` with the approvals list instead of blocking. Upstream `FUSION_COWORK_URL` (default `http://127.0.0.1:11461`), auth `FUSION_COWORK_WEB_API_KEY` (X-API-Key). Registry: 38 built-in tools (39 with fusion-browser).
- Tests: `tests/test_issue_402_cowork_tool.py` (16).

### Shell Embedding Fix + GUI/UX Upgrade (v0.12.0 / v0.12.1) — #405 / #406 / #407
- ✅ **#405 embedded RPC path** — `rpc()` now prefixes `apiBase()`: inside the union-web shell iframe the host bridge fetches the path verbatim at gateway origin root, where the bare `/v1/rpc` hit the SPA catch-all and returned 200 HTML ("Unexpected token '<'"). Same rule as every other `api.*` call.
- ✅ **#406 run event waterfall** — the daemon writes a per-run structural event log (`<out>/<team>/evidence/executions/spans/<execution_id>.jsonl`, capped at 5000 lines, token frames counted not stored) and aggregates it into per-node spans (`task.events` RPC + `GET /v1/runs/{id}/events`). The run detail page renders a waterfall with proportional bars, duration/token/tool-call badges, red failed spans with the error text, and one-click "re-run to this node" via the `only_node` SSE contract (#398). Canvas gains live per-node token/tool-call badges fed by the run SSE stream (coalesced at 400 ms so per-token frames don't re-render the graph), a run totals strip, and a pre-save diff card listing added/removed/changed nodes and edge counts before `graph.update` is sent. Navigation is task-centric: Agents / Workflows / Canvas / Runs stay top-level, every secondary console collapses into "More".
- ✅ **#407 shell manifest ext fields** — `web/manifest.json` declares `pages` (18 deep-linkable hash routes with zh/en titles), `searchProviders` and `notifications`, backed by new endpoints `GET /v1/search` (graphs + runs) and `GET /v1/notifications/runs` (terminal runs as `{count, items}`), each mirrored under `/api/v1/*`.
- Tests: `tests/test_issue_405_406_407.py` (19).

### Run Span Inspector + Agents Card Grid (v0.12.2) — #410 / #411
- ✅ **#410 span inspector drawer** — the span aggregator now records per-node I/O summaries from the structural event log: tool-call arguments as JSON, tool-result/think content, streamed token concatenation, and an event count, capped at 4000 chars input and 8000 chars output. Clicking a waterfall row opens an inspector drawer with duration, tokens, tool calls, event count, and input/output/error blocks, plus the "re-run to this node" action. Nodes with no recorded payload show an explicit "no I/O summary" note instead of an empty panel. The `/v1/runs/{id}/events` payload was extended — no new endpoint.
- ✅ **#411 agents card grid** — the Agents console is now a responsive 2–4 column card grid (`sm:2 / lg:3 / xl:4`) with a dashed create-card as the first tile. Each card shows name, description, model, status, run count and last-run time aggregated client-side from the existing runs list (no new backend surface), keeping detail/publish/unpublish/clone/archive/delete actions per card.
- Tests: `tests/test_issue_405_406_407.py` (22).

### Agents Crash Fix + Error Boundary (v0.12.3) — #413
- ✅ **legacy model objects** — old agent manifests store `model` as `{provider, name, temperature, max_tokens}` while newer ones store a string; rendering the object as a React child threw error #31 and unmounted the entire module on the live 188-agent registry. `modelLabel()` (`web/src/lib/agentModel.ts`) normalizes to `model.name`, and agent cards use it.
- ✅ **router-level error boundary** — `ErrorBoundary` keyed by route wraps all pages, so a single page's render error shows an inline retry instead of blanking the shell; navigating away remounts it.
- ✅ **capability bridge diagnostics** — unreachable-bridge errors now carry the exception type (`TimeoutError` stringifies empty).
- Tests: `tests/test_issue_413_agents_page_model.py` (3).

### Studio Parity Batch (v0.12.5) — #416 #417 #418 #419
- ✅ **request storm fix (#418, P0)** — `useT()` returned a fresh function identity per render, so every effect with `t` in its deps re-ran each render and the canvas/runs pages refetched `/v1/graphs` + `/v1/rpc` in an infinite loop (~4300 req/s per endpoint, 86,637 requests in 10s, `ERR_INSUFFICIENT_RESOURCES` flood). `t` is now a `useCallback` keyed on lang. Idle 10s: canvas 0 requests, runs 2 (5s poller as designed).
- ✅ **dashboard accuracy (#419, P1)** — `dashboard.overview` counts the name-deduped agent view (same tie-break as `agent.list`, live 258 → 15); today requests + tokens aggregate TaskStore tasks + run spans (live 0/0 → 14 / 11,823); `mlx.status` probes engine health when the daemon owns no MLX process (standalone `start.sh`) and guards `pid` on the fallback path; DashboardPage reads the flat mlx payload — model status now shows the loaded model list instead of 暂无模型状态.
- ✅ **Composer parity (#416, P1)** — first-class nav entry; `@` mentions (graphs/agents/knowledge, deduped), `/` skill selector, four-class permission pre-flight card sharing the vendor `PermissionCards` component with the Safety console + cowork approval-card vocabulary, forkable sessions; `knowledge.list` added to the `/v1/rpc` bridge allowlist; streaming + thinking channel unchanged (verified with a real engine round-trip).
- ✅ **agent card parity (#417, P2)** — color dot (`color:<hex>` tag, id-hash fallback), tools count, per-agent runs/tokens/last-run usage; 5-step create wizard (name → system prompt → tools → model → color); inline run button with `agent.execute` modal; `analytics.agent_usage` aggregates TaskStore + run spans and attributes graph-scoped runs via the graph's bound `agent_id` (`list_graphs` surfaces it).
- Tests: `tests/test_issue_419_dashboard_accuracy.py` (9); legacy mlx "not running" tests hardened against a locally running engine. 3135 passed.

### Capability Tool Schema Fix (v0.12.4)
- ✅ **schema-level required params** — registry capability specs declare required arguments as a JSON-Schema array (`"required": ["query"]`); the bridge-tool sync only read per-property `required: true`, so array-form specs registered a tool with an empty `properties` map and the model could not call it. Both forms are now unioned. Covered by `tests/test_capability_tool_schema.py` (3).
- ✅ **adhoc graph contract** — agent ad-hoc graphs no longer chain static capability tool nodes (they executed unconditionally with empty params); the LLM node receives the registered bridge tools through its schema and calls them with real arguments. The shape test now asserts `start -> llm`.

### langgraph Shell Deleted — #456
- ✅ **Option A landed (delete)** — `agent_runtime/langgraph_engine.py` was an empty shell wearing the langgraph name: it never imported langgraph, skill/connector nodes no-op'd, "approve" flipped a status field without resuming execution, and all state was in-memory. Removed along with its 8 `langgraph.*` RPC handlers (workflow dispatcher), the daemon `_get_langgraph_engine` getter, and all test/README references. The visual canvas and workflow GUI are backed by the primary engine only (`graph.py` + `_runtime_nodes.py` + `workflow_engine.py`, SQLite-persisted).
- ✅ **capability door re-routed** — the approved `graph.create` mutation now builds its START→END skeleton through the real `graph.create` RPC (AgentStore-persisted, primary runtime) instead of the in-memory shell.

### Agent SDK Parity (v0.13.0) — #421-#434
- ✅ **declarative subagents (#421)** — `AgentDefinition` dataclass + markdown frontmatter (`description`, `prompt`, `tools`, `disallowedTools`, `model`, `effort`, `memory`, `mcpServers`, `skills`, `maxTurns`, `background`, `permissionMode`). Project `.claude/agents/*.md` overrides the user tier; definitions register programmatically via `team` RPC and appear in the roster.
- ✅ **tool allow/deny (#422)** — excluded tools are removed from the subagent's schema (no prompts, no call-time denial). Supports glob and MCP patterns (`mcp__server`, `mcp__server__*`).
- ✅ **subagent lifecycle (#423/#424)** — independent JSONL transcripts with stable `agentId`, resume from full history, `partial` marking at the turn limit, async-by-default spawns with status/tree listing and cancellation.
- ✅ **session surface (#425)** — `GET /v1/sessions`, `/messages`, `resume`, `fork`, `PATCH` rename/tag/summary, all backed by a pluggable `SessionStore` (#429: SQLite default + file backend for cross-host resume).
- ✅ **file checkpointing (#426)** — per-run workspace snapshots with named checkpoints and rollback; forking branches conversation, rollback branches files.
- ✅ **live injection (#427)** — `sessions.inject_input` / `POST /v1/sessions/{id}/inject` queues a user turn consumed at the next boundary.
- ✅ **per-subagent compaction + effort (#428)** — `autoCompactWindow` and `effort` ride the definition into the LLM request; global defaults are untouched.
- ✅ **markdown skill packages (#430)** — `SKILL.md` + frontmatter (`command`, `parameters`, `output-template`), auto-loaded from `~/.fusion-agent-studio/skills` and project dirs, slash-command forms in the Composer, `skills.commands` / `skills.invoke`.
- ✅ **full-bundle packages (#431)** — one manifest installs agents, graphs, skills, hooks, connectors and MCP servers; credentials are `env:`/`keychain:` references only; any facet failure rolls the whole install back; uninstall removes every facet.
- ✅ **fusion-agent CLI (#432)** — `submit / attach / resume / fork / inject / list / show / cancel` over REST+SSE, with `--sandbox local|multi-node:<node>`. Multi-node runs are delegated to the cluster router and fail loudly when it is unreachable.
- ✅ **TypeScript SDK (#433)** — `sdk/typescript` (`@fusion/agent-studio-sdk`): typed agents/graphs/runs/sessions, SSE async iterators, API-key auth, retries/timeouts, injectable transports. `web/src/api.ts` now uses it instead of a hand-rolled fetch layer.
- ✅ **subagent memory scopes (#434)** — `memory: user|project|local` injects scoped memories before the run and commits durable outcomes after it; `memory: off` disables traffic.
- Tests: `tests/test_issue_421_subagent_definitions.py`, `test_issue_423_subagent_lifecycle.py`, `test_issue_425_session_surface.py`, `test_issue_430_skills_commands.py`, `test_issue_431_bundle_packages.py`, `test_issue_432_cli.py`, plus `sdk/typescript/tests/client.test.ts`. Guide: `docs/guides/13-cli-and-sdk.md`.

### Container Workspace Backend — #495
- ✅ **optional container isolation for shell/code tools** — `terminal`, `code_execute` and `code_sandbox` accept a `workspace` argument (`auto`/`docker`/`local`, also settable per tool via manifest `config: {workspace: docker}` or env `FUSION_WORKSPACE_BACKEND`). Docker mode runs each call in a throwaway container (`--rm`, `--network none`, 512m memory, 256 pids, workdir bind-mounted at `/workspace`); image via `FUSION_WORKSPACE_IMAGE` (default `python:3.12-slim`). Python AST safety checks still run before container exec; non-container languages (node/go/swift/cpp) stay on the local sandbox-exec path.
- ✅ **graceful degradation** — when Docker is absent, `docker`/`auto` resolve to the local path with a logged warning; the sandbox-exec fallback is covered by tests. Availability is probed once per 60s, never per call.
- Tests: `tests/test_issue_495_workspace.py` (22) — backend resolution/fallback, container command construction, terminal/code tools on both backends (PATH-shimmed fake `docker`), catastrophic-pattern deny still enforced, manifest-config selection.

### ACP Client — #496
- ✅ **external agent CLIs as graph nodes** — new `acp` node type drives any Agent Client Protocol agent (e.g. Claude Code via `npx -y @zed-industries/claude-code-acp`) over stdio JSON-RPC: `agent_runtime/acp_client.py` spawns the CLI (`command`/`args`/`cwd`/`env`/`timeout` from `tool_params`; default command from `FUSION_ACP_COMMAND`), then initialize → new_session → prompt → close_session, returning stop reason, joined message, session id and token usage. Depends on `agent-client-protocol>=0.12.1` (PyPI).
- ✅ **external-agent events on the graph event stream** — ACP session updates map onto native `AgentEvent`s (`agent_message_chunk`→TOKEN, `agent_thought_chunk`→THINKING_TOKEN, `tool_call`→TOOL_CALL_START, `tool_call_update`→TOOL_CALL), so the SSE surface and run waterfall render external agents like any other node. The final message lands in the context messages with `acp_message`/`acp_stop_reason` variables and a THINK summary carrying stop reason, session id and usage.
- ✅ **graceful failures + safe defaults** — missing CLI, dead session and timeout surface as structured `AcpRunResult(success=False, error=…)` → graph ERROR events, never crashes. Permission requests default to deny (policy `permission: allow` auto-selects the first option); fs/terminal client-side callbacks and elicitations decline with JSON-RPC `-32601`/decline responses — unattended runs never block. `error_handler` nodes can retry failed `acp` nodes.
- Tests: `tests/test_issue_496_acp.py` (15) — fake ACP agent over real stdio JSON-RPC covers success/stream order/missing CLI/dead session/timeout, graph end-to-end with event mapping, and per-kind mapping units.

### Event Tree + Conversation Fork — #497
- ✅ **every run is a linked event tree** — `AgentEvent` gains `event_id`/`parent_id` (default empty; flat consumers unaffected); the runtime stamps both on every yielded event in one choke point (`_run_with_trajectory`), so `execute_graph` and `execute_graph_stream` produce a chained event history per run. Per-token frames are never tree nodes.
- ✅ **SQLite event log + branch registry** — `agent_runtime/event_tree.py` (`EventTreeStore`, reusing the AgentStore WAL connection) persists three tables: `run_event_log` (per-event JSON + a conversation `messages` snapshot, keyed run+event), `run_meta` (lineage: `root_run_id`/`parent_run_id`/`fork_event_id`, task-id indexed) and `run_branches` (family branch rows with an active pointer). Everything survives daemon restart by construction.
- ✅ **fork any finished/failed run and continue** — `run.fork` copies the path-to-root event prefix into a new run id, registers a branch, and returns the conversation snapshot at the fork point; `graph.execute_async` accepts `fork_from: {run_id, at_event_id}` to seed the new run's context with that snapshot (validated fail-fast — unknown run rejected within the ≤5s contract). REST: `POST /v1/runs/{run_id}/fork`, `GET /v1/runs/{run_id}/branches`, `POST /v1/runs/{run_id}/branches/{branch_id}/activate`; `run_id` accepts either the execution id or its task id.
- Tests: `tests/test_issue_497_event_tree.py` (16) — store units (append/skip-token/path/messages/fork prefix copy/branch activate/task-id resolution/restart durability on a fresh `AgentStore`), legacy-event round-trip, runtime id stamping, and daemon flow (execute → fork at END → fork_from continuation with seeded history → branch list/activate → cross-instance durability).

### AgentSkills SKILL.md Compatibility — #498
- ✅ **stock SKILL.md loads and triggers unmodified** — `SkillMeta` gains a `triggers` surface parsed from every frontmatter shape OpenHands/AgentSkills uses (`triggers:` dict with keywords/paths, bare list, bare string, flat `keywords:`/`paths:` keys); unknown trigger keys are consumed, never dumped into `meta.extra`. `SkillsStore` accepts SKILL.md dirs beyond user/project — `~/.fusion-agent-studio/plugins/<skill>/SKILL.md` is discovered alongside native `.py` plugins, and a frontmatter name different from the directory still resolves.
- ✅ **keyword + path-glob trigger matching** — `SkillsStore.match_skills(text, path)` matches case-insensitive keyword substrings against the user text and `fnmatch` globs against a working-file path. `skills.match` RPC (`{text, path}`) returns matched skill metadata and auto-registers each match's allowed-tools onto the runtime permission engine in one step.
- ✅ **allowed-tools enforced by the permission engine** — `agent_runtime/skill_permissions.py` maps each declared tool to a role-scoped allow rule (`roles=["skill:<name>"]`); a `role="skill:<name>"` execution auto-allows its declared tools while anything else escalates to "ask" (registration arms the engine). Skill surface tools (`skill_search`/`skill_invoke`/`skill.invoke`) stay globally allowed so the flow never locks itself out; registration is idempotent.
- ✅ **matched skills auto-exposed to the LLM** — two new runtime-registered tools (`tools/skill_tools.py`): `skill_search` (trigger match + rule registration, returns name/description/triggers/allowed-tools/command) and `skill_invoke` (renders the skill prompt via the existing progressive-disclosure store).
- Tests: `tests/test_issue_498_skill_md_compat.py` (24) — trigger-shape parsing matrix, keyword/path matching, plugin-dir discovery, role-scoped enforcement (allowed→allow, non-allowed→ask, role mismatch, surface tools, idempotency), LLM tools, runtime wiring, `skills.match` RPC.

### Confirmation Policy Tiers — #499
- ✅ **graph-level confirmation policy** — `AgentGraph.confirmation_policy` configures OpenHands-parity confirmation tiers on top of the safety approval flow: `{"tier": "always"|"never"|"risk", "risk_threshold": 0.5, "tool_overrides": {"file_write": "never"}}`. `None` (default) keeps legacy behavior — the safety gateway alone decides approval. Stashed into `ctx.metadata` per execution (concurrency-safe, mirrors plan_mode), read at the single approval chokepoint.
- ✅ **deterministic risk scoring, never LLM-judged** — `agent_runtime/confirmation_policy.py` scores each gated action on fixed bands: 0.9 destructive command patterns (`rm -rf`, `drop table`, `git push --force`, …), 0.6 network egress in params, 0.3 write-class tools, 0.0 read-only. `ConfirmRisk` confirms at/above `risk_threshold`, auto-approves below; `AlwaysConfirm` confirms everything; `NeverConfirm` auto-approves pending verdicts. Per-tool `tool_overrides` win over the graph tier.
- ✅ **escalation + visible decisions** — `AlwaysConfirm`/`ConfirmRisk` escalate actions the safety funnel auto-passed into the human approval flow (synthesized verdict); hard safety blocks are untouched — the policy only governs the confirmation step. Every decision is logged and surfaces in `SAFETY_APPROVAL` event metadata as `confirmation_policy: {tier, risk_score, decision}`. Parallel fan-out paths cannot pause, so an escalated call degrades fail-closed with an explicit "retry sequentially" error.
- Tests: `tests/test_issue_499_confirmation_policy.py` (30) — policy object matrix, risk-band scorer, graph field round-trip/subgraph propagation, chokepoint tier behavior (never auto-approves, risk below/at threshold, always escalates auto-passed, tool override wins, no-policy unchanged), and end-to-end graph execution with approval.

### Agents Console + Capability Door — #437
- ✅ **native agents console** — the f-ecommerce Agents console is now hosted by fusion-agent-studio itself (the `fas_client` HTTP proxy hop is gone): `web/console/` (agents list, Form/YAML/Test editor, assistant drawer, capability spine, seeded templates) is served at `/console` with its data surface on native REST routes (`/v1/agents-platform/*` — CRUD, compile, templates, assistant, publish, test).
- ✅ **capability door** — `agent_runtime/agent_platform/` ships a single audited dispatch door (`POST /v1/agent-platform/call`): capability lookup, status check, publisher-role resolve (registry wins; module-key callers are admin), draft denial (`version < 1`), write-role check, param re-validation, then dispatch by route kind — self-hosted REST routes dispatch in-process (no HTTP loop back to `:11442`), other modules route via the gateway shell proxy, node via ToolRegistry, RAG via FusionRAGClient, skill via SkillsStore. Mutations never execute directly — they land in the approval workbench (`/v1/agent-platform/approvals`, requester ≠ approver enforced), and an approved mutation executes through the same door (e.g. `graph.create` builds a minimal START→END skeleton on the primary engine via the real `graph.create`, persisted in AgentStore); every outcome is audited.
- ✅ **authoring YAML is the contract** — create/update compile through `yaml_compiler` (capability checks, effort → temperature mapping, no authority granting) and persist an `authoring.yaml` sidecar next to the manifest so the Form ↔ YAML round-trip is lossless; the Form is a view, not the source of truth.
- ✅ **publish registry resolves authority server-side** — publishing records the agent in `~/.fusion-agent-studio/publish_registry.json`; bridge tools (`bt_<agent>_<capability>`) dispatch in-process through the door instead of the removed HTTP console hop.
- Tests: `tests/test_issue_437_agents_console.py` (23).

### Conversational Agent Creation — #441
- ✅ **describe → draft → editor** — the studio Agents page (`#/agents`) now opens with the f-ecommerce conversational creator: a one-sentence composer ("Describe the agent you want…") + **Generate draft** wired to `POST /v1/agents-platform/compose` (NL → authoring YAML over the model gateway, honest failure returns), the "one sentence in, an editable draft YAML out" hint, and seeded sample cards (Use → compiles the template and opens it). A generated draft is saved via `/v1/agents-platform/agents` and lands straight in the Form/YAML/Test editor — `web/console/agents.html` accepts `?agent=<id>` deep links, so compile & sync, the capability picker (⚡ approval-gated / ◇ read-only), the sandbox test tab, and save → publish continue unchanged.
- ✅ **legacy quick-create kept** — the 5-step name/prompt/tools/model/color wizard remains as a secondary path ("Quick create").
- ✅ **SPA REST helper fix** — `apiPost` now sends `content-type: application/json` explicitly; FastAPI model endpoints (unlike `/v1/rpc`) answer 422 without it.

### External Approval Tasks + Decision Webhooks — #446
- ✅ **approval workbench as a service** — external consumers (e.g. EnviPermit) create review tasks on the shared workbench instead of a bespoke state machine: `POST /v1/agent-platform/approvals` with `{title, requester, assigned_to, payload, agent_id}` (`payload` is opaque consumer data, echoed back on decision). The store distinguishes `kind=mutation` records (door-created, approve dispatches the capability) from `kind=external` records (decision recorded only — no studio dispatch), with an in-DB schema migration for the new `kind`/`assigned_to`/`title` columns.
- ✅ **separation-of-duties enforced** — `requester == assigned_to` is rejected at creation (422); the requester can never decide their own task (403); when a task carries an assigned reviewer only that reviewer (or the workbench operator) may decide (403 otherwise). Decisions are idempotent-conflict-guarded (409 on double decide).
- ✅ **signed decision webhooks** — `POST /v1/agent-platform/webhooks` `{url, secret(≥16 chars), events}` registers a consumer endpoint (one per URL, re-register rotates the secret; secrets never appear in `GET` listings or `DELETE` responses). Every approve/reject fires an `approval.decided` webhook: HMAC-SHA256 of the raw body in the `x-fusion-signature` header, 3 attempts with backoff, delivery status tracked per endpoint, and a failed/unreachable webhook never breaks the decision path. REST: `GET/POST /v1/agent-platform/webhooks`, `DELETE /v1/agent-platform/webhooks/{id}`, decisions via `POST /v1/agent-platform/approvals/{id}/approve|reject` with `{decided_by, reason}`.

### Inbound Webhook Triggers — #455
- ✅ **external systems can start runs** — the `WebhookManager` is now wired to REST: `POST /v1/webhooks` `{name, graph_id, secret?, enabled?, rate_limit_per_min?}` registers an inbound trigger (secret auto-generated when omitted, returned exactly once at creation; never appears in `GET` listings). `GET /v1/webhooks` lists, `DELETE /v1/webhooks/{id}` removes, `POST /v1/webhooks/{id}/enabled` `{enabled}` toggles. Registrations persist to `~/.fusion-agent-studio/inbound_webhooks.json` (atomic write, 0600).
- ✅ **per-hook HMAC auth, mandatory** — `POST /v1/webhooks/{id}/invoke` authenticates with the HMAC-SHA256 hexdigest of the **raw request body** in the `X-Signature` header (never a re-serialization). Missing/invalid signature → 401, no run started, audit row written. Disabled/unknown hook → 404. Payloads are JSON objects, capped at 64KB (413).
- ✅ **idempotency + rate limit + audit** — optional `X-Idempotency-Key` header: a duplicate within the 10-minute window returns the original `run_id` (`deduped: true`) instead of double-firing. Per-hook token-bucket rate limit (default 30/min, 429 on burst). Every invoke is audited (hook id, source IP, signature validity, run_id, latency) and counters persist (`invoke_count`, `last_invoked_at`).
- ✅ **end-to-end curl example** (verifiable against a running daemon):
  ```bash
  # 1. register (needs studio API key when auth is configured)
  curl -s -X POST http://127.0.0.1:11442/v1/webhooks \
       -H 'content-type: application/json' -d '{"name":"demo","graph_id":"<graph-id>"}'
  # → {"webhook":{"id":"wh-…","secret":"…",…}}   # keep the secret, shown once

  # 2. trigger (no API key — the HMAC secret is the credential)
  BODY='{"text":"hi"}'
  SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "<secret>" | awk '{print $2}')
  curl -s -X POST http://127.0.0.1:11442/v1/webhooks/wh-…/invoke \
       -H "x-signature: $SIG" -H 'content-type: application/json' -d "$BODY"
  # → {"run_id":"<task-id>","status":"pending","deduped":false}
  ```

### Per-Run Agent Overrides on POST /v1/runs — #460
- ✅ **HTTP/UDS parity for per-run control** — `POST /v1/runs` now accepts an optional `spec` object (or top-level `tools` / `model` / `max_turns` / `budget_tokens`) with the same `SubAgentSpec` semantics the UDS `subagent.run` channel uses, so HTTP-only deployments (e.g. fusion-cowork `AgentExecutor` without daemon-socket access) no longer silently drop turn/token caps. Unknown spec keys and bad types are rejected with 400 (explicit, not ignored).
- ✅ **same execution machinery** — a run submitted with overrides dispatches through the new `subagent.run_task` RPC: background `SubAgentRunner` execution linked to the task record, with TaskStore writeback (`running` → `completed`/`failed`, `last_result` carries report/turns/tokens/agent_id), so `/v1/runs/{id}` polling and `/v1/runs/{id}/result` work unchanged. Cluster (`sandbox: multi-node:…`) delegation keeps its own path — overrides are local-only.
- ✅ **max_turns off-by-one fix (pre-existing)** — `max_turns` was conflated with the graph-level node-visit iteration cap, so a complete single-answer subagent run consumed the cap on `start`+`llm` before the `end` node ran and reported `Max iterations exceeded` as a failure. The graph cap now gets +2 headroom; `max_turns` caps actual LLM turns.
- ✅ **end-to-end** (verified against a live daemon + loaded Qwen2.5-32B via fusion-mlx):
  ```bash
  curl -s -X POST http://127.0.0.1:11442/v1/runs -H 'content-type: application/json' -d '{
    "goal": "Reply with exactly the word: acknowledged",
    "spec": {"model": "mlx-community/Qwen2.5-32B-Instruct-4bit", "tools": [],
              "max_turns": 2, "budget_tokens": 2000}
  }'
  # → {"run_id":"task_…","status":"running","deduped":false,"spec_applied":true}
  # poll GET /v1/runs/{run_id} → completed; result carries turns=1 tokens=57 report="acknowledged"
  ```

### `agent_overrides` Contract on POST /v1/runs — #461
- ✅ **constraints, never expansions** — `POST /v1/runs` accepts an `agent_overrides` object (`tools`, `disallowed_tools`, `model`, `max_turns`, `budget_tokens`) on top of the #460 `spec` surface. When `agent_id` targets an agent with its own tool allowlist, the caller's `tools` is **intersected** with it — a caller cannot grant tools the agent doesn't have; the intersection is what runs.
- ✅ **strict validation, no orphans** — unknown `agent_overrides` keys and bad types → 400; tool names are checked against the live tool registry (`subagent.toolset`) **before** `task.submit`, so an unknown tool name (400) never leaves an orphan pending task behind. Also fixed a latent #460 bug: a `spec` without `max_turns`/`budget_tokens` no longer 400s.
- ✅ **who constrained what, verifiable** — every overridden run writes a `run.overrides` audit row (`actor=api`, effective overrides in `details`), and `effective_overrides` is exposed in three places: the POST response, `GET /v1/runs/{id}`, and the run's `last_result` (so `GET /v1/runs/{id}/result` carries it). Explicit `spec` values win over `agent_overrides` when both are sent.
- ✅ **end-to-end** (live daemon + loaded model):
  ```bash
  curl -s -X POST http://127.0.0.1:11442/v1/runs -H 'content-type: application/json' -d '{
    "goal": "Reply with exactly: ok",
    "agent_overrides": {"tools": [], "max_turns": 2, "budget_tokens": 2000}
  }'
  # → {"run_id":"task_…","status":"running","spec_applied":true,
  #    "effective_overrides":{"tools":[],"max_turns":2,"budget_tokens":2000}}
  # GET /v1/runs/{id} → completed, effective_overrides echoed
  # GET /v1/runs/{id}/result → last_result.effective_overrides present, report="ok"
  # unknown tool → 400 "unknown tool names: nuclear_launch", no task created
  ```

### Autonomous Inbox Consumer — #457
- ✅ **agents read their own mail** — rostered agents registered with `autonomous_inbox: true` and a `graph_id` binding get a daemon-resident background consumer (`FUSION_INBOX_POLL_INTERVAL`, default 5s): each unread mailbox message becomes a durable agent run (message body as input, sender/team context injected), acked **after** the task is durably submitted (at-least-once — a crash mid-conversation redelivers on restart), and the run's final output is posted back to the sender's mailbox as a `kind=reply` message. Human-driven agents are untouched (mail waits for RPC pull). Runs are idempotency-keyed by mailbox message id, so redelivery never double-fires.
- ✅ **roster config** — `team.roster_register` gains `graph_id` and `autonomous_inbox` (schema migration for pre-existing team.db is automatic). Observability via `team.inbox_metrics` RPC: processed/replies/errors counters, inbox depth, dispatch latency, per-agent breakdown.

### Peer-Daemon Push Wake on Mailbox Send — #459
- ✅ **push instead of poll-only** — after every successful mailbox insert, `Mailbox.send`/`Mailbox.broadcast` fire a best-effort POST to the configured peer daemons (fusion-cowork `POST /api/team/notify` contract, dahai80/fusion-cowork#194), so the cowork `RunnerPool` wakes immediately instead of waiting out its poll interval. The `InboxConsumer` poll loop (#457) stays as the safety net — a down, slow, or unreachable peer degrades silently to poll delivery.
- ✅ **never blocks, never raises** — notify runs on a fire-and-forget daemon thread (2s timeout); failures log at debug and the send path is unaffected. `broadcast` sends one wake for the whole batch (not one per recipient) since the peer consumer drains the inbox in a single pass.
- ✅ **env config** — `FUSION_TEAM_PEER_NOTIFY` (`0/false/off/no` disables, default on), `FUSION_TEAM_PEER_NOTIFY_URLS` (comma-separated, e.g. `http://127.0.0.1:11461/api/team/notify`), `FUSION_TEAM_PEER_NOTIFY_KEY` (optional static key sent as `Authorization: Bearer …` for peers whose web API has auth configured; cowork accepts Bearer or `x-api-key`). Unset URLs = disabled — zero behavior change for existing installs.
- ✅ **end-to-end** (verified against a live daemon pair): AS daemon log `peer notify url=http://127.0.0.1:11461/api/team/notify status=200` after `team.mailbox_send`, cowork log `POST /api/team/notify 200 OK`.

### Integration
- ✅ **fusion-mlx** — Apple Silicon optimized model serving
- ✅ **OpenAI-compatible API** — Works with any OpenAI-compatible backend
- ✅ **HTTP REST API** — FastAPI on `127.0.0.1:11442` (launched by `start.sh` via the daemon). Routes under both `/v1/*` and `/api/v1/*` (alias) so external clients using either convention resolve. Agent index auto-rebuilt from on-disk manifests on startup.
- ✅ **fusion-multi-node cluster store (#450)** — `FUSION_SESSION_STORE=cluster` selects a `ClusterSessionStore` HTTP client of the multi-node master's shared session store (`GET/PUT/DELETE /api/v1/session-store[/{id}]`, `POST .../{id}/fork`), so a session checkpointed on node A resumes on node B; base URL from `FUSION_CLUSTER_URL` (default now the registered master port `http://127.0.0.1:11452`, was an unregistered 11457), Bearer auth from `FUSION_CLUSTER_TOKEN` (also sent on sandbox run delegation). SQLite default path unchanged; unreachable clusters fail visibly instead of falling back.
- ✅ **supervisor disposition (#453)** — the fusion-supervisor (`fusion-sv`) manifest no longer manages ports that have no always-on process: `fusion-agent-studio-ws` (11433, opt-in `FUSION_ENABLE_WS=1` feature port), `fusion-agent-studio-web` (11460, vite dev server — production serves the static dist via the daemon/gateway), and `fusion-agent-studio-deployer` (11453, `deployer.py` is an export library with no listener) are marked `supervise: false` in `architecture/port-registry.yaml`. `fusion-agent-studio` (11442) and `fusion-agent-studio-cluster` (11457, embedded in the daemon) remain supervised.
- ✅ **fusion-projects** — project_service binds agents via `GET /api/v1/agents` (set `FUSION_AGENT_STUDIO_URL=http://127.0.0.1:11442`)
- ✅ **Web UI (#327, #330/#331)** — union-web shell module under `web/` (React + vendored `@fusion/web-ui`/`@fusion/web-sdk`): workflow list + JSON create/edit, run monitor, run detail, plus the migrated AgentStudio console (#330/#331) — Agents/AgentDetail CRUD, Dashboard, Team (swarm/plaza/orchestrate), Memory, Safety, Connectors, Apikeys, Cron, Hooks, Tools (dynamic register), Skills, Marketplace, Chat, and the AIAgent per-token SSE chat with Logs/Events debug tabs. N9 GUI parity adds four-class permission cards (folder/code/network/remote) on the Safety page, a thinking-channel toggle (`thinking=0|1` on the SSE routes) with a dedicated reasoning panel, and versioned run artifacts (graph + environment + conversation) surfaced in the Governance page. Studio pages talk through the deny-by-default `POST /v1/rpc` allowlist bridge; agent streaming uses `GET /v1/agents/{id}/execute/stream`. Embedded via the fusion-gateway shell at `/shell/fusion-agent-studio/` (module-key auth injected by the gateway proxy), standalone dev server on `:11460`, and the built SPA served by the daemon at `GET /`. #399: registration of web/manifest.json injects `upstream` + `api_key` into the gateway payload so the dynamic proxy (UpstreamFor) resolves `/shell/fusion-agent-studio/` without static web_shell.modules config; upstream = `FUSION_SHELL_UPSTREAM` env > manifest > `http://127.0.0.1:11442`, api_key = `FUSION_AGENT_STUDIO_API_KEY` env > `~/.fusion-agent-studio/web_module_key` — never written to the repo manifest. See `web/README.md`.
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
├── web/                    # Web UI (#327, union-web shell module)
│   ├── src/                # React app: studio console (15 tabs) + workflows / runs pages
│   ├── vendor/@fusion/     # Vendored web-ui + web-sdk sources (CI self-contained)
│   ├── manifest.json       # Shell manifest registered with fusion-gateway
│   └── dist/               # Built SPA, served by the daemon at GET /
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

# Web UI (bun required) — dev server on :11460, proxies /v1 to daemon :11442
cd web && bun install && bun run dev
```

### Test Stats
- **3545 tests**, 0 failures
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
