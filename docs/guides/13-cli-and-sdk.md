# 13 CLI 与 TypeScript SDK

本篇覆盖两个对外集成面：运维 CLI `fusion-agent`（#432）和 TypeScript SDK
`@fusion/agent-studio-sdk`（#433）。两者都是**薄客户端**：只调用 daemon 已有的
REST / SSE / `/v1/rpc` 面，不直接读数据库。

## 前置条件

- daemon 已启动（见 [01 启动 Daemon](./01-start-daemon.md)），默认 HTTP
  `http://127.0.0.1:11442`
- 启用鉴权时准备 API key：`FUSION_AGENT_STUDIO_API_KEY` 环境变量，或
  `~/.fusion-agent-studio/web_module_key`

## fusion-agent CLI

安装（在仓库根 `source .venv/bin/activate` 之后）：

```bash
pip install -e .
fusion-agent --help
```

### 提交运行并流式观察

```bash
# agent 运行（默认流式打印 span 事件直到终止）
fusion-agent submit writer --input "总结 README.md"

# graph 运行，从 stdin 读输入
echo "生成周报" | fusion-agent submit --graph weekly-report -i -

# 只提交不等待
fusion-agent submit writer -i "后台跑一下" --no-stream
```

`submit` 先 `POST /v1/runs` 创建 run，再连 `GET /v1/runs/{id}/stream`（SSE）
打印 `status` / `span` / `done` 帧。加 `--json` 输出原始 JSON 帧。

### 会话管理

```bash
fusion-agent list sessions --limit 20
fusion-agent show session <session_id>       # 元数据 + transcript
fusion-agent resume <session_id> -i "继续"
fusion-agent fork <session_id> --title "分支尝试"
fusion-agent inject <session_id> -i "补充要求" # 运行中会话的下一个 turn
```

会话存在 daemon 的 SessionStore 中，因此从另一台能访问 daemon 的机器
`resume` 同一 session 是支持的（默认 SQLite 后端；跨主机共享见 `#429` 的
SessionStore adapter）。

### 运行管理

```bash
fusion-agent list runs --status running
fusion-agent attach <run_id>     # 重新接上已有 run 的事件流
fusion-agent show run <run_id>
fusion-agent cancel <run_id>
```

### 沙箱选择（多节点）

```bash
fusion-agent submit writer -i "跑在大节点上" --sandbox multi-node:mini-01
```

- `local`（默认）：daemon 本地执行
- `multi-node:<node_id>`：run 记录后委派给 fusion-multi-node 集群路由
  （`FUSION_CLUSTER_URL`，默认 `http://127.0.0.1:11457`）。集群不可达时 run
  直接置为 `failed` 并返回错误，**不会**静默回退本地执行。

## TypeScript SDK

位置：`sdk/typescript/`，包名 `@fusion/agent-studio-sdk`。

```bash
cd sdk/typescript
bun install
bun run typecheck
bun test
```

最小示例（Node / Bun）：

```ts
import { AgentStudioClient } from '@fusion/agent-studio-sdk';

const client = new AgentStudioClient({ baseUrl: 'http://127.0.0.1:11442' });
const run = await client.submitRun({ goal: '总结 README.md', agent_id: 'writer' });
for await (const event of client.streamRun(run.run_id)) {
    console.log(event.type);
    if (event.type === 'done') break;
}
```

### 传输注入

Web UI（`web/src/api.ts`）不再手写 fetch：它把宿主桥 `sdk.http` 作为
`request` 传给 SDK，嵌入 union-web shell 时仍由 gateway 注入模块 key：

```ts
const rest = new AgentStudioClient({
    baseUrl: apiBase(),
    request: async (method, path, body) => {
        const res = await sdk.http.request(path, { method, body });
        return res.data;
    },
});
```

- 错误：非 2xx 抛 `AgentStudioError`（含 `status` 与解析后的 `body`）
- 重试：`502/503/504` 与网络错误按 `retries`（默认 2）线性退避
- 超时：`timeoutMs`（默认 60s）
- API：`rpc`、agents/graphs、runs、sessions、`streamGraph`/`streamAgent`/`streamRun`

## 相关接口

| 功能 | 接口 |
|------|------|
| 提交 run | `POST /v1/runs`（`goal`、`agent_id`/`graph_id`、`sandbox`） |
| 订阅 run | `GET /v1/runs/{id}/stream`（SSE） |
| run 详情/事件 | `GET /v1/runs/{id}`、`GET /v1/runs/{id}/events` |
| 会话 | `GET /v1/sessions`、`GET /v1/sessions/{id}/messages`、`POST /v1/sessions/{id}/resume|fork|inject` |
| 批量包 | `bundle.install` / `bundle.uninstall` / `bundle.list` / `bundle.get` |
| 技能斜杠命令 | `skills.commands` / `skills.invoke` |
