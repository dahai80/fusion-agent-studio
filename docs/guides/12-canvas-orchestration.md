# Guide 12 — Canvas & Orchestrations

Langflow-parity canvas batch (#393-#398, v0.10.0). Everything edits the single
graph JSON schema — the canvas is a view, not a second source of truth.

## Canvas workflow (Web UI → Workflows → Edit on canvas)

1. **Add nodes** — click a palette button or drag it onto the canvas
   (drops at cursor). Palette is grouped: execution flow vs intelligence.
2. **Configure nodes** — click a node; the inspector renders a generated
   form from `FieldSpec` (`web/src/lib/graphSchema.ts`). Basic fields first,
   `Advanced` discloses the rest. Tool nodes get a `tool_name` dropdown fed
   by `tool.list`. `Raw JSON` stays available as an escape hatch.
3. **Connect** — drag between typed ports. Connect-rejection mirrors runtime
   semantics (no inlet on `start`, no outlet on `end`, single-out cap on
   flow nodes, branch/fan multi-out). Dragging from a port to empty canvas
   opens a searchable node picker that creates + connects in one step.
4. **Edge labels** — click an edge to set a condition branch label
   (matches `get_next_node(condition_result)` label lookup).
5. **Group** — multi-select nodes, `Group selected nodes` wraps them in a
   dashed container. Persisted via `NodeConfig.parent`; runtime ignores it.
6. **Validate & save** — client `validateGraphClient()` blocks hard errors;
   the server responds with `issues` (soft default) rendered as clickable
   notes that select the offending node. Toggle `Strict validate` to make
   hard errors reject the save.
7. **Test runs** — `Run & watch` streams the whole graph; `Rerun downstream
   from here` uses `from_node`; `Run this node only` uses the `only_node`
   SSE param (single-node subgraph + `stop_after`). Breakpoint/pin flags on
   nodes drive `stop_after` and output capture.

## Import / export / templates

- `Export` downloads the current graph JSON.
- `Import` creates a new graph from a picked file (`graph.create`).
- `Templates` lists the repo `examples/*.json` (7 bundled starters), bundled
  at build time via `import.meta.glob`.

## Orchestrations (Workflows page, below the graph table)

Multi-phase multi-agent patterns over the workflow engine:

1. `New orchestration` — name + phase rows. Each phase: name, pattern
   (pipeline / parallel_barrier / loop_until_dry / loop_until_budget /
   adversarial_verify / judge_panel), agent multi-select (from `agent.list`).
2. `Execute` — start a run with an input string; runs poll every 5s
   (`workflow.list {workflow_id}`), show status/phase/error, `Cancel` while
   active (`workflow.cancel`).
3. Delete removes the orchestration definition (`workflow.delete`).

RPC surface used: `graph.get/update/create`, `tool.list`, `agent.list`,
`workflow.list/get/create/execute/pause/resume/cancel/delete/status`,
`budget.status`, `safety.get_pending_actions`, `safety.approve/reject`.

## Related

- Guide 10 (GUI parity) — canvas governance panel background.
- `tests/test_issue_393_398_langflow_parity.py` — server-surface contract.
