# v0.19.2

Patch release: role templates in teams.register — completes the #810 task-dependency/stage-gate trilogy.

## What's in this release

- **Role templates (#810 request #3)** (#821): `teams.register` accepts `role_templates: [{name, prompt_contract, output_artifact}]`; members reference a template via `role_template` and the contract + output artifact path are injected into the member context brief (`[role contract] ...` / `[output artifact] ...`) so pipelines like req/prd/rnd/valid/launch register in one call. Unknown references and duplicate names get typed errors before any kernel call. With #811 (dep_gate) and #819 (task.reject), all three #810 requests are delivered.
- Also first release carrying #818 (per-member privacy passthrough in `teams.register`) and #819 (task.reject reject path) — both merged after the v0.19.1 tag but before this release branch.

## Behavior changes this release

| domain | claim | evidence |
|---|---|---|
| teams | CL-007: role templates in `teams.register` (context injection, typed pre-kernel validation) | `TestRoleTemplates` in `tests/test_issue_707_teams_dispatcher.py`, `insight/claims-registry.md` |
| tasks | CL-006 (from v0.19.1, still new since the 0.19.1 tag): `task.reject` / `TaskStore.reject_task` reject path | `tests/test_issue_810_reject_reopen.py`, `TestTaskRejectRpc` |
