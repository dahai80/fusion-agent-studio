# Fusion Agent Studio — Web UI

Agent workflow web application (issue #327): graph list, run submission with live
SSE streaming, run monitoring, run detail with cancel / replay. Built as a
union-web shell module on the vendored `@fusion/web-ui` component kit and
`@fusion/web-sdk` shell SDK.

## Modes

| Mode | Entry | Auth |
|------|-------|------|
| Embedded (shell iframe) | `http://127.0.0.1:11432/shell/fusion-agent-studio/` | gateway `/shell/<id>/` proxy strips caller auth and injects the module `api_key` as upstream `Authorization: Bearer` — the UI holds no keys |
| Standalone dev | `http://127.0.0.1:11460` | vite proxies `/v1` etc. to the daemon (same-origin, no CORS); pass the static key via `VITE_API_KEY` if the daemon has auth configured |
| Built SPA (served by daemon) | `GET /` on the daemon | same static-key gate as the REST API (`x-api-key` or `Bearer`) |

The bottom navigation is hidden when embedded (`?embedded=1` or inside an
iframe) — the shell owns the chrome.

The dist is built with relative asset paths and derives its API base from the
serving pathname (`/shell/<id>/` prefix or empty), so ONE build serves every
mode above. Never build with `VITE_API_KEY` for release: it bakes the module
key into the JS bundle. A keyless build served by an auth-configured daemon
needs the key out-of-band (`?api_key=` on the SSE URL or `x-api-key` on REST
calls) — release deployments are the embedded mode, where the gateway proxy
injects auth and the UI holds no keys.

## Commands

```bash
cd web
bun install
bun run dev          # vite dev server on 11460 (FUSION_AGENT_STUDIO_PORT overrides)
bun run typecheck    # tsc --noEmit
bun run lint         # biome
bun run build        # typecheck + vite build -> dist/
bun run preview      # serve dist/ on 11460
```

Dev server proxies `/v1`, `/api`, `/health`, `/metrics`, `/graphs`, `/agents`,
`/ws` to `FUSION_DEV_BACKEND` (default `http://127.0.0.1:11442`).

## Ports

| Port | Purpose |
|------|---------|
| 11442 | daemon HTTP API (REST + SSE + built SPA) |
| 11460 | this app's dev/preview server |
| 11432 | fusion-gateway (embedded entry `/shell/fusion-agent-studio/`) |

All ports are registered in `architecture/port-registry.yaml` and validated by
`scripts/check-ports.py`.

## Module key + gateway wiring

The daemon accepts the static key `FUSION_AGENT_STUDIO_API_KEY` on every
protected route (header `x-api-key` or `Authorization: Bearer`; SSE also accepts
`?api_key=` since `EventSource` cannot set headers).

`start.sh` on first start generates `~/.fusion-agent-studio/web_module_key`
(`fk-mod-` prefix, mode 0600) and exports it as `FUSION_AGENT_STUDIO_API_KEY`,
so launchd-started daemons and the gateway config share one value.

Gateway side (`fusion-gateway` `config.yaml`, `web_shell.modules`):

```yaml
web_shell:
  modules:
    - id: fusion-agent-studio
      upstream: http://127.0.0.1:11442
      api_key: <contents of ~/.fusion-agent-studio/web_module_key>
```

With `FUSION_SHELL_GATEWAY_URL` and `FUSION_GATEWAY_ADMIN_KEY` set, the daemon
also self-registers: it POSTs `web/manifest.json` to
`POST /api/v1/shell/manifests` on start and heartbeats every 15 s
(`agent_runtime/shell_registry.py`); a 404 heartbeat re-registers after a
gateway restart. The gateway admin key is read from
`~/.fusion-agent-studio/gateway_admin_key` when the env var is unset.

## Structure

```
src/
  sdk.ts               createShellSdk() instance + embedded helpers
  i18n.ts              useLang()/useT() (zh/en) via sdk.i18n
  api.ts               typed client over daemon /v1 via sdk.http + rpc() bridge
  App.tsx              hash router: #/agents, #/workflows, #/runs, #/runs/:id,
                       #/studio/<tab> (15 studio tabs via StudioNav)
  pages/WorkflowsPage  graph table + JSON create/edit (graph.* RPC), goal submit
  pages/RunsPage       dashboard tiles + runs table, 5 s visibility-aware poll
  pages/RunDetailPage  TaskProgress, KV info, cancel w/ confirm, replay
  pages/studio/        AgentStudio + AIAgent console (issues #330/#331):
                       Agents/AgentDetail (agent.* CRUD, soul/skills/versions),
                       Dashboard (overview+mlx+usage+audit), Team (swarm/plaza/
                       orchestrate), Memory, Safety, Connectors, Apikeys, Cron,
                       Hooks, Tools (dynamic register), Skills (skill.execute+
                       research.adaptive), Marketplace, Chat (chat.* sessions),
                       AiAgentPage (per-token SSE via /v1/agents/{id}/execute/
                       stream + Logs/Events debug tabs)
  components/StudioNav      horizontal studio tab bar
  components/StreamPanel    EventSource SSE panel (tokens, timer, stop/rerun)
  components/ui/            Modal/Tabs/Textarea/Switch/JsonEditor primitives
manifest.json          shell manifest (registered with the gateway)
vendor/@fusion/        vendored web-ui + web-sdk sources (see below)
```

## RPC bridge (#330)

Studio pages talk to the daemon through `POST /v1/rpc` — a deny-by-default
allowlisted JSON-RPC bridge over the existing dispatcher surface
(`agent_runtime/rpc_bridge.py`). Only explicitly allowlisted methods dispatch;
mutating methods are audit-logged as `web.rpc.{method}`. Agent streaming uses
the dedicated `GET /v1/agents/{agent_id}/execute/stream` SSE endpoint (#331)
that reuses the ad-hoc agent graph builder and accepts `?api_key=` or Bearer
(EventSource GET constraint).

## Vendored dependencies

`@fusion/web-ui` and `@fusion/web-sdk` are vendored source packages pinned from
the private fusion-web-shell repo (commit `e2fb1cf6c876c56e844b72642a8e677c9455344f`)
so CI is self-contained. `package.json` references them as
`file:vendor/@fusion/<pkg>` — no `workspace:*` references, no private registry
access needed. To update, re-vendor the new source tree and update this pin.
