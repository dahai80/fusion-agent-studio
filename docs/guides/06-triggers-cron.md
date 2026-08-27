# 06 触发与定时任务

> 场景：你想让智能体"每天早上 8 点巡检磁盘"、"每次代码提交自动跑审查"、"并发跑 5 个任务并按项目分组看结果"。这些不是手动 `agent.execute` 能干的——需要触发器、任务队列、项目聚合。本篇讲 fusion-agent-studio 的四套触发机制。

## 本篇你将完成

- 用 cron 定时跑一个图 / 技能
- 注册一次性定时（run_at 指定时刻）
- 提交任务（`task.submit`）并查状态
- 用幂等键防重复提交
- 按项目聚合多任务
- 看任务健康度（队列深度 / 并发上限）

---

## 准备

复用 `rpc.py`（见 [索引页](./README.md#最小-rpc-调用脚手架)）。

> **前提**：cron / 一次性定时依赖 daemon 常驻。daemon 被关停后定时任务会错过。生产环境务必装开机自启（见 01 篇 `install-launchd`），否则重启机器后没人拉起。

先备一个可跑的图或技能。下面用 04 篇的 `check_disk` 技能建个图（技能也能直接挂 cron，但挂图更通用）。简单起见，先建个最小图：

```python
from rpc import rpc
import asyncio

GRAPH = {
    "id": "disk-report-001",
    "name": "Daily Disk Report",
    "nodes": {
        "start": {"type": "start", "label": "Start",
                  "system_prompt": "你是巡检助手，执行 df 并总结。"},
        "check": {"type": "tool", "label": "df",
                  "tool_name": "terminal", "tool_params": {"command": "df -h /"}},
        "report": {"type": "llm", "label": "Report",
                   "model": "qwen2.5-7b-instruct", "temperature": 0.3},
        "end": {"type": "end", "label": "Done"}
    },
    "edges": [
        {"source_id": "start", "target_id": "check"},
        {"source_id": "check", "target_id": "report"},
        {"source_id": "report", "target_id": "end"}
    ],
    "start_node_id": "start",
    "version": "1.0"
}

async def main():
    print(await rpc("graph.create", {"graph_data": GRAPH, "stable_id": True}))

asyncio.run(main())
```

> `stable_id: true` 让 graph_id 按 name+内容 hash 复用——cron 绑的是这个 id，重建图不会让 cron 失效。

## 机制一：cron 定时

cron 用标准 5 字段表达式：`分 时 日 月 周`。daemon 内部时区默认 UTC，可用 `FUSION_CRON_TZ` 改。

### 注册定时任务

```python
from rpc import rpc
import asyncio

async def main():
    r = await rpc("cron.register", {
        "id": "daily-disk",
        "name": "每日磁盘巡检",
        "expression": "0 8 * * *",        # 每天 08:00（UTC）
        "graph_id": "disk-report-001",
        "enabled": True,
        "input_data": {"note": "例行巡检"},
        "max_retries": 3,
    })
    print(r)

asyncio.run(main())
```

`cron.register` 参数：`id`（任务标识，唯一）、`name`、`expression`（5 字段 cron）、`graph_id`（要跑的图）、`enabled`、`input_data`（传给图的输入）、`max_retries`（失败重试次数）。返回 `{status:"ok", job:{...}}`。

到点后 daemon 自动 `graph.execute` 这个图，结果写进任务记录。

### 常用 cron 表达式

| 表达式 | 含义 |
|--------|------|
| `0 8 * * *` | 每天 08:00 |
| `*/30 * * * *` | 每 30 分钟 |
| `0 0 * * 1` | 每周一 00:00 |
| `0 0 1 * *` | 每月 1 号 00:00 |
| `0 9,18 * * *` | 每天 09:00 和 18:00 |

### 管理定时任务

```python
await rpc("cron.list", {})                                   # 全部
await rpc("cron.unregister", {"id": "daily-disk"})            # 删除
await rpc("cron.get", {"id": "daily-disk"})                  # 详情
```

## 机制二：一次性定时（run_at）

不需要周期，只想"明天下午 3 点跑一次"——用一次性定时。到点触发后**自动注销**，不重复。

```python
from rpc import rpc
import asyncio

async def main():
    r = await rpc("cron.register_once", {
        "id": "oneoff-deploy-check",
        "name": "部署后验证",
        "run_at": "2026-08-28T15:00:00+08:00",   # ISO8601 带时区
        "graph_id": "disk-report-001",
        "input_data": {"reason": "部署后巡检"},
    })
    print(r)

asyncio.run(main())
```

`run_at` 是 ISO8601 字符串，带时区偏移。到点触发即自动从调度器注销（one-shot 语义）。无 `max_retries` 字段。

## 机制三：任务队列（task.submit）

cron 是"到点自动跑"；`task.submit` 是"现在提交一个任务进队列，异步跑，回头查结果"。适合脚本触发 / API 触发 / 需要并发控制的场景。

### 提交并查状态

```python
from rpc import rpc
import asyncio

async def main():
    r = await rpc("task.submit", {
        "graph_id": "disk-report-001",
        "input": "巡检磁盘",
        "priority": 1,
        # 可选：idempotency_key, project_id, cron_id, run_at
    })
    task_id = r["task_id"]
    print("submitted:", task_id, "status:", r["status"])

    # 轮询状态
    s = await rpc("task.status", {"task_id": task_id})
    print("now:", s["status"])   # pending → running → completed / failed / cancelled

    # 完成后取结果
    g = await rpc("task.get", {"task_id": task_id})
    print("result:", g.get("result"))

asyncio.run(main())
```

任务状态机：`pending` → `running` → `completed` / `failed` / `cancelled`。

### 幂等：防重复提交

脚本可能重试、网络可能抖动——同一次逻辑提交被执行两次就糟了。给 `idempotency_key`，相同 key 的重复提交返回**同一个旧任务**，不新建：

```python
await rpc("task.submit", {
    "graph_id": "disk-report-001",
    "input": "巡检",
    "idempotency_key": "disk-2026-08-27-001",   # 业务侧唯一键
})
# 再提交同样的 key → 返回同一个 task_id，deduped: true
```

返回里 `deduped: true` 表示这次是命中去重、没新建任务。**空 key 不去重**（向后兼容），只有显式给值才生效。

### 任务管理 RPC

```python
await rpc("task.list", {"status": "running", "limit": 20})
await rpc("task.cancel", {"task_id": task_id})
await rpc("task.rerun", {"task_id": task_id})
await rpc("task.health", {})   # 见下
```

## 机制四：项目聚合（project）

多个相关任务按 `project_id` 打标签分组，方便整体查看。例如"巡检项目"下挂着磁盘、内存、日志三个任务。

```python
from rpc import rpc
import asyncio

async def main():
    # 提交时打项目标签
    for name, inp in [("disk", "查磁盘"), ("mem", "查内存"), ("log", "查日志")]:
        await rpc("task.submit", {
            "graph_id": "disk-report-001",
            "input": inp,
            "project_id": "inspect-2026-08",
            "idempotency_key": f"inspect-{name}-2026-08-27",
        })

    # 列出该项目下所有任务
    proj = await rpc("project.list", {})
    tasks = await rpc("project.tasks", {"project_id": "inspect-2026-08"})
    print("project tasks:", len(tasks["tasks"]))

asyncio.run(main())
```

`project.list` 列所有 project_id；`project.tasks` 取某项目下的任务列表。

## 队列健康度

`task.health` 看队列深度和并发上限，配合上游（fusion-event）做反向背压：

```python
await rpc("task.health", {})
```
```json
{"ok": true, "pending_tasks": 3, "running_tasks": 2, "total_tasks": 12, "max_concurrency": 5}
```

`max_concurrency` 来自 `FUSION_TASK_CONCURRENCY`（默认 5）。队列堆太多（pending 远超并发上限）就该降速或扩容，别无脑 submit。

## 四种机制怎么选

| 机制 | 触发方式 | 适合 |
|------|----------|------|
| cron | 周期到点 | 每日巡检、定期报告 |
| register_once | 指定时刻一次 | 部署后验证、延时任务 |
| task.submit | 即时入队 | 脚本/API 触发、需并发控制、要查结果 |
| project | 标签分组 | 多任务整体管理 |

常见组合：**cron 到点 → 内部 graph.execute → 任务落库 → project 分组查看**。一次性任务用 `register_once`；即时任务用 `task.submit` + `idempotency_key`。

> cron 默认串行变并行（多 job 到点并发 `asyncio.gather`，每个 job 有超时）。任务超过 `FUSION_TASK_TTL`（默认 30 天）会被自动清理。

---

下一篇：[07 程序化集成（SDK）](./07-sdk-programmatic.md)
