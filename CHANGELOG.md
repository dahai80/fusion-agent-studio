---
doc_id: fusion-agent-studio/changelog
product: fusion-agent-studio
class: platform
entry: service
doc_type: changelog
title: {zh: 变更日志, en: Changelog}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - pyproject.toml
---

# Changelog

中文：按版本记录面向使用者的变更。完整的逐 issue 变更历史见各分支 commit 与 GitHub Releases；本文件只记录需要文档跟进的行为变化。

English: user-facing changes per version. The full per-issue history lives in branch commits and GitHub Releases; this file records only behavior changes that require documentation follow-up.

## 0.14.17 (2026-11-07)

- docs: adopt fusion docs-system v1.1 contract (platform x service) — six Tier 0/1 docs, frontmatter on all in-repo markdown, docs CI workflow.
- 摘要：接入 docs-system 文档契约，新增产品页/快速上手/API/配置/架构/排查六篇与 CI 校验；无运行时行为变化。 / Summary: docs-system contract adoption; no runtime behavior change.

## Notes

- Version source: `pyproject.toml` `[project] version`. / 版本来源为 `pyproject.toml`。
- 0.14.x feature history (seats, shell registry, ACP, RAG backends, safety tiers) is tracked in GitHub issues (#164, #209, #251, #270, #271, #327, #357, #490, #499, #500, #509, #526 referenced in source comments). / 0.14.x 的功能史由 GitHub issue 追踪，源码注释中可见上述编号。
