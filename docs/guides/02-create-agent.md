# 02 创建你的第一个 Agent

> 场景：daemon 已启动，你想从零建一个能跑的智能体并执行它。

fusion-agent-studio 有**两条创建路径**，本篇都带你走一遍，并说明何时用哪条：

- **声明式（`agent.create`）**：只给名字、模型、系统提示、工具列表，daemon 自动合成执行图。**最简单，推荐入门。**
- **图式（`graph.create` + `graph.execute`）**：自己定义节点（start/llm/tool/…）和边，对执行流程有完全控制。适合多步骤、多工具的复杂工作流。

两条路径背后是同一套 runtime，区别只在"图怎么来"。

## 本篇你将完成

- 用 `agent.create` 建一个声明式智能体并执行
- 用 `graph.create` 建一个带工具节点的图并执行
- 理解节点类型、AgentGraph 结构、执行事件流

---

## 准备：最小 RPC 调用器

复用 [索引页](./README.md#最小-rpc-调用脚手架) 的 `rpc.py`，后续示例 `from rpc import rpc`。

确认 daemon 与模型服务在线：

```python
from rpc import rpc
import asyncio
async def main():
    print("daemon:", await rpc("ping"))
    print("mlx:", await rpc("mlx.health"))
asyncio.run(main())
```

## 路径 A：声明式 agent（推荐入门）

`agent.create` 不需要你画图——它读一份**智能体清单**（manifest），执行时自动合成 `start → llm → tool1 → tool2 → … → end` 的链。你只需声明：叫什么、用哪个模型、什么人设、能用哪些工具。

### 创建

```python
from rpc import rpc
import asyncio

async def main():
    r = await rpc("agent.create", {
        "name": "my-first-agent",
        "description": "我的第一个智能体",
        "model": "qwen2.5-3b-instruct",
        "system_prompt": "你是一个简洁的中文助手，回答控制在 100 字以内。",
        "tools": [],                 # 空表示纯对话，不带工具
        "temperature": 0.7,
        "max_tokens": 2048,
        "context_window": 32768,
        "safety_level": "L1",
    })
    print(r)

asyncio.run(main())
```

返回：
```json
{"agent_id": "a1b2c3d4e5f6", "manifest": {"name": "my-first-agent", "model": "qwen2.5-3b-instruct", ...}}
```
记下 `agent_id`，后续要用。

> `agent.create` 支持的完整字段：`name`（必填）、`model`、`system_prompt`（默认 `You are {name}.`）、`temperature`(0.7)、`max_tokens`(4096)、`tools`(空 list)、`capabilities`、`safety_level`("L1")、`tags`、`description`、`knowledge_base_ids`、`rag_strategy`("hybrid")、`context_window`(32768)、`style`、`top_p`(1.0) 等。还可带 `soul`/`memory`/`agents_md` 写入智能体包的 markdown 文件。

### 执行

```python
from rpc import rpc
import asyncio

AGENT_ID = "a1b2c3d4e5f6"   # 换成上一步拿到的

async def main():
    r = await rpc("agent.execute", {"agent_id": AGENT_ID, "input": "用一句话介绍你自己"})
    print("status:", r["status"])
    print("graph_id:", r["graph_id"])
    print("session_id:", r["session_id"])
    for ev in r["events"]:
        print(f"  [{ev['type']}] {str(ev.get('content',''))[:80]}")

asyncio.run(main())
```

`agent.execute` 只读 `agent_id` 和 `input` 两个参数。返回 `events`（事件流列表）、`status`（`completed`/`error`）、`graph_id`、`session_id`。

### 查看 / 列出 / 删除

```python
await rpc("agent.get", {"agent_id": AGENT_ID})     # 详情
await rpc("agent.list", {})                         # 全部智能体
await rpc("agent.delete", {"agent_id": AGENT_ID})   # 删除
```

智能体持久化在磁盘 `~/.fusion-agent-studio/agents/<agent_id>/.fusion-agent/`（manifest.json / soul.md / skills/ 等），重启 daemon 不丢。

## 路径 B：图式 graph（完全控制执行流程）

当你要编排"先读文件 → 再 LLM 分析 → 再写结果"这类多步流程，用 `graph.create` 自己定义节点与边。

### 11 种节点类型

| 类型 | 作用 |
|------|------|
| `start` | 入口，可设 `system_prompt` |
| `llm` | LLM 推理节点，设 `model`/`temperature`/`max_tokens` |
| `tool` | 调用内置工具，设 `tool_name`/`tool_params` |
| `condition` | 条件分支，设 `condition_expr` |
| `loop` | 循环，设 `max_iterations` |
| `parallel` | 并行扇出，多出边即多分支并发 |
| `end` | 终点 |
| `error_handler` | 错误处理，设 `max_retries`/`retry_delay` |
| `rag` | 知识库检索增强 |
| `planner` | 计划-确认-执行（可阻塞审批） |
| `verify` | 对抗式验证 |

### 创建图

`graph.create` 支持两种传参：完整 `graph_data`（一个 AgentGraph dict），或拆开的 `nodes[]`+`edges[]`。完整 `graph_data` 更直观，直接对应 `examples/` 里的图样例：

```python
from rpc import rpc
import asyncio

GRAPH = {
    "id": "first-graph-001",
    "name": "First Graph",
    "description": "读文件 → LLM 分析 → 输出",
    "nodes": {
        "start": {"type": "start", "label": "Start",
                  "system_prompt": "你是代码分析助手，阅读代码并给出改进建议。"},
        "read":   {"type": "tool", "label": "Read File",
                   "tool_name": "file_read", "tool_params": {"path": "README.md"}},
        "analyze":{"type": "llm", "label": "Analyze",
                   "model": "qwen2.5-3b-instruct", "temperature": 0.3, "max_tokens": 2048},
        "end":    {"type": "end", "label": "Done"}
    },
    "edges": [
        {"source_id": "start", "target_id": "read"},
        {"source_id": "read", "target_id": "analyze"},
        {"source_id": "analyze", "target_id": "end"}
    ],
    "start_node_id": "start",
    "version": "1.0"
}

async def main():
    r = await rpc("graph.create", {"graph_data": GRAPH})
    print(r)

asyncio.run(main())
```

返回含 `graph_id`。也可用拆开形式：`{"name": ..., "nodes": [...], "edges": [...]}`（每个 node 带 `id` 字段）。

> 可选 `stable_id: true` —— 按 name+内容 hash 复用 graph_id（upsert 语义），让 cron/缓存绑定不随重建失效。可选 `strict_validate: true` —— 硬拒未知 `tool_name` / 无法解析的 `condition_expr`；默认只软警告。

### 执行图

```python
from rpc import rpc
import asyncio

async def main():
    r = await rpc("graph.execute", {
        "graph_id": "first-graph-001",
        "input": "分析 README.md",
        "session_id": "sess-1",       # 可选，留空自动生成
        "variables": {"lang": "zh"},  # 可选，节点里用 {{ lang }} 插值
    })
    print("status:", r.get("status"))
    for ev in r.get("events", []):
        print(f"  [{ev['type']}] {str(ev.get('content',''))[:80]}")

asyncio.run(main())
```

`graph.execute` 参数：`graph_id`（必填）、`input`、`session_id`、`task_id`、`variables`、`agent_id`。返回事件流。

### 执行事件类型

常见事件（`ev["type"]`）：

| 事件 | 含义 |
|------|------|
| `NODE_START` / `NODE_END` | 节点开始 / 结束 |
| `THINK` | LLM 思考输出 |
| `TOKEN` | 真流式逐 token（见 07 篇 SSE/WS） |
| `TOOL_CALL` / `TOOL_RESULT` | 工具调用 / 结果 |
| `ERROR` | 错误（`metadata.tool_error` 标记工具错） |
| `COMPLETE` | 图完成 |

## 两条路径怎么选

| | 声明式 `agent.create` | 图式 `graph.create` |
|---|---|---|
| 适合 | 单轮/多轮对话、固定工具链 | 多步骤、条件分支、并行、RAG |
| 图定义 | 自动合成（start→llm→tools） | 你画节点+边 |
| 复用 | publish 到 marketplace（见 08 篇） | 导出 JSON / Python 脚本 |
| 触发 | agent.execute + cron（见 06 篇） | graph.execute + cron |

> 进阶：图式也能绑到 agent——`graph.create` 带 `agent_id`，或用 `agent.create` 建壳后再用 `graph.update` 替换其图。日常对话用 A，复杂工作流用 B。

---

下一篇：[03 配置不同类型的 Agent](./03-configure-agents.md)
