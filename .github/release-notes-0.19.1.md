# v0.19.1

Patch release: teams privacy passthrough + task reject path + test stability fix.

## What's in this release

- **Per-member privacy passthrough in teams dispatcher** (#818): `teams.register` accepts an optional per-member `privacy` (`local-only` or empty), validated against harness `VALID_PRIVACY_MODES` before any kernel call; invalid values get a typed error and the team is not registered. `TeamRuntime.run_member` carries it into `spec.metadata["privacy"]`, completing the #813 privacy routing chain (kernel enforcement -> harness producer -> dispatcher passthrough).
- **Task reject path (#810 request #2)** (#819): new `task.reject` RPC and `TaskStore.reject_task` — the rejected task goes `review_state=needs_fix`, the previous task (first of `depends_on`) or a named task re-opens (`completed` -> `pending`) with the reason appended to its description, and the target's `review_state` resets so an approved `dep_gate` re-blocks downstream tasks (fail-closed). Broadcasts `task.reopened` (team-scoped).
- **Test stability** (#817): `DaemonServer._mlx_attach_enabled` opt-out so gateway-evicting test fixtures keep the stub path even while a live fusion-mlx is reachable (#694 lazy attach no longer resurrects the real client mid-test).

## Behavior changes this release

- CL-006: `task.reject` / `TaskStore.reject_task` reject path — see `insight/claims-registry.md` (evidence: `tests/test_issue_810_reject_reopen.py`, `TestTaskRejectRpc`).
