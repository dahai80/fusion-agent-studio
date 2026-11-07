---
doc_id: fusion-agent-studio/guide/13-reviewer-packs
product: fusion-agent-studio
class: platform
entry: service
doc_type: guide
title: {zh: 评审员包, en: Reviewer Packs}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - docs/guides/13-reviewer-packs.md
---

# Guide 13 — Reviewer Packs (#549)

Run role-based multi-reviewer passes on any artifact (design doc, code, spec) — four first-party packs execute in parallel and return schema-conforming findings with per-pack attribution. No prompt-writing required.

## The four first-party packs

| Pack | Focus |
|------|-------|
| `architecture-contract` | interface consistency, data-format drift, boundary violations, breaking-change risk |
| `security` | STRIDE pass, authn/authz gaps, injection/SSRF/path-traversal, secret leakage |
| `domain` | domain-correctness pass with pluggable per-domain knowledge notes |
| `red-team` | mandatory dissent — argues why the artifact will fail; strongest-objective finding required |

## Findings contract

Every finding conforms to:

```json
{
    "severity": "P0|P1|P2|P3",
    "location": "section / file / line",
    "rationale": "why this fails",
    "recommendation": "what to change",
    "pack": "which pack reported it"
}
```

Free-text-only verdicts are rejected at parse time — a pack that returns prose instead of findings surfaces as a per-pack error, never as a silently empty review.

## RPC surface

```jsonc
// list packs
{"method": "reviewer.packs", "params": {}}

// findings schema
{"method": "reviewer.schema", "params": {}}

// seed + install the 4 first-party bundles
{"method": "reviewer.seed", "params": {"base_dir": "/tmp/packs"}}

// run packs in parallel
{"method": "reviewer.run", "params": {
    "packs": ["security", "red-team"],
    "artifact": "<artifact text>",
    "request": "<original request>",
    "blind": true,
    "params_by_pack": {"domain": {"domain_notes": "..."}}
}}
```

## Blind-review mode

Default `blind: true`: pack input = artifact + original request only. Author notes/rationale are excluded from every pack prompt. Toggle off to pass `author_notes` (rendered as an untrusted section).

## Domain knowledge hooks

The `domain` pack accepts `params_by_pack.domain.domain_notes` — plaintext knowledge notes for the customer's domain. Claims are checked against the notes; findings state explicitly when a claim falls outside them.

## UI

Studio web → 评审包 (Review): select packs, paste artifact + request, run. Findings render with severity badges and per-pack summary. `#/review`

## Authoring your own packs

A reviewer pack is a standard agent bundle (see guide 08):

```
reviewer-pack-<key>/
    manifest.json      # {"name": "reviewer-pack-<key>", "kind": "reviewer_pack", "agent": {...}, "skills": [...]}
    agent.md           # reviewer role definition + STRICT JSON output contract
    skills/<key>-checklist/SKILL.md
```

Install with `bundle.install` (path) or `reviewer.seed` (first-party set). Requirements for the agent definition:

1. Output MUST be strict JSON `{"findings": [...]}` — each item carries severity/location/rationale/recommendation.
2. State the blind-review rule: judge artifact + request only.
3. `red-team`-style packs MUST require ≥1 finding (mandatory dissent).
