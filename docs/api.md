# Fusion-MLX Agent Studio API Reference

> Module-level documentation for `agent_runtime`, `tools`, and `server` packages.

---

## `agent_runtime` — Core Engine

### `agent_runtime.graph` — Agent Graph Data Model

```python
from agent_runtime.graph import AgentGraph, NodeConfig, Edge, NodeType
```

**`NodeType`** — Literal type for node types: `"start" | "llm" | "tool" | "condition" | "loop" | "end" | "error_handler"`

**`NodeConfig`** — Configuration for a single node.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | `NodeType` | — | Node type |
| `label` | `str` | `""` | Display label |
| `model` | `str` | `""` | Model name (LLM nodes) |
| `system_prompt` | `str` | `""` | System prompt (LLM nodes) |
| `temperature` | `float` | `0.7` | Sampling temperature |
| `max_tokens` | `int` | `4096` | Max tokens to generate |
| `tool_name` | `str` | `""` | Tool name (tool nodes) |
| `tool_params` | `dict` | `{}` | Tool parameters |
| `condition_expr` | `str` | `""` | Condition expression |
| `max_iterations` | `int` | `10` | Max loop iterations |
| `max_retries` | `int` | `3` | Max retries (error_handler) |
| `retry_delay` | `float` | `1.0` | Retry delay in seconds |
| `x`, `y` | `float` | `0.0` | Canvas position |

**`AgentGraph`** — Complete workflow graph.

| Method | Description |
|--------|-------------|
| `add_node(id, config)` | Add a node |
| `add_edge(source, target, label="")` | Add a directed edge |
| `get_node(id)` | Get node by ID |
| `get_outgoing_edges(id)` | Get edges from a node |
| `get_next_node(id, condition="")` | Get next node, respecting condition labels |
| `find_llm_model()` | Find the first LLM node's model name |
| `validate()` | Validate graph structure, returns error list |
| `to_dict()` / `to_json()` | Serialize to dict/JSON |
| `from_dict()` / `from_json()` | Deserialize from dict/JSON |
| `create_default(name)` | Create a simple Start→LLM→End graph |

---

### `agent_runtime.context` — Execution Context

```python
from agent_runtime.context import AgentContext, AgentEvent, AgentEventType
```

**`AgentEventType`** — Enum: `THINK | TOOL_CALL | TOOL_RESULT | RESULT | ERROR | START | END`

**`AgentEvent`** — A single event emitted during execution.

| Field | Type | Description |
|-------|------|-------------|
| `type` | `AgentEventType` | Event type |
| `content` | `str` | Event content |
| `name` | `str` | Tool name (for tool events) |
| `args` | `dict` | Tool arguments |
| `node_id` | `str` | Source node ID |
| `timestamp` | `float` | Unix timestamp |

**`AgentContext`** — Conversation context for an execution session.

| Method | Description |
|--------|-------------|
| `add_message(role, content, ...)` | Add a message to history |
| `add_event(event)` | Add an event |
| `is_complete()` | Check if execution is finished |
| `is_max_iterations_reached()` | Check max iterations limit |
| `elapsed_seconds()` | Get elapsed time |
| `token_usage()` | Get aggregated token usage |
| `to_dict()` / `from_dict()` | Serialize/deserialize |

---

### `agent_runtime.runtime` — Agent Runtime Engine

```python
from agent_runtime.runtime import AgentRuntime
```

**`AgentRuntime(mlx_client, tool_registry, max_iterations=25)`**

| Method | Description |
|--------|-------------|
| `execute_graph(graph, input, context=None)` | Execute a graph, yielding `AgentEvent`s |

The runtime drives the LLM → tool → observe → decide loop:
1. Validates the graph
2. Walks nodes from start node
3. For LLM nodes: calls fusion-mlx via HTTP, handles tool calls
4. For tool nodes: executes the tool directly
5. For condition nodes: evaluates the expression
6. For loop nodes: tracks iteration count
7. For error_handler nodes: retries on failure
8. Emits events for each step

---

### `agent_runtime.orchestrator` — Multi-Agent Orchestration

```python
from agent_runtime.orchestrator import MultiAgentOrchestrator, AgentConfig, OrchestrationResult
```

**`MultiAgentOrchestrator(mlx_client, tool_registry)`**

| Method | Description |
|--------|-------------|
| `sequential(agents, input)` | Run agents in sequence, each output feeds the next |
| `parallel(agents, input)` | Run agents in parallel (max 5 concurrent) |
| `master_worker(master, workers, task)` | Master decomposes, workers execute, master summarizes |

---

### `agent_runtime.persistence` — SQLite Persistence

```python
from agent_runtime.persistence import AgentStore, Checkpoint
```

**`AgentStore(db_path)`** — SQLite-backed persistence.

| Method | Description |
|--------|-------------|
| `save_graph(graph)` | Save/update an agent graph |
| `load_graph(id)` | Load a graph by ID |
| `list_graphs()` | List all graphs |
| `delete_graph(id)` | Delete a graph |
| `create_session(id, graph_id, name)` | Create an execution session |
| `update_session_status(id, status)` | Update session status |
| `save_checkpoint(session_id, context, node_id)` | Save execution checkpoint |
| `load_latest_checkpoint(session_id)` | Load most recent checkpoint |

---

### `agent_runtime.exporter` — Graph Export

```python
from agent_runtime.exporter import GraphExporter
```

**`GraphExporter`**

| Method | Description |
|--------|-------------|
| `to_python(graph, include_runtime=True)` | Export as standalone Python script |
| `to_json(graph)` | Export as JSON |
| `to_yaml(graph)` | Export as YAML-like format |

---

### `agent_runtime.templates` — Preset Templates

```python
from agent_runtime.templates import TemplateManager, register_default_templates
```

**`TemplateManager`** — Manages preset agent templates.

| Method | Description |
|--------|-------------|
| `register(name, graph)` | Register a template |
| `get(name)` | Get a template by name |
| `list()` | List all templates |
| `has(name)` | Check if template exists |

**Preset templates (8):** `code-assistant`, `file-organizer`, `terminal-automation`, `data-extractor`, `web-summary`, `batch-rename`, `code-review`, `git-automation`

---

### `agent_runtime.variable_manager` — Variable Management

```python
from agent_runtime.variable_manager import VariableManager
```

**`VariableManager`** — Cross-node variable passing.

| Method | Description |
|--------|-------------|
| `set(name, value, coerce="")` | Set a variable (optional type coercion) |
| `get(name, default="")` | Get a variable (supports dot notation) |
| `interpolate(template)` | Replace `{{ var }}` placeholders |
| `delete(name)` | Delete a variable |
| `clear()` | Clear all variables |
| `keys()` | List all variable names |
| `to_dict()` / `load_from(data)` | Serialize/deserialize |

---

### `agent_runtime.json_schema` — Structured Output

```python
from agent_runtime.json_schema import JsonSchemaValidator
```

**`JsonSchemaValidator(schema)`** — JSON Schema validation and coercion.

| Method | Description |
|--------|-------------|
| `validate(data)` | Validate data against schema, returns errors |
| `coerce(data)` | Coerce types to match schema |
| `extract_from_text(text)` | Extract JSON object from text |
| `to_instruction()` | Generate LLM prompt instruction |

---

### `agent_runtime.debugger` — Step Debugger

```python
from agent_runtime.debugger import StepDebugger
```

**`StepDebugger`** — Single-step execution and breakpoints.

| Method | Description |
|--------|-------------|
| `pause()` | Pause execution |
| `resume()` | Resume execution |
| `step_over()` | Execute next node and pause |
| `add_breakpoint(node_id)` | Add a breakpoint |
| `remove_breakpoint(node_id)` | Remove a breakpoint |
| `check_pause(node_id)` | Check if should pause before a node |
| `next_event()` | Block until next debug event |

---

### `agent_runtime.triggers` — Webhook & Cron

```python
from agent_runtime.triggers import WebhookManager, CronManager, Webhook, CronJob
```

**`WebhookManager`** — Webhook trigger management.

| Method | Description |
|--------|-------------|
| `register(webhook, handler)` | Register a webhook |
| `handle(webhook_id, payload)` | Handle an incoming webhook |

**`CronManager`** — Scheduled job management.

| Method | Description |
|--------|-------------|
| `register(job, handler)` | Register a cron job |
| `start()` | Start the cron scheduler loop |
| `stop()` | Stop the cron scheduler |

---

### `agent_runtime.i18n` — Internationalization

```python
from agent_runtime.i18n import I18n
```

**`I18n(language="en")`** — Multi-language support.

| Method | Description |
|--------|-------------|
| `t(key, default="")` | Translate a key |
| `set_language(lang)` | Switch language |
| `register_locale(lang, translations)` | Register a custom locale |

**Built-in languages:** `en` (English), `zh` (Chinese)

---

## `tools` — Built-in Tool System

### `tools.base` — Base Tool

```python
from tools.base import BaseTool, ToolResult
```

**`BaseTool`** — Abstract base class for all tools.

| Attribute | Description |
|-----------|-------------|
| `name` | Tool name (used for function calling) |
| `description` | Tool description |
| `parameters` | JSON Schema for parameters |

| Method | Description |
|--------|-------------|
| `execute(**kwargs)` | Execute the tool (abstract) |
| `openai_schema()` | Get OpenAI function-calling schema |

### `tools.registry` — Tool Registry

```python
from tools.registry import ToolRegistry
```

| Method | Description |
|--------|-------------|
| `register(tool)` | Register a tool |
| `get(name)` | Get a tool by name |
| `has(name)` | Check if tool exists |
| `list_tools()` | List all tools with metadata |
| `to_openai_schemas()` | Convert all tools to OpenAI schemas |

### `tools.plugin_manager` — Plugin System

```python
from tools.plugin_manager import PluginManager
```

| Method | Description |
|--------|-------------|
| `discover()` | Scan plugin directory for available plugins |
| `load_plugin(name)` | Load a single plugin |
| `load_all()` | Load all plugins |
| `create_plugin_template(name)` | Generate a boilerplate plugin file |

### Built-in Tools (19)

| Tool | Name | Description |
|------|------|-------------|
| `FileReadTool` | `file_read` | Read file contents |
| `FileWriteTool` | `file_write` | Write/append to file |
| `FileListTool` | `file_list` | List directory contents |
| `TerminalTool` | `terminal` | Execute shell commands |
| `GitTool` | `git` | Git operations (status/log/diff/commit/branch/pull) |
| `TextProcessTool` | `text_process` | Text transformation |
| `TextSearchTool` | `text_search` | Regex/plain text search |
| `HttpRequestTool` | `http_request` | HTTP requests (GET/POST/PUT/DELETE/PATCH) |
| `CodeExecuteTool` | `code_execute` | Execute Python in subprocess |
| `JsonParseTool` | `json_parse` | JSON parse/validate/pretty-print |
| `CsvParseTool` | `csv_parse` | CSV parse/filter/convert |
| `Base64Tool` | `base64` | Base64 encode/decode |
| `DateTimeTool` | `date_time` | Current time/format/timestamp |
| `UuidTool` | `uuid` | UUID generation |
| `HashTool` | `hash` | MD5/SHA1/SHA256/SHA512 |
| `PathOpsTool` | `path_ops` | Path operations (join/resolve/parent/filename) |
| `ZipTool` | `zip` | ZIP file list/extract |
| `SqliteQueryTool` | `sqlite_query` | SQLite query execution |
| `AnnotationNode` | `annotation` | Text annotation/documentation |

---

## `server` — fusion-mlx Communication

### `server.fusion_mlx_client` — HTTP Client

```python
from server.fusion_mlx_client import FusionMLXClient, LLMResponse
```

**`FusionMLXClient(base_url, api_key, timeout)`** — HTTP client for fusion-mlx.

| Method | Description |
|--------|-------------|
| `chat(model, messages, tools, ...)` | Call `/v1/chat/completions` |
| `list_models()` | List available models |
| `health()` | Check if server is healthy |
| `get_server_stats()` | Get server statistics |
| `create_agent_session(model, ...)` | Create OpenClaw agent session |
| `submit_tool_result(session_id, ...)` | Submit tool result |

### `server.process_manager` — Process Management

```python
from server.process_manager import FusionMLXProcessManager
```

**`FusionMLXProcessManager(port, model, ...)`** — Manages fusion-mlx process lifecycle.

| Method | Description |
|--------|-------------|
| `start(wait_timeout)` | Start fusion-mlx subprocess |
| `stop(timeout)` | Stop the process |
| `restart()` | Restart the process |
| `is_running()` | Check if process is running |