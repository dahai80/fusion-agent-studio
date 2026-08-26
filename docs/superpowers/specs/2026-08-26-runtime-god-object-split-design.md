# Design: runtime.py god-object split (audit 0826 P2-4 tech-debt)

## Context
`agent_runtime/runtime.py` = 3604 LOC, `AgentRuntime` class = 48 methods, single
god-object. Audit 0826 P2-4 (P3 tech-debt): "5 bundle 全加法修复从未提取模块,
单类仍是 god-object, 架构未改善持续恶化". Re-assessment §9 listed it as residual
risk. `_execute_llm_node` is a 940-line flat monolith (no inner sub-methods) — core
execution path, must NOT be rewritten.

## Goal
Reduce runtime.py LOC + improve locatability WITHOUT changing runtime behavior,
the public `AgentRuntime(...)` interface, or the 8 construction sites. Zero test
delta expected (2260 passed baseline).

## Approved approach: Mixin 分文件
Per user-approved preview: method bodies move verbatim into mixin modules;
`AgentRuntime` becomes a thin multi-inheritance shell keeping `__init__` + dispatch
entry points. Public surface unchanged.

## Extraction plan — 4 mixin modules

Module `_runtime_helpers.py` — shared module-level symbols (avoids circular import):
- `_env_flag`, `_max_sub_graph_depth`, `_parallel_branch_concurrency` (functions)
- `_MAX_TOOL_CALL_CHAIN`, `_MAX_RETRY_CONTEXT_MESSAGES` (constants)
- `logger` (module logger)
Both `runtime.py` and the mixin modules import from here. No runtime class logic.

Module `_runtime_safety.py` → class `_SafetyApprovalMixin` (5 methods):
- `approve_action` (2319), `reject_action` (2335)
- `approve_plan_in_graph` (2351), `reject_plan_in_graph` (2361)
- `_await_safety_approval` (2371, ~112 LOC)
Dependencies: `self.safety_gateway`, `self._safety_futures`, `self._plan_futures`,
`self._exec_future_keys`. Module deps: `logger`, `time`, `asyncio`.

Module `_runtime_checkpoint.py` → class `_CheckpointMixin` (3 methods):
- `_extract_pending_tool_calls` (947, static-ish)
- `_save_checkpoint` (967), `resume_from_checkpoint` (1007, ~71 LOC)
Dependencies: `self.checkpointer`, `self.logger`. Module deps: `logger`, `json`.

Module `_runtime_dynamic_tools.py` → class `_DynamicToolsMixin` (4 methods):
- `_validate_tool_args` (2097), `_dynamic_tool_schemas` (2140)
- `_dynamic_register_tool` (2193), `_dynamic_unregister_tool` (2277)
Dependencies: `self.tools`, `self._SAFE_TOOL_NAME_RE`, `self.safety_gateway`.
Module deps: `logger`, `re`, `json`.

Module `_runtime_nodes.py` → class `_NodeExecutorsMixin` (largest, ~10 methods):
- `_execute_llm_node` (1078, **940 LOC — move verbatim, DO NOT refactor body**)
- `_execute_tool_node` (2483), `_apply_tool_output_mapping` (2651)
- `_execute_condition_node` (2681), `_execute_loop_node` (2697)
- `_execute_error_handler_node` (2724), `_execute_parallel_node` (2819)
- `_find_merge_node` (3016), `_build_branch_subgraph` (3054)
- `_execute_sub_graph` (3097), `_extract_template_name` (3210)
- `_execute_rag_node` (3217), `_execute_planner_node` (3330)
- `_execute_verify_node` (3464), `_exec_parallel_tool` (2018)
- `_fire_tool_hooks` (2083)
Module deps: all imports (json/re/time/asyncio/os + plan_tools sentinel + helpers).
This is ~2600 LOC moved verbatim.

## Stays in runtime.py (~1000 LOC)
- Module docstring, imports (incl. new mixin imports)
- `_runtime_helpers` re-exports (for any external `from .runtime import _env_flag`)
- `ConditionEngine` class (already self-contained, lines 77-263) — UNLESS it belongs
  with helpers; keep here as it's the runtime's condition evaluator
- `AgentRuntime(__init__`, `set_tool_configs`, `build_tool_configs`,
  `_merge_tool_config_defaults`, `execute_graph`, `execute_graph_stream`,
  `_run_with_trajectory`, `_execute_graph_inner`, `_detect_unclosed_artifacts`,
  `_extract_breakpoint`, `_register_exec_future`, `_reap_exec_futures`,
  `_seed_ctx_variables`, `_auto_store_memory`, `set_knowledge_engine`)
- Class declaration: `class AgentRuntime(_NodeExecutorsMixin, _SafetyApprovalMixin,
  _CheckpointMixin, _DynamicToolsMixin):`

## Risk controls
1. **Verbatim move**: cut/paste method bodies byte-for-byte. No reformatting (Rule 3),
   no logic edits, no comment changes. Only the indentation stays the same (they're
   already 4-space class methods).
2. **MRO safety**: mixins have NO `__init__`, no overlapping method names with
   `AgentRuntime` or each other. MRO linearization is trivial (all leaf mixins).
   Verify with `AgentRuntime.__mro__` in a test.
3. **Circular import**: mixin modules import helpers from `_runtime_helpers` (leaf,
   no back-import). `runtime.py` imports mixins at top. No cycle.
4. **Checkpoint after each module**: extract one mixin → `pytest tests/ -x` green →
   next. One regression = stop, bisect. (Rule 10)
5. **Public surface unchanged**: `agent_runtime/__init__.py:102` still exports
   `AgentRuntime, ConditionEngine`. 8 construction sites untouched. `isinstance`
   checks still pass.

## Testing
- Baseline: `pytest tests/` → 2260 passed / 2 skipped (re-confirm before start).
- After each mixin extraction: `pytest tests/ -x` green (checkpoint).
- After all 4: full `pytest tests/` → 2260 passed / 2 skipped (zero delta).
- `ruff check .` green (product files NOT formatted — surgical only).
- Add 1 regression test: assert `AgentRuntime.__mro__` contains all 4 mixins +
  `_execute_llm_node` still bound + `isinstance(rt, AgentRuntime)`.

## Out of scope
- Any logic change inside `_execute_llm_node` or any executor (verbatim move only).
- Splitting `_execute_llm_node` internally (no sub-methods exist; would be a rewrite).
- Moving `ConditionEngine` (self-contained, low value, keep to minimize churn).
- daemon_server.py god-object (separate tech-debt, not this task).

## Verification (success criteria)
- runtime.py: 3604 → ~1000 LOC.
- 5 new files: `_runtime_helpers.py` + 4 mixin modules.
- `pytest tests/` 2260 passed / 2 skipped (zero delta). ruff green.
- Public `AgentRuntime(...)` interface + 8 construction sites: 0 changes.
