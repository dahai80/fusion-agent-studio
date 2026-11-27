---
doc_id: fusion-agent-studio/changelog/claims-registry
product: fusion-agent-studio
class: core
entry: developer
doc_type: changelog
title: {zh: 行为声明登记表（eval#25 C-2 发布注记证据链）, en: Behavior Claims Registry (eval#25 C-2 release-notes evidence chain)}
audience: maintainer
status: stable
version: 1.0.0
updated: 2026-11-27
owner: @dahai80
source_of_truth:
  - insight/claims-registry.md
---

# Claims registry

Evidence-chain registry for release-note behavior claims (eval#25 C-2, spec
`insight/fusion-claude-insight-1102.md` §6.2). One row per externally
observable behavior claim. Register a row here BEFORE citing its CL id in a
release's "Behavior changes this release" section. Baselines must be
recomputable (protocol + run id / command).

| CL id | release | domain | claim | evidence chain | baseline |
|---|---|---|---|---|---|
| CL-001 | v0.18.17 | kernel-pin | CI hook-event count-pin (28) matches the resolved fusion-harness kernel; pin admits 0.31.0 (TURN_INTERRUPT / TURN_REBIND) | PR #803 + PR #805; `tests/test_issue_682_harness_pin.py`, `tests/test_issue_333_hooks.py`; CI run 45767441666 | CI green on release PR #805 |
| CL-002 | v0.18.17 | packaging | every published GitHub release carries windows/ubuntu/macos bundles + sha256 assets | PR #805 (`publish.yml` release-assets job); v0.18.17 release assets (6 files) | release-check on v0.18.17 + asset listing |
| CL-003 | v0.19.0 | tasks | dep_gate stage gate: a task with dep_gate=approved stays blocked until every dependency carries review_state=approved (fail-closed) | PR #811; `agent_runtime/task_store.py` deps_met; task_store tests | CI green on release PR for v0.19.0 |
| CL-004 | v0.19.0 | api | artifact content is readable over HTTP: GET /v1/agents/{id}/artifacts/{artifact_id} returns the stored artifact body | PR #812; api_server route + daemon handler tests | CI green on release PR for v0.19.0 |
| CL-005 | v0.19.0 | subagent | privacy routing: local-only constraint enforced at model resolve — privacy-constrained subagents never select remote model endpoints | PR #813; model-resolve privacy tests | CI green on release PR for v0.19.0 |
| CL-006 | v0.19.1 | tasks | reject path: task.reject (or TaskStore.reject_task) marks the task review_state=needs_fix, re-opens the previous (or named) task (completed -> pending) with the reason appended as `[reject] <reason>`, and resets the target's review_state so an approved dep_gate re-blocks downstream tasks | PR #819; `tests/test_issue_810_reject_reopen.py`, `TestTaskRejectRpc` | CI green on release PR for v0.19.1 |
| CL-007 | v0.19.2 | teams | role templates: teams.register accepts role_templates [{name, prompt_contract, output_artifact}]; members reference one via role_template and the contract + artifact path are injected into the member context brief; unknown refs and duplicate names get typed errors pre-kernel | PR #821; `TestRoleTemplates` in `tests/test_issue_707_teams_dispatcher.py` | CI green on release PR for v0.19.2 |
