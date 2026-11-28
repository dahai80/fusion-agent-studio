# v0.19.3

Patch release: docs-only — failure-modes library scaffold.

## What's in this release

- **Failure-modes library scaffold** (#824): `docs/failure-modes/README.md` — the mandatory close-out flow rule (every FAIL/residual issue close must produce a failure-modes entry with symptom signature, structural fix, and an eval linkage line pointing at the corresponding fusion-eval core task), an index table, and an entry template. Institutional source: `insight/fusion-claude-insight-1102.md` §6.3 (eval#25 cross-repo process). Closes the reverse half of the E-1 loop: failure → eval-task coverage is enforced from now on.

## Behavior changes this release

| domain | claim | evidence |
|---|---|---|
| docs | None. (documentation-only release; no runtime behavior change) | `docs/failure-modes/README.md`, CI validate green on release PR |
