# 09 切换记忆后端到 fusion-memory

> 场景：你的智能体跑了一阵，本地 SQLite 记忆库越来越满，检索开始变慢。你想要更强的语义检索 + 实体图谱 + 遗忘曲线，于是装了 fusion-memory（fm-server）。本篇讲怎么把 agent-studio 的记忆存储切到 fusion-memory，怎么确认切成功了，切过去后哪些功能会降级。

## 本篇你将完成

- 配环境变量启用 fusion-memory adapter
- 用 RPC 查当前走的是哪个后端
- 理解切换后降级的两个方法（delete_scope / count）
- 避开端口 11435 冲突

---

## 准备

复用 `rpc.py`（见 [索引页](./README.md#最小-rpc-调用脚手架)）。

前置：fusion-memory 的 fm-server 已装好并能在 `127.0.0.1:11435` 跑起来。它的 HTTP JSON-RPC 2.0 接口要求 Bearer 鉴权，你需要一个 API key。

## 1. 启用 adapter

记忆后端切换是**纯环境变量控制**的，不改代码、不重启 daemon 之外的东西。两个 env：

| 环境变量 | 必填 | 说明 |
|----------|------|------|
| `FUSION_MEMORY_API_KEY` | 是 | fm-server Bearer 鉴权 key。**设了它 = 用 adapter**；不设 = 旧版本地 SQLite |
| `FUSION_MEMORY_BASE_URL` | 否 | fm-server 地址，默认 `http://127.0.0.1:11435` |

在启动 daemon 前设好（daemon 进程读 env，运行时改 env 不生效，需重启 daemon）：

```bash
export FUSION_MEMORY_API_KEY="your-fm-server-key"
# 可选：fm-server 不在默认口时
# export FUSION_MEMORY_BASE_URL="http://127.0.0.1:11435"
./start.sh start
```

不设 `FUSION_MEMORY_API_KEY`（或 adapter 初始化失败），daemon 自动回退到旧版 `MemoryEngine`（本地 SQLite `~/.fusion-agent-studio/memory.db`），所有 9 个方法照常工作。这是**安全网**：fm-server 没起、key 写错，都不会让 daemon 崩。

## 2. 确认当前后端

切完要验证到底走的是哪条路。用一个 RPC：

```python
from rpc import rpc
import asyncio

async def main():
    r = await rpc("memory.backend")
    print(r["result"])

asyncio.run(main())
```

走 fusion-memory adapter 时返回：
```json
{"backend": "fusion_memory", "base_url": "http://127.0.0.1:11435", "api_key_configured": true}
```

走旧版 SQLite 时返回：
```json
{"backend": "sqlite", "base_url": "/Users/you/.fusion-agent-studio/memory.db", "api_key_configured": false}
```

`api_key_configured` 是 adapter 是否拿到 key（不是 fm-server 是否在线——在线与否由存储调用时 fail-empty 体现，见下）。

## 3. 切换后的降级

adapter 完整实现了 `MemoryEngine` 的 9 方法表面（`store` / `recall` / `list_recent` / `get` / `delete` / `delete_scope` / `count` / `recall_relevant` / `auto_forget`），调用方无感。但 fm-server 当前缺两个 RPC，adapter 做了降级：

| 方法 | 降级行为 | 原因 |
|------|----------|------|
| `delete_scope` | **no-op**，记 warning 日志，返回 0 | fm-server 无按 scope 批删的 RPC |
| `count` | **近似**：用 `retrieve` 拉一批 `top_k=1000` 数 `len(blocks)` | fm-server 无 list-all-ids RPC，无法精确计数 |

其余方法直连 fm-server：`store→commit`、`recall/list_recent/recall_relevant→retrieve`、`get→GET /v1/memory/{id}`、`delete→delete(confirm=true)`、`auto_forget→consolidate`。

**fail-empty 约定**：adapter 任何调用失败（fm-server 没起 / HTTP 错 / RPC 报错）都不抛异常，而是 log warning + 返回空值（`store` 返 `""`、`recall` 返 `[]`、`recall_relevant` 返 `""`、`count` 返 `0`）。daemon 主流程不中断。所以切到 adapter 后若检索突然变空，先看 daemon 日志里有没有 `fusion-memory ... error` 行——大概率 fm-server 没起或 key 错。

## 4. 端口 11435 冲突

注意：daemon 的 **WS（WebSocket）端口默认也是 11435**。但 WS 默认是关的（`FUSION_ENABLE_WS=1` 才起），所以默认情况下不冲突。

只有当你**同时**开了 WS **又**跑了 fm-server 默认口时，两者抢 11435，其中一方 bind 失败。解法二选一：

```bash
# 解法 A：改 daemon WS 端口
export FUSION_WS_PORT=11460

# 解法 B：改 fm-server 端口（在 fusion-memory 那边设）
# export FUSION_MEMORY_HTTP_PORT=11460
```

daemon 启动时会自检：同时探测到 `FUSION_ENABLE_WS=1` 且 `FUSION_MEMORY_API_KEY` 设了，就打一条 warning 提醒你改口。

---

上一篇：[08 部署与发布](./08-deploy-publish.md)
