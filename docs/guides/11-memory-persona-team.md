# 11 记忆治理、人设分层与团队协作

本篇覆盖 v0.9.0（#389/#390/#391）新增的三组能力：记忆衰减与冲突检测、人设分层热重载与 agents.md 互转、团队 durable roster 与 mailbox。所有示例沿用 [01 篇](./01-start-daemon.md) 的 `rpc()` 脚手架。

## 1. 记忆：租户/团队隔离

`memory.store` / `memory.recall` / `memory.list_recent` / `memory.count` 新增 `tenant_id`、`team_id` 过滤。空字符串表示不隔离（兼容旧行为）。

```python
await rpc("memory.store", {"content": "deploy uses blue-green", "tenant_id": "t1", "team_id": "ops"})
await rpc("memory.recall", {"query": "deploy", "tenant_id": "t1", "team_id": "ops"})
```

写入与删除动作会记审计日志（`memory.store` / `memory.delete` / `memory.delete_scope` / `memory.auto_forget`）。

## 2. 记忆：衰减清扫与冲突

```python
# 只出建议，不删
await rpc("memory.cleanup_suggestions", {"threshold": 0.5, "half_life_days": 14})

# 显式清扫；dry_run=true 先看候选
await rpc("memory.decay_sweep", {"threshold": 0.5, "dry_run": True})
await rpc("memory.decay_sweep", {"threshold": 0.5, "max_age_days": 90, "dry_run": False})

# 近似重复/冲突（Jaccard）
await rpc("memory.conflicts", {"threshold": 0.6, "limit": 200})
```

评分公式：`importance * 0.5 ** (age_days / half_life_days)`。`max_age_days > 0` 时超龄条目直接进入候选，原因标 `max_age`。

## 3. 人设：分层、声明与热重载

层级优先级：`global < tenant < team < agent < run`。路径位于 `~/.fusion-agent-studio/persona/`：

```
agent.md                    # global
tenants/<tenant>/agent.md   # tenant
teams/<team>/agent.md       # team
```

frontmatter 可声明规则、记忆范围与技能：

```markdown
---
rules:
  - cite evidence
  - never guess
memory_scope: project
skills: [review, test]
---
## Role
review code
```

合并结果中 `declarations.rules` 累加去重、`skills` 取并集、`memory_scope` 高层覆盖低层。daemon 启动后默认监听人设目录（mtime 轮询，约 2s），变更触发 `PERSONA_RELOADED` hook；设 `FUSION_PERSONA_WATCH=0` 关闭。

## 4. agents.md 导入导出

```python
await rpc("agent.import_agents_md", {"src_dir": "/path/to/agents"})
await rpc("agent.export_agents_md", {"agent_id": "reviewer", "dest_dir": "/tmp/exported"})
```

导入把 `*.md` 的 frontmatter（`name`/`description`/`model`/`tools`）与正文转成 `.fusion-agent` 包；导出为反向操作，可往返。

## 5. 团队：durable roster 与 mailbox

```python
await rpc("team.roster_register", {"agent_id": "a1", "name": "Alpha", "team": "ops"})
await rpc("team.roster_list", {"team": "ops"})
await rpc("team.roster_status", {"agent_id": "a1", "status": "busy"})

msg = await rpc("team.mailbox_send", {"sender": "a1", "recipient": "a2", "body": "ping", "team": "ops"})
await rpc("team.mailbox_inbox", {"recipient": "a2"})
await rpc("team.mailbox_ack", {"recipient": "a2", "message_id": msg["message"]["message_id"]})
await rpc("team.mailbox_broadcast", {"sender": "a1", "team": "ops", "body": "standup"})
await rpc("team.mailbox_stats", {})
```

存储为 SQLite WAL（`FUSION_TEAM_DB` 可覆盖路径），daemon 重启后保留。`team.mailbox_send` 同时触发 `TEAM_MESSAGE` hook。

## 6. SLO 导出（#387）

```python
await rpc("telemetry.session_slo_export", {"path": "/tmp/slo.jsonl"})
```

追加一行带 `schema: 1` 的 JSONL，供 `fusion-bench` 读取；不传 `path` 时用 `FUSION_SLO_BASELINE_PATH`。

## 验证

```bash
pytest tests/test_issue_390_memory_persona_protocol.py tests/test_issue_386_387_389_canvas_slo_team.py -v
ruff check agent_runtime tools server tests
```

---

上一篇：[10 GUI 对标三件](./10-gui-parity.md) · 下一篇：[12 画布与编排](./12-canvas-orchestration.md)
