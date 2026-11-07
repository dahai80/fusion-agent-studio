---
doc_id: fusion-agent-studio/guide/reviewer-red-team-capability-spec
product: fusion-agent-studio
class: platform
entry: service
doc_type: guide
title: {zh: 红队评审员能力规格, en: Reviewer: Red Team Capability Spec}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - docs/agents/reviewer-red-team/CAPABILITY_SPEC.md
---

# Capability Spec — Red-Team Reviewer Agent

## Inputs

| input | required | notes |
|---|---|---|
| `artifact` | yes | text to review (code, design doc, spec) |
| `original_request` | no | what the author set out to do |
| `author_notes` | no | excluded when blind review is on (default) |
| `params_by_pack` | no | per-run overrides of pack defaults |

## Outputs

Array of findings, each schema-validated (#549):

- `severity`: P0 / P1 / P2 / P3
- `location`, `rationale`, `recommendation`, `pack`

Free-text-only verdicts are rejected by the findings schema.

## Tool authorization boundary

- Allowlist: `artifact_get_source`, `artifact_list_all` (read-only)
- No terminal, network, or write tools. The agent cannot mutate artifacts.

## Model matrix

| SKU | provider | model handle | context window |
|---|---|---|---|
| `local` | mlx | any registered local model | model-dependent |
| `pro` | gateway | any model in the gateway model matrix | model-dependent |

The model handle is a replaceable binding; changing it does not alter the
capability fingerprint (prompt pack + tools + KB binding + eval ref).

## Eval set reference

`eval-sets/reviewer-red-team.json` (3 cases: violation-detection, clean-pass,
regression) — see [EVAL_REPORT.md](EVAL_REPORT.md).
