<div align="center">

# Fusion-MLX Agent Studio

**Local Agent Development Platform for Apple Silicon**

Run, build, and orchestrate AI agents entirely on your Mac — no cloud, no API fees, no data leaving your device.

[![Version](https://img.shields.io/badge/v0.1.0-blue.svg)]()
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-416-success.svg)](tests/)

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
    mlx = FusionMLXClient(base_url="http://localhost:8000/v1")

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
fusion-mlx serve --model qwen3.5-9b --port 8000

# Terminal 2: Run your agent
python my_agent.py
```

---

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                    Agent Studio                                │
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
│  │  FusionMLX Client (httpx → localhost:8000)              │  │
│  │  Never imports MLX/engine/pool — pure HTTP               │  │
│  └─────────────────────────────────────────────────────────┘  │
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
| `agent_runtime/` | Core engine: graph, state machine, orchestrator, debugger, persistence | 12 files |
| `tools/` | Built-in tool system: 19 tools + plugin system | 11 files |
| `server/` | fusion-mlx HTTP client + process manager | 2 files |

---

## Features

### Agent Runtime
- ✅ **State machine engine** — LLM → tool → observe → decide loop
- ✅ **6+ node types** — Start, LLM, Tool, Condition, Loop, End, Error Handler
- ✅ **Multi-agent orchestration** — Sequential, parallel, master-worker
- ✅ **Step debugger** — Breakpoints, pause/resume, step-over
- ✅ **Variable manager** — Cross-node variable passing with interpolation
- ✅ **JSON Schema** — Structured output enforcement
- ✅ **Sub-graphs** — Reusable composed workflows
- ✅ **Checkpoint/resume** — SQLite persistence for long-running agents
- ✅ **Python export** — Export graphs as standalone scripts
- ✅ **Template system** — 8 preset templates (code review, file organizer, etc.)

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
├── tests/                  # 416 tests
│   ├── test_runtime.py     # Runtime engine tests
│   ├── test_graph.py       # Graph model tests
│   ├── test_tools.py       # Tool tests
│   └── ...                 # 12 test files total
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
- **416 tests**, 0 failures
- **95%+ statement coverage**
- **Python 3.11+** compatible

---

## License

MIT

## Acknowledgments

- [fusion-mlx](https://github.com/dahai80/fusion-mlx) — Apple Silicon model serving
- [MLX](https://github.com/ml-explore/mlx) — Apple's machine learning framework
- [Dify.AI](https://github.com/langgenius/dify) — Reference for visual agent orchestration