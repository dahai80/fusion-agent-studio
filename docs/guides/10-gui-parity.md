# 10 GUI 对标三件：权限卡、思考通道、运行产物

N9（#348）为 GUI 补齐三块能力：四类权限审批卡、思考通道开关、运行产物版本化三元组。三块均在 daemon / HTTP 层落地，web 端已接入；SwiftUI 端按上游 issue 推进。

## 1. 四类权限卡（Permission Cards）

待审动作按 `category` 确定性映射为四类卡（纯规则，不调用模型）：

| 卡类 | category |
|------|----------|
| `folder` 文件夹 | `file_read` `file_write` `doc_retrieval` `knowledge_search` |
| `code` 代码 | `code_analysis` `code_edit` `shell_exec` `git_push` `database_write` |
| `network` 网络 | `network_access` |
| `remote` 远程 | `tool_call` `llm_call` |

未知 category 落 `code`。RPC `safety.get_pending_actions` 的每条动作新增两个字段：

```json
{
  "action_id": "…",
  "card_type": "folder",
  "card": {
    "action_id": "…",
    "card_type": "folder",
    "category": "file_write",
    "level": "L2",
    "status": "pending",
    "content": "write /tmp/x",
    "requires_diff": true,
    "diff": "--- original\n+++ proposed\n…",
    "original": "…",
    "proposed": "…"
  }
}
```

Web Safety 页「待审动作」按四类卡过滤、用卡片网格展示，diff 可展开后批准/拒绝（`safety.approve_action` / `safety.reject_action`）。

## 2. 思考通道（Thinking Channel）

模型返回的 reasoning 增量（`reasoning_content` / `reasoning` / `thinking` 任一键）不再被丢弃：

1. `FusionMLXClient.chat_stream` 解析为 `StreamChunk.delta_reasoning`
2. `LLMGateway.chat_stream` 以 `delta_reasoning` 透传
3. runtime 流式循环发出独立事件 `thinking_token`（`metadata.channel = "thinking"`），与正文 `token` 分离

开关：

- 运行级：SSE 查询参数 `thinking=0|1`
  `GET /v1/graphs/{graph_id}/execute/stream?input=…&thinking=0`
  `GET /v1/agents/{agent_id}/execute/stream?input=…&thinking=0`
  默认 `1`。
- 节点级：LLM 节点 `tool_params.thinking = false` 强制关闭该节点思考事件。
- Web AIAgent 对话页右上角「显示思考 / Show thinking」开关控制参数并渲染思考面板。

## 3. 运行产物（Run Artifact 三元组）

图定义 + 环境快照 + 对话留档，按图名版本化保存在本地：

```
~/.fusion-agent-studio/run_artifacts/run-<hex16>.json
```

`graph.execute` 与 `task` 异步执行完成后自动记录，返回值带 `run_artifact_id`。记录结构：

- `graph`：`AgentGraph.to_dict()` 快照
- `environment`：python/platform/gateway_base + 相关环境变量（key 环境变量只记 `set`，不落值）
- `conversation`：`{count, messages}`（role/content/tool_calls/tool_call_id）

只读 RPC（web 桥白名单内）：

```python
await rpc("run_artifact.list", {"limit": 20, "name": ""})   # 摘要，不含图/对话正文
await rpc("run_artifact.get", {"artifact_id": "run-…"})     # 完整三元组
await rpc("run_artifact.versions", {"name": "my_graph"})    # 同名各版本
```

Web Governance 页「产物 / Artifacts」页签列出运行记录，点击「查看三元组」并排展示三块内容。

## 4. 列表去重与测试残留清理（#335）

`graph.list` / `agent.list` 默认按名称去重：保留最新一条，其余隐藏并返回 `duplicate_count`。需要完整清单时传 `dedupe=false`。

```python
await rpc("graph.list", {})                  # 去重视图
await rpc("graph.list", {"dedupe": False})   # 原始全量
```

批量清理测试残留（鉴权同其他 mutating RPC，先 `auth.login`）：

```python
await rpc("graph.purge_test", {"names": ["TestDeployGraph"], "prefixes": ["e2e-"], "dry_run": True})
await rpc("agent.purge_test", {"prefixes": ["c12_"], "names": ["PyBot"], "dry_run": True})
```

去掉 `dry_run` 后执行；按名称精确匹配或前缀匹配，只删命中项。

## 验证

```bash
pytest tests/test_issue_348_n9_gui_parity.py -v
ruff check agent_runtime tools server tests
cd web && bun run build && bun run lint
```
