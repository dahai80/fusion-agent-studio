# 05 Agent 调用 MCP 服务

> 场景：你想让智能体用一个外部能力——比如官方的 filesystem MCP server、或自己写的 MCP 工具——而不是自己重新造。MCP（Model Context Protocol）是接外部工具/资源的标准协议。本篇讲怎么把一个 MCP server 注册进来、发现它的工具/资源/提示、让智能体用上。

## 本篇你将完成

- 注册一个 HTTP MCP server（最常见）
- 注册一个 stdio MCP server（本地子进程）
- 注册一个 SSE MCP server
- 列出 server 的工具 / 资源 / 提示
- 让智能体调用注册来的 MCP 工具
- 理解安全默认（localhost-only）与放行配置

---

## 准备

复用 `rpc.py`（见 [索引页](./README.md#最小-rpc-调用脚手架)）。

## 注册 MCP server

核心一个 RPC：`mcp.register_server`。**传输方式不用你指定**——daemon 根据你给的字段自动判断：

- 给 `server_url` → HTTP 传输
- 给 `sse_url` + `post_url` → SSE 传输
- 给 `stdio_cmd` → stdio 传输（拉起一个本地子进程）

### 方式一：HTTP MCP server

最常见，server 是个常驻 HTTP 服务：

```python
from rpc import rpc
import asyncio

async def main():
    r = await rpc("mcp.register_server", {
        "server_url": "http://localhost:8765",
        "headers": {"Authorization": "Bearer my-token"},   # 可选
        "tool_filter": ["read_file", "list_files"],         # 可选，只接部分工具
    })
    print(r)

asyncio.run(main())
```

返回：
```json
{"registered": ["read_file", "list_files"], "count": 2}
```
`registered` 是注册进来的工具名列表——这些工具之后能被智能体当内置工具一样调用。

### 方式二：stdio MCP server

server 是个可执行程序 / 脚本，daemon 拉起子进程通过 stdin/stdout 通信。给 `stdio_cmd`（命令数组）：

```python
await rpc("mcp.register_server", {
    "stdio_cmd": ["python", "-m", "my_mcp_server"],
    "env": {"MY_CONFIG": "/etc/my.conf"},   # 可选，子进程环境变量
})
```

> 安全：`stdio_cmd[0]`（可执行名）会拦截 shell / 网络解释器（`sh`、`bash`、`python -c` 这类）——避免把 MCP 退化成无约束命令执行入口。用真正的 server 程序，别拿 stdio 当 shell 通道。

### 方式三：SSE MCP server

给 `sse_url`（事件流端点）+ `post_url`（请求投递端点）：

```python
await rpc("mcp.register_server", {
    "sse_url": "http://localhost:8766/sse",
    "post_url": "http://localhost:8766/messages",
})
```

## 发现 server 的能力

注册后，server 暴露的不只是工具，还有 **resources**（可读资源，如文件、配置）和 **prompts**（预设提示模板）。

```python
# 已注册的所有 server
await rpc("mcp.list_servers", {})

# 某 server 的资源（需要先注册）
await rpc("mcp.list_resources", {"server_url": "http://localhost:8765"})

# 某 server 的提示模板
await rpc("mcp.list_prompts", {"server_url": "http://localhost:8765"})
```

> MCP 是**懒注册**：`register_server` 当时只拉起连接、取工具清单；resources / prompts 按需在 `list_*` 时才发现，不会空转轮询。

## 让智能体用上 MCP 工具

注册进来的工具名进了 daemon 的工具表。把它加进智能体的 `tools`，智能体就能调：

```python
from rpc import rpc
import asyncio

async def main():
    # 假设注册回了工具名 "read_file"
    agent = await rpc("agent.create", {
        "name": "fs-agent",
        "model": "qwen2.5-7b-instruct",
        "tools": ["read_file", "list_files"],
        "system_prompt": "你能通过 MCP 读写文件，用户给路径你就读。",
    })
    aid = agent["agent_id"]

    r = await rpc("agent.execute", {"agent_id": aid, "input": "读 /tmp/notes.txt 的内容"})
    for ev in r["events"]:
        print(f"  [{ev['type']}] {str(ev.get('content',''))[:80]}")

asyncio.run(main())
```

## 安全：默认只允许 localhost

MCP 直接连外部服务有 SSRF / 数据外泄风险。daemon **默认只允许 localhost**（`127.0.0.1` / `::1`）的 MCP server。连云上 MCP 会拒。

要放行特定外部 host，设环境变量 `FUSION_MCP_ALLOWLIST`：

```bash
# start.sh 启动前导出，逗号分隔多个 host
export FUSION_MCP_ALLOWLIST="mcp.example.com,files.internal.corp"
./start.sh restart
```

不放行就别连外部——绝大多数 MCP server 都能本地起。

> daemon 还拦截云元数据 IP（`169.254.169.254` 等）和已知 SSRF 目标。生产环境若要连外部 MCP，走 `FUSION_MCP_ALLOWLIST` 显式放行，别图省事全开。

## 管理已注册 server

```python
await rpc("mcp.unregister_server", {"server_url": "http://localhost:8765"})
await rpc("mcp.list_servers", {})
```

## 一次完整场景：接 filesystem MCP

假设你已本地起了官方 filesystem MCP server（监听 `localhost:8765`，暴露 `read_file`/`list_files`/`write_file`）：

```python
from rpc import rpc
import asyncio

async def main():
    # 1. 注册（只接读工具，写工具过滤掉更安全）
    reg = await rpc("mcp.register_server", {
        "server_url": "http://localhost:8765",
        "tool_filter": ["read_file", "list_files"],
    })
    print("registered tools:", reg["registered"])

    # 2. 建智能体并装上这些工具
    a = await rpc("agent.create", {
        "name": "fs-reader",
        "model": "qwen2.5-7b-instruct",
        "tools": reg["registered"],
        "system_prompt": "你通过 MCP 读文件。用户给路径就 list+read，然后总结。",
        "safety_level": "L1",
    })

    # 3. 跑
    r = await rpc("agent.execute", {
        "agent_id": a["agent_id"],
        "input": "列出 /tmp 下有哪些文件，并读最大的那个的前 200 字",
    })
    for ev in r["events"]:
        if ev["type"] in ("TOOL_CALL", "TOOL_RESULT", "THINK"):
            print(f"  [{ev['type']}] {str(ev.get('content',''))[:100]}")

asyncio.run(main())
```

> `tool_filter` 是个值得养成的习惯：只接需要的工具，减少智能体误用面。读写全开的 server，给只读智能体就只过 `read_*`。

---

下一篇：[06 触发与定时任务](./06-triggers-cron.md)
