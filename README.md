<div align="center">

# Fusion-MLX Agent Studio

**Local Agent Development Platform for Apple Silicon**

Run, build, and orchestrate AI agents entirely on your Mac — no cloud, no API fees, no data leaving your device.

[![Version](https://img.shields.io/badge/v0.1.0-blue.svg)]()
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-1246-success.svg)](tests/)

[Quick Start](#quick-start) · [Architecture](#architecture) · [Documentation](docs/) · [Examples](examples/)

</div>

---

## Why Fusion-MLX Agent Studio?

| Feature | Agent Studio | Dify.AI | n8n | LangFlow |
|---------|-------------|---------|-----|----------|
| **Local-first** | ✅ 100% offline | ⚠️ Partial | ✅ | ✅ |
| **Apple Silicon optimized** | ✅ MLX native | ❌ Ollama | ❌ | ❌ |
| **macOS native app** | ✅ SwiftUI | ❌ Web | ❌ Web | ❌ Web |
| **System tools** (file/shell/git) | ✅ Deep integration | ❌ | ⚠️ | ❌ |
| **Multi-model concurrency** | ✅ EnginePool | ❌ | ❌ | ❌ |
| **Quantization (40+ formats)** | ✅ 2-bit to FP8 | ❌ | ❌ | ❌ |
| **Zero API cost** | ✅ | ❌ | ✅ | ✅ |
| **Data privacy** | ✅ Never leaves device | ⚠️ Self-host | ✅ | ✅ |

**One sentence:** Fusion-MLX Agent Studio = a local offline Dify + macOS-native depth + MLX inference performance.

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
│  │  │ State Machine │  │   │  │ 19 tools  │  │               │
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
│  graph.* / mlx.* / hardware.* / knowledge.* / env.* /        │
│  planner.* / rag.* / memory.* / safety.* / template.* / deploy.* / agent.* / marketplace.* │
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
| `agent_runtime/` | Core engine: graph, state machine, orchestrator, debugger, persistence, API server, daemon server (UDS JSON-RPC), templates, bridge, editor, metrics, marketplace, data ingestion, sandbox, aware, FMP, knowledge, gateway, swarm, plaza, planner, RAG pipeline | 32 files |
| `tools/` | Built-in tool system: 19 tools + plugin system | 11 files |
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
- ✅ **Daemon server** — UDS JSON-RPC 2.0 server for fusion-studio GUI integration (graph.*, mlx.*, hardware.*, knowledge.*, env.*, planner.*, rag.*, memory.*, safety.*, template.*, deploy.*, agent.*, marketplace.*)
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
- ✅ **Memory auto-compression** — Tiered memory (short_term/long_term/archive) with LLM-based summarization and age/importance promotion
- ✅ **Planner node** — OpenDevin-style "plan-confirm-execute" workflow with risk assessment (low/medium/high)
- ✅ **Data readers** — Web, GitHub, Notion, PDF, Directory readers for LlamaIndex-style document ingestion
- ✅ **AgentPackage workspace** — Snapshot/restore workspace dirs, .git snapshots, source management, skill DAG import/export

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
│   ├── daemon_server.py    # UDS JSON-RPC 2.0 daemon for fusion-studio
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
│   └── deployer.py         # One-click deploy
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
├── tests/                  # 1246 tests
│   ├── test_runtime.py     # Runtime engine tests
│   ├── test_graph.py       # Graph model tests
│   ├── test_tools.py       # Tool tests
│   ├── test_business_scenarios.py # End-to-end business scenario tests
│   ├── test_agent_handlers.py    # Agent/marketplace handler tests
│   └── ...                 # 14+ test files total
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
- **1246 tests**, 0 failures
- **94%+ statement coverage**
- **Python 3.11+** compatible
- **16 business scenario integration tests** covering: agent lifecycle (create→configure→execute→delete), skill management, soul management, marketplace (publish→search→install), memory (store→recall→delete), safety (check→evaluate→policy), planner, deploy export/import, templates, graph CRUD, agent filtering, env health, RAG, ping

---

## License

MIT

## Acknowledgments

- [fusion-mlx](https://github.com/dahai80/fusion-mlx) — Apple Silicon model serving
- [MLX](https://github.com/ml-explore/mlx) — Apple's machine learning framework
- [Dify.AI](https://github.com/langgenius/dify) — Reference for visual agent orchestration