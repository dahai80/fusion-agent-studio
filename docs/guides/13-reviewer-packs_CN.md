# 指南 13 — 评审包（#549）

对任意产物（设计文档 / 代码 / 规格）运行基于角色的多评审人检查 — 四个一方评审包并行执行，返回符合 schema 的结构化发现并带逐包署名。无需手写提示词。

## 四个一方评审包

| 包 | 关注点 |
|------|-------|
| `architecture-contract` | 接口一致性、数据格式漂移、跨模块边界违规、破坏性变更风险 |
| `security` | STRIDE 攻击面排查、authn/authz 缺口、注入/SSRF/路径穿越、密钥泄露 |
| `domain` | 领域正确性检查，支持按客户领域插拔知识笔记 |
| `red-team` | 强制异议 — 论证产物为何会失败；必须给出最强客观发现 |

## 发现契约

每条发现必须符合：

```json
{
    "severity": "P0|P1|P2|P3",
    "location": "章节 / 文件 / 行",
    "rationale": "失败原因",
    "recommendation": "修改建议",
    "pack": "报告包"
}
```

纯文本结论在解析期被拒绝 — 返回散文而非 findings 的包会以逐包错误呈现，不会静默变成空评审。

## RPC 面

```jsonc
// 列出评审包
{"method": "reviewer.packs", "params": {}}

// 发现 schema
{"method": "reviewer.schema", "params": {}}

// 生成并安装 4 个一方 bundle
{"method": "reviewer.seed", "params": {"base_dir": "/tmp/packs"}}

// 并行运行评审包
{"method": "reviewer.run", "params": {
    "packs": ["security", "red-team"],
    "artifact": "<产物文本>",
    "request": "<原始请求>",
    "blind": true,
    "params_by_pack": {"domain": {"domain_notes": "..."}}
}}
```

## 盲审模式

默认 `blind: true`：评审包输入 = 产物 + 原始请求。作者说明/理由被排除在每个评审包提示之外。关闭后可传入 `author_notes`（以不可信区块渲染）。

## 领域知识钩子

`domain` 包接受 `params_by_pack.domain.domain_notes` — 客户领域的纯文本知识笔记。断言对照笔记校验；超出笔记范围的发现会在 rationale 中显式声明。

## UI

Studio web → 评审包 (Review)：勾选评审包、粘贴产物与请求、运行。发现以严重度徽章 + 逐包摘要渲染。`#/review`

## 自研评审包

评审包是标准 agent bundle（见指南 08）：

```
reviewer-pack-<key>/
    manifest.json      # {"name": "reviewer-pack-<key>", "kind": "reviewer_pack", "agent": {...}, "skills": [...]}
    agent.md           # 评审角色定义 + STRICT JSON 输出契约
    skills/<key>-checklist/SKILL.md
```

用 `bundle.install`（路径）或 `reviewer.seed`（一方集合）安装。agent 定义要求：

1. 输出必须是严格 JSON `{"findings": [...]}` — 每项含 severity/location/rationale/recommendation。
2. 声明盲审规则：仅依据产物 + 请求判断。
3. `red-team` 类包必须要求 ≥1 条发现（强制异议）。
