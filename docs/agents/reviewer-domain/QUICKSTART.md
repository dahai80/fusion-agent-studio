---
doc_id: fusion-agent-studio/guide/reviewer-domain-quickstart
product: fusion-agent-studio
class: platform
entry: service
doc_type: guide
title: {zh: 领域评审员快速开始, en: Reviewer: Domain Quickstart}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - docs/agents/reviewer-domain/QUICKSTART.md
---

# Quickstart — Domain Reviewer Agent (10 minutes)

## 1. Prerequisites (2 min)

- fusion-agent-studio daemon running: `./start.sh start`
- fusion-mlx serving a local model on :11434 (local SKU), or gateway access
  to a cloud model (pro SKU)

## 2. Build the commodity from the pack (1 min)

```json
{"method": "commodity.from_reviewer_pack", "params": {"pack_key": "domain"}}
```

Attach a model binding (this is the ONLY SKU-specific step):

```json
{"method": "commodity.validate", "params": {"commodity": <definition + model_bindings>}}
```

## 3. Publish (2 min)

```json
{"method": "commodity.publish", "params": {"commodity": <validated definition>}}
```

Publishing fails with a per-item error list if any docs five-pack item is
missing — fix the listed items and retry.

## 4. Run a review (3 min)

```json
{"method": "reviewer.run", "params": {"artifact": "<text>", "packs": ["domain"]}}
```

Findings come back schema-validated: severity / location / rationale /
recommendation, per-pack attribution.

## 5. Verify (2 min)

- Blind review: author notes are excluded from the pack input by default
- Swap `model_bindings.local` for `pro` — capability fingerprint unchanged
- Conversational example: ask the studio agent "review this artifact for
  domain issues" and it routes through the same reviewer contract
