# 08 部署与发布

> 场景：智能体调通了，技能装好了，定时任务也挂上了。现在要把它"交付"出去——导出成可移植文件、发布成可调用 API、塞进本地 marketplace 供别人装、或导出成独立 FastAPI 服务。本篇讲四条交付路径。

## 本篇你将完成

- 导出图（json / yaml / python / fastapi 四种格式）
- 导入图（限定目录，防穿越）
- 发布智能体为 HTTP 聊天端点（`agent.publish`）
- 发布到本地 marketplace 并安装
- 用 HTTP API（端口 11455）远程调用
- launchd 持久化（生产常驻）

---

## 准备

复用 `rpc.py`（见 [索引页](./README.md#最小-rpc-调用脚手架)）。先有个图或智能体（前面几篇建过）。

## 路径一：导出 / 导入图

把图从 daemon 存储里导成文件，方便版本管理、跨机迁移、纳入 git。

### 导出

`deploy.export` 支持四种格式：

| format | 产物 | 用途 |
|--------|------|------|
| `json` | 图 JSON | 跨机导入、存档 |
| `yaml` | 图 YAML | 人读友好、入 git |
| `python` | 可跑 Python 脚本 | 独立运行、CI |
| `fastapi` | 带 FastAPI server 的 .py | 独立 HTTP 服务 |

```python
from rpc import rpc
import asyncio

async def main():
    # JSON（最常用）
    r = await rpc("deploy.export", {
        "graph_id": "first-graph-001",
        "format": "json",
        "filepath": "~/.fusion-agent-studio/exports/first-graph.json",
    })
    print(r)   # {"status":"ok","path":"...","format":"json"}

    # Python 脚本（with_server=True 带启动代码）
    r = await rpc("deploy.export", {
        "graph_id": "first-graph-001",
        "format": "python",
        "filepath": "~/.fusion-agent-studio/exports/first_graph.py",
        "with_server": True,
    })

    # 独立 FastAPI 服务（默认 port 11453）
    r = await rpc("deploy.export", {
        "graph_id": "first-graph-001",
        "format": "fastapi",
        "filepath": "~/.fusion-agent-studio/exports/first_graph_api.py",
        "port": 11460,
    })

asyncio.run(main())
```

`deploy.export` 参数：`graph_id`、`format`、`filepath`（不传则写临时目录）、`with_server`（python 格式用）、`port`（fastapi 格式用）。返回 `{status, path, format}`。

`deploy.list_formats` 查支持格式：
```python
await rpc("deploy.list_formats", {})   # {"formats": ["json","python","yaml","fastapi"]}
```

### 导入

```python
r = await rpc("deploy.import", {
    "filepath": "~/.fusion-agent-studio/exports/first-graph.json",
})
print(r)   # {"graph_id": ..., "name": ..., "nodes": N, "edges": M}
```

**安全约束**：`deploy.import` 只接受 `~/.fusion-agent-studio/exports/` 目录下的文件（resolve + relative_to 校验，防 `../` 穿越）。导出和导入自成闭环——导出写到 exports/，导入只读 exports/。外部文件先拷进该目录再导入。

## 路径二：发布智能体为 HTTP 端点

把智能体发布成一个 OpenAI 风格的 chat 端点，别的应用能像调 LLM 一样调它。

```python
from rpc import rpc
import asyncio

AGENT_ID = "..."

async def main():
    r = await rpc("agent.publish", {"agent_id": AGENT_ID})
    print(r)
    # {"agent_id": ..., "status": "published",
    #  "version": 2, "published_at": 1234567890,
    #  "api_endpoint": "http://localhost:11434/v1/agents/<id>/chat"}

asyncio.run(main())
```

发布后 `api_endpoint` 可直接 POST 调用（走 fusion-mlx 的 11434 端口）。每次 publish 递增 `version`。

管理发布状态：
```python
await rpc("agent.unpublish", {"agent_id": AGENT_ID})   # 回到 draft
await rpc("agent.archive", {"agent_id": AGENT_ID})     # 归档
await rpc("agent.published_list", {})                  # 已发布列表
```

状态流转：`draft` → `published` →（`archived` / `unpublish` 回 `draft`）。已发布的不能重复 publish（报 "already published"）。

## 路径三：本地 marketplace

把智能体打包进本地 marketplace，别人能搜索、安装。

### 发布到 marketplace

```python
from rpc import rpc
import asyncio

async def main():
    r = await rpc("marketplace.publish", {
        "name": "Daily Disk Inspector",
        "author": "dahai",
        "description": "每日磁盘巡检智能体",
        "category": "ops",
        "tags": ["disk", "ops", "cron"],
        "version": "1.0.0",
        "graph_data": {  # 图定义（可从已存图取）
            "nodes": {"start": {"type": "start"}, "end": {"type": "end"}},
            "edges": [{"source_id": "start", "target_id": "end"}],
            "start_node_id": "start",
        },
    })
    print(r)   # {"entry_id": "..."}

asyncio.run(main())
```

### 搜索 / 获取 / 安装

```python
# 搜索
await rpc("marketplace.search", {"query": "disk", "category": "ops", "limit": 20})

# 列分类
await rpc("marketplace.list_categories", {})

# 获取详情
await rpc("marketplace.get", {"entry_id": "..."})

# 安装到本地（target_dir 可选）
await rpc("marketplace.install", {"entry_id": "...", "target_dir": "~/.fusion-agent-studio/agents"})

# 卸载
await rpc("marketplace.uninstall", {"entry_id": "..."})
```

安装后图落到本地 `~/.fusion-agent-studio/`，可像自建图一样 `graph.execute`。

## 路径四：HTTP API 远程调用

除了 UDS socket，daemon 还开一个 HTTP API（端口 11455），方便非本机进程 / 容器 / 远程服务调用。

```bash
# 健康检查
curl http://127.0.0.1:11455/health

# 执行图（需鉴权，配 API key 时带 x-api-key 头）
curl -X POST http://127.0.0.1:11455/v1/graphs/first-graph-001/execute \
  -H "Content-Type: application/json" \
  -H "x-api-key: $FUSION_API_KEY" \
  -d '{"input": "巡检磁盘"}'
```

> **安全**：HTTP API 默认仅 `127.0.0.1`。WS / SSE 执行端点同样需鉴权（query 参数 `?api_key=...`）。配了 API key 后，未带 key 的请求被拒（401 / WS 4401）。CORS 收紧——任意网页 JS 不能直接打 11455 触发执行。生产环境务必配 `FUSION_API_KEY`。

HTTP 端口在 `daemon_server.py`（默认 11455），WS 默认 11435（需 `FUSION_ENABLE_WS=1` + `FUSION_WS_TOKEN`）。

## 持久化：launchd 常驻

生产部署最后一步——让 daemon 开机自启、崩溃自拉起（cron / 定时任务依赖它常驻）：

```bash
./start.sh install-launchd     # 安装并加载
./start.sh status              # 验证
./start.sh uninstall-launchd   # 卸载
```

plist 在 `~/Library/LaunchAgents/com.fusion-agent-studio.server.plist`：`RunAtLoad` + `KeepAlive`。详见 01 篇。

## 四条交付路径怎么选

| 路径 | 产物 | 适合 |
|------|------|------|
| `deploy.export` json/yaml | 文件 | 版本管理、跨机迁移、入 git |
| `deploy.export` python/fastapi | 独立脚本/服务 | 脱离 daemon 独立运行、CI |
| `agent.publish` | HTTP chat 端点 | 当 LLM 服务被其他应用调 |
| `marketplace.publish` | marketplace 条目 | 团队内分发、可搜索可安装 |

典型交付流：**本地调通 → `deploy.export` json 存档入 git → `agent.publish` 上线成端点 → 团队复用就 `marketplace.publish`**。要长期跑就 `install-launchd` 保常驻。

> 导出目录 `~/.fusion-agent-studio/exports/` 是导入的唯一合法来源——别绕过它直接喂任意路径，会被安全校验拒。

---

## 完整使用路径回顾

八篇串起来，从零到交付：

1. [启动 Daemon](./01-start-daemon.md) — 服务跑起来
2. [创建 Agent](./02-create-agent.md) — 建第一个智能体
3. [配置 Agent](./03-configure-agents.md) — 调模型 / 工具 / 人格
4. [安装 Skill](./04-install-skills.md) — 固定多步流程
5. [调用 MCP](./05-use-mcp.md) — 接外部能力
6. [触发与定时](./06-triggers-cron.md) — 自动跑
7. [程序化集成](./07-sdk-programmatic.md) — 写应用
8. [部署与发布](./08-deploy-publish.md) — 交付出去

更多细节见顶层 [README](../../README.md)（功能清单视角）与代码 `examples/`（真实样例）。
