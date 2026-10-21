# @fusion/agent-studio-sdk

TypeScript client for Fusion Agent Studio. Wraps the daemon REST surface
(`/v1/agents`, `/v1/graphs`, `/v1/runs`, `/v1/sessions`), the `/v1/rpc`
JSON-RPC bridge, and the SSE streams — no backend code, no database access.

## Install

```bash
bun add file:../fusion-agent-studio/sdk/typescript
```

Runs in Node 18+, Bun, and browsers (any runtime with `fetch` and
`ReadableStream`).

## Quick start

```ts
import { AgentStudioClient } from '@fusion/agent-studio-sdk';

const client = new AgentStudioClient({
    baseUrl: 'http://127.0.0.1:11442',
    apiKey: process.env.FUSION_AGENT_STUDIO_API_KEY,
});
const run = await client.submitRun({ goal: 'Summarize README.md', agent_id: 'writer' });
for await (const event of client.streamRun(run.run_id)) {
    console.log(event.type);
    if (event.type === 'done') break;
}
```

## Behavior

- `apiKey` is sent as `x-api-key`; SSE requests also get `?api_key=` because
  `EventSource` cannot set headers.
- Transient failures (`502` / `503` / `504` and network errors) are retried
  with linear backoff (`retries`, default 2).
- Every request is bounded by `timeoutMs` (default 60s).
- `request` replaces the JSON transport (the web UI passes its host bridge
  here); `streamRequest` replaces the SSE transport. Both keep auth and
  retry semantics in the caller.

## API

| Method | Description |
|--------|-------------|
| `rpc(method, params)` | `/v1/rpc` bridge (dispatcher methods) |
| `listAgents`, `listGraphs`, `createGraph`, `deleteGraph`, `executeGraph` | agent + graph management |
| `streamGraph`, `streamAgent` | per-token SSE streams |
| `submitRun`, `getRun`, `runEvents`, `listRuns`, `cancelRun`, `streamRun` | runs |
| `listSessions`, `getSession`, `sessionMessages`, `resumeSession`, `forkSession`, `renameSession`, `injectInput` | sessions |

## Development

```bash
cd sdk/typescript
bun install
bun run typecheck
bun test
bun run build
```
