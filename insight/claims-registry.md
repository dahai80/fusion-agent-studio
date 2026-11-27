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
