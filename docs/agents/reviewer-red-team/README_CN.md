---
doc_id: fusion-agent-studio/guide/reviewer-red-team-readme
product: fusion-agent-studio
class: platform
entry: service
doc_type: readme
title: {zh: 红队评审员说明（中文）, en: Reviewer: Red Team README (Chinese)}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - docs/agents/reviewer-red-team/README_CN.md
---

# Red-Team Reviewer 智能体

首个发布智能体商品族（#570）成员。能力：Mandatory dissent review: argues concretely why the artifact will fail, must produce a strongest-objective finding.

## 能力概要

- Prompt 包：内置（reviewer-red-team 审查契约，默认盲审）
- 工具白名单：`artifact_get_source`、`artifact_list_all`
- 知识库绑定：无（自包含审查契约）
- 模型绑定：可替换 — 提供 `local`（MLX 本地模型）与 `pro`
  （网关云模型）两种 SKU。绑定不属于能力本体。

## SKU 版本

| SKU | 模型 | 卖点 |
|---|---|---|
| `local` | MLX 本地模型（如 Qwen3.5-4B） | 零数据出境 — 产物文本不出机器 |
| `pro` | 网关模型矩阵中的云端旗舰模型 | 对细微违规的召回更强 |

## 文档五件套（发布门禁 G5）

- [README.md](README.md) / [README_CN.md](README_CN.md)
- [QUICKSTART.md](QUICKSTART.md)
- [CAPABILITY_SPEC.md](CAPABILITY_SPEC.md)
- [EVAL_REPORT.md](EVAL_REPORT.md)
- [CHANGELOG.md](CHANGELOG.md)

五件缺一不可；commodity 发布校验会拒绝缺失任何一项的上架请求。
