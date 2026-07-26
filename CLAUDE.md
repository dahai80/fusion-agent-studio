# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Fusion-MLX Agent Studio — a local-first AI agent orchestration platform for Apple Silicon. Runs entirely offline, communicating with a separate `fusion-mlx` model server over HTTP (OpenAI-compatible chat completions API at `http://localhost:11434/v1`). No cloud LLM calls; all inference goes through the local fusion-mlx server.

## Common Commands

```bash
# Setup
source .venv/bin/activate
pip install -e ".[test]"

# Run all tests
pytest tests/

# Run a single test file
pytest tests/test_p0_capabilities.py

# Run with coverage
pytest tests/ --cov=agent_runtime --cov=tools --cov=server

# Run a specific test by name
pytest tests/test_p0_capabilities.py::test_something -v
```

No Makefile or Dockerfile exists for this project. The `langflow/` directory is a separate unrelated repo — ignore it.

## Architecture

```
User Input -> AgentRuntime -> AgentGraph (walks nodes) -> FusionMLXClient (HTTP) -> fusion-mlx server
                                  |                                          ^
                                  v                                          |
                            ToolRegistry (19 tools) -> tool results ---------+
```

### Core Data Flow

1. Build an `AgentGraph` — a directed graph of `NodeConfig` nodes connected by `Edge` objects
2. `AgentRuntime.execute_graph(graph, input)` — async state machine that walks nodes, yielding `AgentEvent` stream
3. LLM calls go through `FusionMLXClient` → HTTP → fusion-mlx server (never direct MLX imports)
4. Tool calls go through `ToolRegistry` → `BaseTool` subclass → results feed back into LLM context

### Key Packages

| Package | Responsibility |
|---------|---------------|
| `agent_runtime/` | Core engine: graph model, runtime, context, events, persistence, debugger, exporter, deployer, triggers, templates, variables, i18n, multi-agent orchestration, sub-graphs |
| `tools/` | 19 built-in tools inheriting `BaseTool`, plus `ToolRegistry` and `create_default_registry()` factory |
| `server/` | `FusionMLXClient` (HTTP bridge), `FusionMLXProcessManager` (subprocess lifecycle), `LLMResponse` model |
| `tests/` | Priority-tiered test suite: `test_p0_*` (core), `test_p1_*` (features), `test_p2_*` (advanced) |

### Node Types

7 node types in `NodeType` enum: `start`, `llm`, `tool`, `condition`, `loop`, `end`, `error_handler`. Each has type-specific execution logic in `AgentRuntime`.

### Key Design Patterns

- **Async-first**: All tool execution and LLM calls are `async`. `asyncio_mode = "auto"` in pytest.
- **Event-stream**: `execute_graph()` returns `AsyncIterator[AgentEvent]` — consumers iterate over events for real-time monitoring.
- **Dataclass models**: All data structures are `@dataclass` with `to_dict()`/`from_dict()` serialization. No ORM.
- **OpenAI-compatible**: Tool schemas follow OpenAI function-calling format; LLM client mirrors `/v1/chat/completions`.
- **Plugin system**: Users drop `.py` files with `BaseTool` subclasses into `~/.fusion-agent-studio/plugins/`; `PluginManager` auto-loads them.
- **SQLite persistence**: `AgentStore` at `~/.fusion-agent-studio/store.db` for graphs, sessions, checkpoints.

## LLM Testing Requirements

When tests involve actual model inference: verify port 11434 is available, restart fusion-mlx, and use that port. Download models via mirror `https://hf-mirror.com` if needed.

## Code Conventions

- Python ≥3.11
- 4-space indentation (multiples of 4, never 5/9/11 spaces)
- No docstrings — clean code only
- All code must include logging for debugging
- Use `@dataclass` for data models with `to_dict()`/`from_dict()` pattern
- Follow existing patterns in the codebase; do not introduce competing patterns
