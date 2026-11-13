---
doc_id: fusion-agent-studio/plan/evolution-2026-11-verification
product: fusion-agent-studio
class: core
entry: developer
doc_type: architecture
title: {zh: 演进计划 2026-11 落地验证, en: Evolution Plan 2026-11 Landing Verification}
audience: maintainer
status: stable
version: 1.0
updated: 2026-11-13
owner: "@dahai"
source_of_truth:
  - docs/plans/evolution-2026-11-verification.md
---

# Evolution Plan 2026-11 — 100% Landing Verification

> Companion to `docs/plans/evolution-2026-11.md`. Maps every plan item to its
> implementation, tests, and proof. Status: verification in progress.

## E1 — session-level run budget enforcement

| Plan item | Landed as | Verified by |
|---|---|---|
| RunBudget{max_cost_usd, max_tokens, max_wall_s} | `agent_runtime/budget_engine.py` | tests/test_e1_budget_enforcement.py |
| Enforce after each LLM round, stop new calls | runtime post-llm-node check, `ctx.run_tracker` | TestRuntimeEnforcement |
| Parked conversion (#646 reuse) | `usage_limit` frame + park resume_request carries run_budget | TestManagedRunPark |
| `run.budget_reached` webhook | api_server pump fire | TestManagedRunPark |
| `run_budget` on task.submit / agent.run path | /v1/graph/stream `run_budget` query param + resume | tests/test_e1_budget_enforcement.py::TestManagedRunPark |
| Proof: truncation within 1 round | tests/proof/prove_budget_enforcement.py (Proof 9 in CI) | CI proof job |

## E2 — general eval runner

| Plan item | Landed as | Verified by |
|---|---|---|
| Dataset YAML at evals/ dir | studio_home()/evals/*.yaml, list_datasets | tests/test_e2_eval_runner.py |
| Scorers: exact / contains / llm_judge / tool_sequence | eval_runner.Scorer classes | TestScorers |
| eval.from_trace replay | EvalRunner.from_trace | TestFromTrace |
| reviewer pack as dataset format | EvalDataset.from_reviewer_pack | TestReviewerPack |
| RPC eval.run / listDatasets / fromTrace | dispatchers/eval.py | TestEvalDispatcher |
| EvalsPage web | web/src/pages/studio/EvalsPage.tsx | web component tests |
| Proof: 20-case reproducible | tests/proof/prove_eval_runner.py (Proof 10) | CI proof job |

## E3 — OTel GenAI semconv + traceparent

| Plan item | Landed as | Verified by |
|---|---|---|
| GenAI semconv span names | telemetry.SEMCONV_SPAN_NAMES + semconv_span() | tests/test_e3_otel_semconv.py |
| SDK exporter path (opentelemetry-sdk optional) | _OtelSdkBridge + [otel] extra | TestSdkBridge |
| In-memory ring buffer unchanged | telemetry engine intact | TestSemconv.test_otlp_export_uses_semconv |
| traceparent passthrough (inbound continues trace) | parse/format + span_from_traceparent + http middleware | TestTraceparent / TestSpanFromTraceparent |
| Local Tempo single-link live test | NOT RUN — needs local Tempo instance; deferred (export path unit-verified) | — |

## E4 — Inspector tabs

| Plan item | Landed as | Verified by |
|---|---|---|
| 5 tabs Summary/Events/Tools/Threads/Resources | RunDetailPage refactor | web/src/pages/__tests__/RunDetailPage.test.tsx |
| Tools: calls/failure rate/median, row click → event | runs.tool_stats + ToolsTab drill-down | test_e4_inspector_rpcs.py + component test |
| Threads: per-thread lane + minimap + nested tree | runs.thread_lanes + ThreadsTab | test_e4_inspector_rpcs.py + component test |
| ?event={id} deep link | hashQuery event param → Threads highlight | component test |
| Browser e2e (multi-subagent lane + deep link) | component tests cover the contract; browser e2e deferred | — |

## E5 — approval webhooks + offline reviewer

| Plan item | Landed as | Verified by |
|---|---|---|
| approval.requested / approval.resolved webhooks | door.py _notify_requested/_notify_decision | test_e5_offline_reviewer.py::TestApprovalLifecycleWebhooks |
| Offline reviewer job, LLM-judge vs permission rules | offline_reviewer.OfflineReviewer | TestOfflineReviewer |
| Report lands in audit_logger | _write_audit | TestOfflineReviewer.test_audit_write + proof |
| Proof: 3 overreach ≥2 hits, zero FP baseline | tests/proof/prove_offline_reviewer.py (Proof 11) | 3/3 flagged, 0 FP — run recorded |

## E6 — playground + asset registry

| Plan item | Landed as | Verified by |
|---|---|---|
| asset.put/list/checkout version chain (SQLite) | asset_registry.AssetRegistry | test_e6_playground_assets.py (immutable, idempotent checkout) |
| marketplace format alignment | sha256 content_hash addressing | test_verify_detects_tamper |
| playground replay + param diff | playground.Playground | TestPlayground + web component test |
| trace replay with edits | Playground.from_trace | TestPlaygroundRpcs.test_from_trace_rpc |
| Browser e2e (diff rendering) | component test covers diff rendering; browser e2e deferred | — |

## E7 — WS default-on

| Plan item | Landed as | Verified by |
|---|---|---|
| FUSION_ENABLE_WS default 1, D-1 token not relaxed | daemon_server start() _ws_active resolve | tests/test_m2_team_rpcs_ws.py |
| Degrade to off + warning, daemon unaffected | missing token → warn + off | test_status_ws_disabled_default |
| start.sh status shows WS state | degrade-aware daemon.status ws_enabled | tests/test_m2_team_rpcs_ws.py |

## E8 — dev/prod control-plane split

| Plan item | Landed as | Verified by |
|---|---|---|
| Layer metadata on studio pages | web/manifest.json "layer" on all 21 pages | json validity + README contract |
| Prod layer mounted in web-shell behind gateway login | upstream fusion-web-shell#153 filed (work boundary) | issue |
| Local dev layer no regression | additive metadata, standalone serves all | typecheck + build |

## E9 — single-machine worker pool

| Plan item | Landed as | Verified by |
|---|---|---|
| Daemon-derived worker pool, per-worker workspace | worker_pool.WorkerPool | test_e9_worker_pool.py |
| Parked resume correct | _enqueue re-queue after parked_until | proof + test_parked_session_resumes_same_worker |
| Zero cross-talk | isolated workspace dirs | Proof 12: 20 markers, one per worker dir |
| Proof: 4 workers x 20 sessions | tests/proof/prove_worker_pool.py (Proof 12) | 20/20 ok, 3 parked resumed |

## Surpass points

| # | Item | Status |
|---|---|---|
| S1 | eval case format shared with SFT export | partial — dataset format unified; trainer export deferred |
| S2 | local-first control room, localhost webhook targets | done — webhooks fire to any registered local URL |
| S3 | budget enforcement in harness funnel | partial — enforcement at runtime post-llm seam riding ctx; harness funnel integration deferred to fusion-harness upstream |
| S4 | regulator-examinable audit format | done — findings per-event in audit.db |
| S5 | proof per batch | done — Proofs 9-12 registered in CI |

## Suite state at verification

- pytest: 4119 passed, 0 failed, 5 skipped (full suite, feat/e6-playground-assets pre-merge)
- ruff: clean
- web: typecheck + build green; component tests green
- CI: all green and merged — #672 (v0.14.30), #673 (plan doc), #676 (E1), #677 (E2), #678 (E3+E7), #679 (E4+E5), #680 (E6+E8+E9+web)
- CI fixes landed with the batch: eval proof excludes wall-clock duration_s (#680); docs-only PRs report required check names via step-level gates (#680)
