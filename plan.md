# Implementation Plan: AS-8, Remote RPC Adaptation, Cross-Repo Issues

## Task 1: AS-8 — Artifact-Aware System Prompt Templates

**What**: Register artifact-aware system prompt template(s) in `PromptTemplateManager`, inject into agent context when artifacts are active.

**Current state**:
- `prompt_templates.py` has `PromptTemplateManager` with 5 templates (code-review, summarize, data-extract, translate, terminal-command)
- `runtime.py:646-654` already injects `get_active_artifacts_context()` into system prompt when artifacts exist
- The injection is just a flat list of artifact names + truncated content — no usage guidelines

**Implementation**:
1. Add `artifact-long-text` template to `register_default_prompt_templates()` in `prompt_templates.py`
   - Content from PRD: artifact-ref syntax, incremental editing rules, section markers, truncation handling
2. In `runtime.py:_execute_llm_node()`, after the existing artifact context injection (L646-654), also render and append the `artifact-long-text` template if artifacts exist
3. Add `ARTIFACT_SYSTEM_PROMPT` constant in `artifact_tools.py` (the actual prompt text)

**Files modified**:
- `agent_runtime/prompt_templates.py` — add artifact-long-text template
- `agent_runtime/artifact_tools.py` — add ARTIFACT_SYSTEM_PROMPT constant
- `agent_runtime/runtime.py` — inject template-rendered prompt alongside existing artifact context

## Task 2: AS-1~7 Remote RPC Adaptation

**What**: Create `ArtifactBridge` that wraps local `ArtifactManager` with remote RPC calls to `fusion-artifacts-engine`, with auto-fallback to local.

**Implementation**:
1. Create `agent_runtime/artifact_bridge.py`:
   - `ArtifactBridge` class with `remote_url` config (default: `http://127.0.0.1:11451`)
   - For each AS-1~7 operation: try remote RPC first, fallback to local `ArtifactManager`
   - Uses `httpx` for async HTTP calls to artifacts-engine JSON-RPC
   - Ping check on init to detect remote availability

2. Create `agent_runtime/dispatchers/artifact.py`:
   - `ArtifactDispatcher(SubDispatcher)` with RPC methods
   - Delegates to `ArtifactBridge`

3. Modify `daemon_server.py:_get_artifact_manager()` → return `ArtifactBridge`
4. Register `ArtifactDispatcher` in `daemon_server._init_sub_dispatchers()`

**Files created**:
- `agent_runtime/artifact_bridge.py`
- `agent_runtime/dispatchers/artifact.py`

**Files modified**:
- `agent_runtime/daemon_server.py`
- `agent_runtime/dispatchers/__init__.py`

## Task 3: Cross-Repo Issues

File consolidated issues per upstream repo, then implement within agent-studio scope only.

## Execution Order

1. AS-8 (self-contained)
2. Remote RPC bridge (AS-1~7)
3. ArtifactDispatcher
4. Cross-repo issues
5. Tests + lint + version bump
