---
doc_id: fusion-agent-studio/guide/13-cli-and-sdk
product: fusion-agent-studio
class: platform
entry: service
doc_type: guide
title: {zh: CLI 与 SDK, en: CLI and SDK}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - docs/guides/13-cli-and-sdk.md
---

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

### 审批网关（#578）

G-gate（gate:g1 / gate:g4 / gate:g5）审批任务的注册、决策与决策 webhook
注册表都走 `approvals` 命令组（agent-platform REST，鉴权同上）：

```bash
# 注册外部审批任务（assigned_to 必填 —— reviewer 才能拍板）
fusion-agent approvals create \
    --title "gate:g1 — proposal" \
    --requester pipeline-bot \
    --assigned-to owner-a \
    --type gate:g1 \
    --payload-json '{"repo":"fusion-agent-studio","pr":"#576"}'

fusion-agent approvals list --status pending

# 决策（身份来自 --decided-by 或 FUSION_APPROVAL_DECIDER）
fusion-agent approvals approve <approval_id> --decided-by owner-a
fusion-agent approvals reject <approval_id> --note "理由" --decided-by owner-a

# 决策 webhook 注册表（HMAC secret >= 16 字符，服务端不回显）
fusion-agent approvals webhooks add --url https://cb.example/hook \
    --secret sixteen-char-key --events approval.decided
fusion-agent approvals webhooks list
fusion-agent approvals webhooks remove <webhook_id>
```

**决策 webhook 线上契约（#580）**：每次审批决策向注册 URL `POST` 一次：

- 签名头 `x-fusion-signature: sha256=<hexdigest>`（HMAC-SHA256，secret 为
  注册时共享值，签名覆盖原始 body 字节）——与 fusion-gateway
  `ed25519:<b64>` 同属"算法随值上链"的生态约定
- body 为**扁平**决策对象（顶层即字段，另带 `event: "approval.decided"`
  标记，无嵌套 envelope）：`approved`（bool）、`status`、`approval_id`、
  `decided_by`、`decided_at`、`reason`、`params` 等 —— cowork 消费端
  `POST /api/hooks/webhook/{run_id}` 直接读顶层 `approved` 决定续跑/拒绝

约束（服务端强制，CLI 只做前置校验）：requester 不能自批（requester !=
decided_by）；external + 指派场景仅 assigned_to 可决策；已决策记录不可
二次决策（409）。

### 管理员密钥找回（#579 loopback）

bootstrap key 明文只在首次启动打印一次；丢失后无需登录即可通过本机 UDS
控制面找回（同 daemon 信任模型：`/tmp/fusion-studio.sock` 0o600 +
peer-uid 校验，不暴露网络，落 `apikey.create` / `apikey.rotate` 审计）：

```bash
# 新建管理员 key（明文只打印一次）
fusion-agent apikeys recover --name ops-recovery --permissions admin

# 或轮换已有 key（旧 secret 立即失效）
fusion-agent apikeys recover --rotate <key_id>

# socket 路径覆盖（默认 /tmp/fusion-studio.sock，或 FUSION_STUDIO_SOCK）
fusion-agent apikeys recover --name ops --socket /tmp/fusion-studio.sock
```

仅本机可用：这两个 RPC（`apikey.loopback_create` / `apikey.loopback_rotate`）
只接受 peer-uid 校验通过的 UDS 连接，HTTP REST 与 web RPC 桥均不可达。

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

**独立模式运行时密钥（#584）**：daemon 静态托管（:11442 直接打开）时写操作
需要鉴权，而嵌入 shell 的不变式是 UI 不持有任何密钥。独立模式下在
「API 密钥」页顶部的「本机直连访问密钥」卡片粘贴管理员密钥（来自
`fusion-agent apikeys recover` 或 daemon 首次启动日志），保存到浏览器
localStorage 后，`sdk.ts` 在每次 HTTP 调用与 SSE 查询串上自动携带；
嵌入模式永不读取该存储。构建期 `VITE_API_KEY` 仅作为开发兜底。

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
