# 07 程序化集成（SDK）

> 场景：前面的示例都用裸 socket RPC 演示底层机制。实际写应用时，你想要个 Python SDK——能建模智能体、流式拿结果、注册自定义工具、一键脚手架。本篇讲 `agent_runtime.sdk`。

## 本篇你将完成

- 用 `AgentClient` 连 daemon（注意 socket 路径坑）
- 用 `Agent` 建模并 `query` 流式拿结果
- 注册自定义工具（`Tool(handler=...)`）
- 用 `sdk.scaffold` 一键生成项目骨架
- 程序化配置（hooks / memory / context_window / tools / max_iterations / temperature）

---

## 准备

```bash
source /Users/dahai/fusion/.venv/bin/activate
pip install -e ".[test]"
```

daemon 须已启动（见 01 篇）。

## AgentClient：连 daemon

SDK 入口是 `AgentClient`。**第一个坑**：它的默认 socket 是 `~/.fusion-agent-studio/daemon.sock`，而 daemon 默认监听 `/tmp/fusion-studio.sock`——**不一致，必须显式传 socket_path**，否则连不上。

```python
from agent_runtime.sdk import AgentClient

client = AgentClient(socket_path="/tmp/fusion-studio.sock")
```

`AgentClient` 封装了前几篇裸 `rpc` 做的事，并加了类型/超时。常用方法：

| 方法 | 作用 |
|------|------|
| `create_agent(...)` | 建智能体 |
| `execute_agent(agent_id, input)` | 执行 |
| `configure_agent(agent_id, config)` | 改配置 |
| `list_agents()` / `list_tools()` | 列出 |
| `register_hook(event, handler)` | 注册生命周期 hook |
| `store_memory(agent_id, ...)` | 写记忆 |
| `register_tool(tool)` | 注册自定义工具到 daemon |

> `call()` 支持自定义 `timeout`（默认永久等待，向后兼容）。生产环境建议显式传 timeout，避免 daemon 挂住时调用方永久阻塞。

## Agent：建模并执行

`Agent` 是智能体的程序化建模，`query()` 是执行入口。

### 非流式：一次拿完整结果

```python
from agent_runtime.sdk import AgentClient, Agent

client = AgentClient(socket_path="/tmp/fusion-studio.sock")

agent = Agent(
    name="qa",
    system_prompt="你是问答助手，简洁准确。",
    model="qwen2.5-7b-instruct",
    tools=[],
)

result = agent.query(client, input="MLX 是什么？用一句话回答。")
print(result)
```

### 流式：逐 token 拿

```python
from agent_runtime.sdk import AgentClient, Agent

client = AgentClient(socket_path="/tmp/fusion-studio.sock")
agent = Agent(
    name="qa",
    system_prompt="你是问答助手。",
    model="qwen2.5-7b-instruct",
)

async for chunk in agent.query(client, input="讲讲 AgentGraph", stream=True):
    # chunk 是流式增量（token / 事件）
    print(chunk, end="", flush=True)
```

`query(stream=True)` 返回异步生成器，逐块产出；`stream=False` 返回协outine，await 拿完整结果。**同一个 `query` 不能既 stream 又 return**——SDK 内部把流式 dispatch 拆成了单独的异步生成器。

## 注册自定义工具

内置工具不够用时，写自己的工具。一个 `Tool` = 名字 + 参数 schema + 一个 async handler。

```python
import asyncio
from agent_runtime.sdk import AgentClient, Agent, Tool

client = AgentClient(socket_path="/tmp/fusion-studio.sock")

async def fetch_price(symbol: str) -> str:
    # 你的业务逻辑：查股价
    return f"{symbol}: 168.42"

price_tool = Tool(
    name="fetch_price",
    description="查股票实时价格",
    parameters={
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "股票代码"}},
        "required": ["symbol"],
    },
    handler=fetch_price,
)

# 注册到 daemon 的工具表
price_tool.register_to_daemon(client)

# 建智能体并用上它
agent = Agent(
    name="stock",
    system_prompt="你是股票助手，用 fetch_price 查价后回答。",
    model="qwen2.5-7b-instruct",
    tools=["fetch_price"],
)

async def main():
    async for chunk in agent.query(client, input="苹果股价多少？", stream=True):
        print(chunk, end="", flush=True)

asyncio.run(main())
```

`Tool.register_to_daemon(client)` 把工具注册到 daemon 的工具表（通过 `tool.register_python` RPC，daemon 用 `exec` 加载工具源码）。注册后该工具名就能进任意智能体的 `tools` 列表。

> handler 是普通 async 函数，返回 `str`。参数 schema 用 OpenAI function-calling 格式。工具名须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`。

## 程序化配置

`Agent` 构造时可直接传一组配置字段，等价于 `agent.configure`，但一步到位：

```python
agent = Agent(
    name="full",
    system_prompt="你是全功能助手。",
    model="qwen2.5-7b-instruct",
    tools=["file_read", "fetch_price"],
    context_window=65536,
    max_iterations=15,
    temperature=0.3,
    hooks={"on_session_start": my_hook},
    memory=True,   # 启用自动记忆
)
```

支持的程序化字段：`hooks`、`memory`、`context_window`、`tools`、`max_iterations`、`temperature`。这些在 `query` 前通过 `agent.configure` + `hooks.register` + `memory.store` RPC 应用到 daemon 侧。

### 生命周期 hooks

hooks 在智能体执行的关键节点触发：

| 事件 | 时机 |
|------|------|
| `on_session_start` | 会话开始 |
| `on_user_prompt_submit` | 用户输入提交后 |
| `on_pre_compact` | 上下文压缩前 |
| `on_subagent_start` / `on_subagent_end` | 子智能体调用前后 |
| `on_session_end` | 会话结束 |
| `on_stop` | 停止 |

```python
client.register_hook("on_user_prompt_submit", lambda ctx: print("user said:", ctx["input"]))
```

## 一键脚手架（sdk.scaffold）

不想从零写？`sdk.scaffold` 生成项目骨架，带模板：

```python
from agent_runtime.sdk import scaffold

scaffold(
    name="my-coder",
    template="coder",          # basic | coder | reviewer | researcher
    output_dir="./my-agents",
)
```

四个模板对应不同典型智能体：
- `basic` — 最小可跑的对话智能体
- `coder` — 带文件/终端工具的编码助手
- `reviewer` — 代码审查官
- `researcher` — 研究员（带 http/kb 工具）

生成后 `cd` 进目录改 `system_prompt` / `tools`，按里面的 README 跑。

## SDK vs 裸 RPC，何时用哪个

| | 裸 RPC（前几篇） | SDK（本篇） |
|---|---|---|
| 适合 | 理解机制、shell/非 Python 调用、最小依赖 | 写 Python 应用、要类型/流式/自定义工具 |
| 依赖 | 仅 stdlib | `pip install -e .` |
| 流式 | 自己拼 WS/SSE | `query(stream=True)` |
| 自定义工具 | 手写 `tool.register_python` | `Tool(handler=...).register_to_daemon` |

经验：**学习/调试用裸 RPC（看见每一步），写应用用 SDK（少写胶水）。** 前几篇的 RPC 概念在 SDK 里完全适用——SDK 只是封装。

> SDK 的 `AgentClient` 默认 socket 与 daemon 不同，这是历史遗留。**永远显式传 `socket_path="/tmp/fusion-studio.sock"`**，或设环境变量 `FUSION_STUDIO_SOCKET` 让两边一致。

---

下一篇：[08 部署与发布](./08-deploy-publish.md)
