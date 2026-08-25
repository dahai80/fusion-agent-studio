# A-1 Per-Execution State Migration — Design

Audit 0825 finding A-1: `AgentRuntime` is a process-level singleton; concurrent graph
executions stomp each other's mutable execution state written to INSTANCE attributes.

## Problem

`AgentRuntime.__init__` (runtime.py:252-325) stores 5 execution-scoped state items on
`self`:

| Attr | Type | Scope | Concurrency hazard |
|------|------|-------|--------------------|
| `_safety_futures` | `dict[action_id, Future]` | cross-RPC registry | B's `execute_graph` calls `.clear()` → drops A's pending approval futures |
| `_plan_futures` | `dict[plan_id, Future]` | cross-RPC registry | same `.clear()` hazard |
| `plan_mode` | `bool` | per-exec | B's start flips A's active read-only/explore gate |
| `_tool_call_chain_count` | `int` | per-exec | B's start zeroes A's running chain count |
| `variables` | `VariableManager` | per-exec | shared mutable; sub-runtime pollutes parent (A-2) |

`_execute_graph_inner` (478-486) resets all five at dispatch entry. Graph B starting
destroys Graph A's in-flight state. A-2 (sub-runtime shares `self.variables` by
reference via writes at 2822-2827) is the same class of bug in the sub-graph direction.

## Root cause

Execution state lives on the singleton, not on the execution. Two graphs cannot run
against one runtime without stomping.

## Solution — Approach 1 (minimal-surgical)

Fix the concurrency hazard without restructuring every method signature. Three tiers
by hazard type.

### Tier 1: Cross-RPC futures — keep flat, stop clearing

`_safety_futures` / `_plan_futures` are registries: `execute_graph` creates a future
keyed by a globally-unique `action_id` (uuid4) / `plan_id`; a SEPARATE daemon RPC
(`approve_action`, `reject_action`, `approve_plan_in_graph`, `reject_plan_in_graph`)
resolves it by that key. The RPC has only the action_id, not the exec_id.

Concurrency safety is inherent: keys are globally-unique uuids. The ONLY bug is the
`.clear()` at 478-480 dropping in-flight futures from a concurrent exec.

Fix:
- Remove `self._safety_futures.clear()` and `self._plan_futures.clear()` at 478-480.
- Futures already self-clean: `pop` on resolve (2092/2098/2108/2114/2122/2132),
  timeout (2192/3094), completion (2204/3107). No leak.
- Add a best-effort reaper guard keyed by `exec_id` so `stop()` can clean only this
  exec's stragglers without touching another exec's. Store
  `_exec_futures: dict[exec_id, set[action_id]]`; register on future create; reap
  the set on exec completion. Low-risk; protects against a future that times out
  but whose `pop` path is skipped by an exception.

### Tier 2: `plan_mode` + `_tool_call_chain_count` → `AgentContext`

Pure per-exec. Add to `AgentContext`:
- `plan_mode: bool = False`
- `tool_call_chain_count: int = 0`

Thread through via the `ctx` already present at every site. Migrate reads/writes:

| Site | Old | New |
|------|-----|-----|
| 478 | `self._tool_call_chain_count = 0` | `ctx.tool_call_chain_count = 0` |
| 482-486 | `self.plan_mode = True/False` | `ctx.plan_mode = True/False` |
| 664/707 | `self._tool_call_chain_count = 0` | `ctx.tool_call_chain_count = 0` |
| 869 | `state["tool_call_chain_count"]` reads `self._tool_call_chain_count` | read `ctx.tool_call_chain_count` |
| 918 | `self._tool_call_chain_count = state.get(...)` | `ctx.tool_call_chain_count = state.get(...)` |
| 1323-1324 | `self._tool_call_chain_count += 1` + check | `ctx.tool_call_chain_count += 1` + check |
| 1341 | `not self.plan_mode` | `not ctx.plan_mode` |
| 1396 | `self.plan_mode` (gate) | `ctx.plan_mode` |
| 1566 | `self.plan_mode = False` (exit_plan_mode) | `ctx.plan_mode = False` |
| 1741 | `self.plan_mode` | `ctx.plan_mode` |
| 2277 | `self.plan_mode` | `ctx.plan_mode` |
| 2564 | `self.plan_mode` (parallel) | `ctx.plan_mode` |
| 2630 | `sub_runtime.plan_mode = self.plan_mode` | `sub_runtime_plan_mode = ctx.plan_mode` (passed into sub) |

Sub-runtime inheritance: sub-runtime is a NEW `AgentRuntime` (2832) running its own
`execute_graph`. Its `ctx` is `sub_ctx` (2850). Pass parent's plan_mode:
`sub_ctx.plan_mode = ctx.plan_mode`. Remove the instance-level
`sub_runtime.plan_mode = self.plan_mode` write at 2630/2846 — the sub's own
`_execute_graph_inner` will read `graph.plan_mode` and set `sub_ctx.plan_mode`
correctly; explicit inheritance preserves a parent-imposed plan_mode that the
sub-graph's own flag doesn't override.

### Tier 3: `variables` → per-exec (A-2)

Largest scope: 27 interpolate/get/set sites. `variables` is a `VariableManager`
instance. Approach: each top-level `execute_graph` owns its own
`VariableManager`, threaded via ctx. But `ctx` is a plain dataclass without a
VariableManager today.

Decision: attach a per-exec `VariableManager` to ctx as `ctx.variables` (new field,
optional, defaults to a fresh manager). Runtime keeps `self.variables` ONLY as a
factory/seed default for top-level execs that don't supply one. Each exec snapshots
the seed into `ctx.variables` at dispatch entry (copy, not share). Sub-runtime gets
its own copy via the existing `sub_vars.load_from(...)` pattern (2829-2830) — already
isolated, KEEP it. Remove the parent-pollution writes at 2822-2827: those set parent
`self.variables` from input_mapping BEFORE creating sub_vars — they should set into
`ctx.variables` (parent's exec vars), not the singleton. And output writeback (2876)
should write `ctx.variables`, not `self.variables`.

Threading model: every `self.variables.X` site has `ctx` in scope (verified across all
27 sites). Replace `self.variables` → `ctx.variables`. The few sites without ctx
(e.g. checkpoint restore at 918-922 reads `state["variables"]`) get ctx passed in or
use the restore's local ctx.

Checkpoint interplay (868/918-922): checkpoint state already serializes
`variables.to_dict()` and restores via `set(k,v)`. Migrate to `ctx.variables`.

### `AgentContext` field additions

```python
plan_mode: bool = False
tool_call_chain_count: int = 0
variables: dict = field(default_factory=dict)   # snapshot, NOT a manager
```

Wait — `VariableManager` is not a dataclass-friendly snapshot. Re-decide: store the
MANAGER instance on ctx via a non-dataclass attribute set at dispatch entry, OR store
the dict snapshot and wrap. Simpler + lower-risk: keep `VariableManager` as a runtime
member but make it per-exec by giving the runtime a `_exec_vars: dict[exec_id, VM]`
registry — no, that reintroduces a singleton registry.

**Final decision (Tier 3):** `AgentContext.variables` holds a `VariableManager`
instance. Add it as a typed field with `field(default_factory=VariableManager)`.
Import guarded to avoid cycles. Every `self.variables` → `ctx.variables`. Top-level
exec seeds `ctx.variables` from `self.variables` (the runtime's default/declared vars)
via copy (`ctx.variables.load_from(self.variables.to_dict())`). This is the SAME
isolation pattern sub-runtime already uses (2829-2830) — generalize it to top-level.
After exec, declared-graph-level mutations do NOT bleed back to `self.variables`
unless an explicit output_mapping writeback targets it (sub-graph path only).

This fixes A-2: parent and sub no longer share `self.variables` by reference.

## Files touched

- `agent_runtime/context.py` — add 3 fields to `AgentContext` + to_dict/from_dict.
- `agent_runtime/runtime.py` — migrate ~60 sites; remove 2 `.clear()`; add
  `_exec_futures` reaper; seed `ctx.variables` at dispatch; sub-runtime inheritance.
- `tests/test_audit_0825_bundle3.py` — NEW: concurrency regression tests.

## Testing

Concurrency regression (the actual A-1 bug): two graphs run concurrently against one
runtime. Graph A has a pending safety/plan approval future; Graph B starts. Assert
Graph A's future survives (not cleared), Graph B's plan_mode independent of A's,
Graph B's tool_call_chain_count independent of A's, A's variables not polluted by B.

Plus: checkpoint roundtrip preserves plan_mode/tool_call_chain_count/variables;
sub-runtime variable isolation (A-2); exit_plan_mode flips only this exec's plan_mode.

## Risk

Tier 3 is the blast radius. Mitigation: mechanical `self.variables` → `ctx.variables`
replacement, every site verified to have ctx in scope. Run full suite after each tier.
