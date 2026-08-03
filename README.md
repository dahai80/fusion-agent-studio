<div align="center">

# Fusion-MLX Agent Studio

**Local Agent Development Platform for Apple Silicon**

Run, build, and orchestrate AI agents entirely on your Mac — no cloud, no API fees, no data leaving your device.

[![Version](https://img.shields.io/badge/v0.4.0-blue.svg)]()
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-1701-success.svg)](tests/)

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
│  │  │ State Machine │  │   │  │ 31 tools  │  │               │
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
| `tools/` | Built-in tool system: 31 tools + plugin system | 11 files |
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
- ✅ **Agent lifecycle** — draft → published → archived status flow with version tracking, API endpoint generation, clone, debug execute_stream
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
- ✅ **Code sandbox** — AST safety analysis, diff preview, macOS sandbox-exec isolation for code execution
- ✅ **3-Tier aware engine** — Debounce → AST diff → LLM gate cascade for file-change significance detection
- ✅ **FMP router v2** — @Mention routing, round-robin turns, per-agent circuit breaker, message dedup
- ✅ **Knowledge engine** — SQLite-vec + FTS5 hybrid search, RRF fusion, scoped namespaces, auto-embedding
- ✅ **LLM gateway** — Unified model proxy with priority routing, capability matching, fallback chain, per-model circuit breaker
- ✅ **Swarm router** — Agent handoff with hop_count limit (max 3), task delegation, auto-escalation
- ✅ **Plaza broadcast** — Multi-agent shared log stream with @Mention triggers, 3-round circuit breaker, human break-in, supervisor designate
- ✅ **HITL L1/L2/L3 governance** — Autonomous (L1), diff preview (L2), gateway approval (L3) safety levels with category-based policies
- ✅ **RAG pipeline** — KnowledgeEngine retrieval → context assembly → LLM generation, integrated as DAG node type
- ✅ **Fusion-RAG integration** — `FusionRAGClient` (HTTP proxy to fusion-rag at `:11436/kb/*`) with semantic search, hybrid BM25+Vector (RRF), contextual retrieval, reranking, RAG Q&A, directory scan/watch, project KB mapping; daemon `kb.search/ask/scan/health` RPC; REST `POST /v1/knowledge-bases/{kb_id}/search|ask|scan`, `GET /v1/knowledge-bases/rag-status`; graceful fallback when fusion-rag unavailable
- ✅ **Memory auto-compression** — Tiered memory (short_term/long_term/archive) with LLM-based summarization and age/importance promotion
- ✅ **Planner node** — OpenDevin-style "plan-confirm-execute" workflow with risk assessment (low/medium/high)
- ✅ **Data readers** — Web, GitHub, Notion, PDF, Directory readers for LlamaIndex-style document ingestion
- ✅ **AgentPackage workspace** — Snapshot/restore workspace dirs, .git snapshots, source management, skill DAG import/export
- ✅ **Agent Loop (内生回灌)** - `loop_mode="agent"` LLM node re-invokes itself after each tool round until end_turn; per-node `max_loop_iterations` cap, stop_reason-driven termination, Compaction/Hooks 接入点
- ✅ **Context compaction** - 4-stage pipeline (microcompact → smart-truncate → hard-compact) + `reactive_strip` 413 recovery; deterministic-first, MLX optional; wired into Agent Loop each round; reactive 413 auto-retry wired into `LLMGateway` (strip + retry same model before fallback); compaction summaries persisted to `memory_engine` (auto-summary, scope=`compaction`)
- ✅ **Hooks lifecycle** - `HookEngine` with 10 events (PRE/POST_TOOL_USE, SESSION_START/END, STOP, PRE_COMPACT, SUBAGENT_*, USER_PROMPT_SUBMIT); callback + command hooks, regex matcher, block/approve decisions, `~/.fusion-agent-studio/hooks.json` config; exposed via daemon `hooks.list/register/test`
- ✅ **Workflow engine** — 6 execution patterns (pipeline, parallel_barrier, loop_until_dry, loop_until_budget, adversarial_verify, judge_panel); WorkflowConfig + WorkflowRun lifecycle; daemon `workflow.*` RPC (9 methods)
- ✅ **Session manager** — Fork sessions (async background tasks), attach/detach event streams, background_list/kill; daemon `session.*` RPC (5 methods)
- ✅ **Telemetry engine** — OTLP-compatible spans/traces/metrics; auto-counters (llm_calls, tool_calls, tokens); latency tracking (avg/p99); export to JSON/OTLP/console; daemon `telemetry.*` RPC (5 methods)
- ✅ **Agent SDK** — `Agent` + `Tool` + `AgentClient` classes for programmatic access; JSON-RPC 2.0 over UDS; scaffold_agent templates (basic/coder/reviewer/researcher); daemon `sdk.*` RPC (3 methods)
- ✅ **Built-in plugins** — 5 workflow manifests: code_review (5-agent parallel→judge), feature_dev (3-agent 7-phase pipeline), security_scan (3-agent parallel→adversarial→pipeline), pr_review (6-agent parallel→adversarial→judge), agent_builder (pipeline→pipeline→adversarial)
- ✅ **Safety classifier** — Keyword-based risk scoring → auto_approve/preview/human_approve classification; daemon `safety.classify_action` RPC
- ✅ **Adversarial verify** — N-skeptic majority vote pattern; configurable voter_count + threshold; daemon `verify.adversarial_verify` RPC
- ✅ **Team limits** — Concurrent agent + depth limits; daemon `team.set_limits/get_limits` RPC
- ✅ **AX accessibility** — Semantic annotations + screen-reader descriptions for agent outputs; daemon `session.accessibility` RPC
- ✅ **Mid-turn model switch** — Switch LLM mid-conversation; daemon `mlx.switch_model` RPC
- ✅ **Tool schema lazy load** — On-demand OpenAI-compatible schema generation; daemon `tool.get_schema` RPC

### Tools (19 built-in)
| Category | Tools |
|----------|-------|
| **File** | `file_read`, `file_write`, `file_list` |
| **Terminal** | `terminal` (shell execution) |
| **Git** | `git` (status, log, diff, commit, branch, pull) |
| **Text** | `text_process`, `text_search` |
| **HTTP** | `http_request` (GET/POST/PUT/DELETE/PATCH) |
| **Code** | `code_execute` (subprocess sandbox) |
| **Data** | `json_parse`, `csv_parse`, `base64` |
| **Utility** | `date_time`, `uuid`, `hash`, `path_ops`, `zip` |
| **Database** | `sqlite_query` |
| **Annotation** | `annotation` (documentation notes) |

### Triggers
- ✅ **Webhook** — External event triggers
- ✅ **Cron** — Scheduled execution (cron expressions)

### Plugin System
- ✅ Dynamic loading of user-defined Python tools
- ✅ **Artifact FC tools** — 5 artifact tools (get_source, create, update, create_snapshot, list_all) with context injection and proactive pruning
- ✅ Plugin directory at `~/.fusion-agent-studio/plugins/`
- ✅ Template generator for new plugins

### Integration
- ✅ **fusion-mlx** — Apple Silicon optimized model serving
- ✅ **OpenAI-compatible API** — Works with any OpenAI-compatible backend
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
- **1591 tests**, 0 failures
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
