# NL Graph Builder — Shared GraphEditor Mutation Contract (#568)

Conversational flow building: the user describes what they want, the model
edits the flow graph. This is the shared contract for the web canvas panel
(fusion-agent-studio `WorkflowCanvasPage`) and the macOS counterpart
(fusion-studio#433) — both MUST stay behaviorally identical on this seam.

## RPC: `graph.nl_plan`

Request params:

| param | type | notes |
|---|---|---|
| `prompt` | string | user request, Chinese or English |
| `graph_json` | object | current graph (client codec shape); grounds edits |
| `catalog` | `[{type, label}]` | executable node types from the client node registry (#551); empty = server node-type set |
| `templates` | `[{name, description}]` | template gallery (#558) for recommendation/instantiation |
| `model` | string | optional; daemon LLM gateway default when empty |

Response: `{reply, ops}` on success; `{error, details, allowed_types}` when
any op fails validation (nothing is applied on the server — the client
applies ops, so a rejected plan changes nothing).

## Op schema (the GraphEditor mutation contract)

| op | fields | effect |
|---|---|---|
| `add_node` | `id, type, label?, config?` | new node; `type` MUST be in `catalog` |
| `remove_node` | `id` | remove node + its edges |
| `set_config` | `id, config` | merge config into existing node |
| `connect` | `source, target, label?` | directed edge |
| `disconnect` | `source, target` | drop that edge |
| `clear_graph` | — | wipe all nodes/edges |
| `instantiate_template` | `name` | clone a template from the gallery into a fresh graph |

## Validation rules (server, `FlowDispatcher._handle_graph_nl_plan`)

1. Node types: only `catalog` types (registry grounding — no hallucinated nodes).
2. `connect`/`disconnect`/`remove_node`/`set_config` targets must exist in the
   graph state (after earlier ops in the same plan — chaining is allowed:
   `add_node` then `connect` to it in one plan).
3. `add_node` with an existing id is rejected with a "use set_config" hint.
4. `instantiate_template` names must come from the provided template list.
5. The system prompt instructs start/end completeness and same-language
   `reply`; ambiguity returns `reply` with empty `ops` (clarifying question).

## Client application rules (both surfaces)

- Every applied batch goes through the undo stack (one entry per plan).
- Destructive ops (`remove_node`, `clear_graph`) require explicit user
  confirmation before applying; the confirmation names the affected nodes.
- Rejected plans surface `error + details` verbatim in the chat panel.
- Template instantiation routes through the #558 gallery path
  (`flow.template_instantiate` → `graph.create` → load).

## Tests

- Server: `tests/test_issue_568_nl_builder.py` (15).
- Web: `web/src/pages/studio/__tests__/WorkflowCanvasPage.test.tsx` NL builder describe (3).
