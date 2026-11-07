---
doc_id: fusion-agent-studio/guide/reviewer-security-changelog
product: fusion-agent-studio
class: platform
entry: service
doc_type: changelog
title: {zh: 安全评审员变更日志, en: Reviewer: Security Changelog}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - docs/agents/reviewer-security/CHANGELOG.md
---

# Changelog — reviewer-security

## 1.0.0 (2026-11-04)

- First published commodity edition (agent commodity standard #570)
- Capability: prompt pack from reviewer pack `security` (#549), read-only tool
  allowlist, no KB binding, eval set `eval-sets/reviewer-security.json`
- Dual SKU: `local` (MLX local model) + `pro` (cloud via gateway)
- Docs five-pack complete (G5 gate)
- Model-binding change history:
  - 1.0.0: initial bindings — local: Qwen3.5-4B (eval report generated);
    pro: gateway-assigned (report pending cloud run)
