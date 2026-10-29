# Fusion Agent Studio — Web UI（中文说明）

Agent 工作流 Web 应用（issue #327）：图列表、带实时 SSE 流式输出的运行提交、
运行监控、运行详情（取消 / 重放）。基于内嵌的 `@fusion/web-ui` 组件库与
`@fusion/web-sdk` 壳 SDK 构建，作为 union-web 壳模块运行。

## 两种运行模式

| 模式 | 入口 | 鉴权 |
|------|------|------|
| 内嵌（壳 iframe） | `http://127.0.0.1:11432/shell/fusion-agent-studio/` | gateway `/shell/<id>/` 代理剥掉调用方凭证、注入模块 `api_key` 为上游 `Authorization: Bearer` —— UI 不持有任何密钥 |
| 独立开发 | `http://127.0.0.1:11460` | vite 将 `/v1` 等代理到 daemon（同源无 CORS）；daemon 配置了鉴权时通过 `VITE_API_KEY` 传入静态 key |
| 构建产物（daemon 托管） | daemon 的 `GET /` | 与 REST API 相同的静态 key 门禁（`x-api-key` 或 `Bearer`） |

内嵌时（`?embedded=1` 或处于 iframe 内）底部导航自动隐藏 —— 壳负责页面框架。

dist 以相对路径构建，并从服务路径推导 API base（带 `/shell/<id>/` 前缀或为空），
一份构建产物覆盖上表全部模式。发布构建绝不带 `VITE_API_KEY`：那会把模块 key 烧进
JS bundle。无 key 构建产物由开启鉴权的 daemon 托管时，密钥需走带外通道（SSE URL
的 `?api_key=` 或 REST 的 `x-api-key`）—— 发布形态是内嵌模式，gateway 代理注入
鉴权，UI 不持有任何密钥。

## 常用命令

```bash
cd web
bun install
bun run dev          # vite 开发服务器 11460（FUSION_AGENT_STUDIO_PORT 可覆盖）
bun run typecheck    # tsc --noEmit
bun run lint         # biome
bun run build        # typecheck + vite build -> dist/
bun run preview      # 在 11460 托管 dist/
```

开发服务器把 `/v1`、`/api`、`/health`、`/metrics`、`/graphs`、`/agents`、
`/ws` 代理到 `FUSION_DEV_BACKEND`（默认 `http://127.0.0.1:11442`）。

## 端口

| 端口 | 用途 |
|------|------|
| 11442 | daemon HTTP API（REST + SSE + 构建后的 SPA） |
| 11460 | 本应用 dev/preview 服务器 |
| 11432 | fusion-gateway（内嵌入口 `/shell/fusion-agent-studio/`） |

所有端口登记在 `architecture/port-registry.yaml`，由 `scripts/check-ports.py`
校验。

## 模块 key 与 gateway 接线

daemon 在所有受保护路由上接受静态 key `FUSION_AGENT_STUDIO_API_KEY`
（`x-api-key` 头或 `Authorization: Bearer`；SSE 因 `EventSource` 无法设头，
另接受 `?api_key=` 查询参数）。

`start.sh` 首次启动生成 `~/.fusion-agent-studio/web_module_key`
（`fk-mod-` 前缀，0600 权限）并导出为 `FUSION_AGENT_STUDIO_API_KEY`，
launchd 直启的 daemon 与 gateway 配置共用同一值。

gateway 侧（`fusion-gateway` `config.yaml` 的 `web_shell.modules`）：

```yaml
web_shell:
  modules:
    - id: fusion-agent-studio
      upstream: http://127.0.0.1:11442
      api_key: <~/.fusion-agent-studio/web_module_key 文件内容>
```

设置 `FUSION_SHELL_GATEWAY_URL` 与 `FUSION_ADMIN_KEY` 后，daemon
还会自注册：启动时把 `web/manifest.json` POST 到
`POST /api/v1/shell/manifests`，之后每 15 秒心跳
（`agent_runtime/shell_registry.py`）；心跳 404（gateway 重启后）自动重注册。
#399：注册载荷注入 `upstream`（`FUSION_SHELL_UPSTREAM` 环境变量 > manifest >
`http://127.0.0.1:11442`）与 `api_key`（`FUSION_AGENT_STUDIO_API_KEY` 环境变量 >
`~/.fusion-agent-studio/web_module_key`），gateway 动态代理据此解析
`/shell/fusion-agent-studio/`，无需上方静态 web_shell.modules 配置。
api_key 绝不写入 manifest.json（仓库文件）。
gateway 管理密钥在环境变量未设时从 `~/.fusion-agent-studio/gateway_admin_key`
读取。

## 目录结构

```
src/
  sdk.ts               createShellSdk() 实例 + 内嵌判断
  i18n.ts              useLang()/useT()（中/英）基于 sdk.i18n
  api.ts               经 sdk.http 的 daemon /v1 类型化客户端
  App.tsx              hash 路由：#/workflows、#/runs、#/runs/:id
  pages/WorkflowsPage  图表格、goal 提交、实时 SSE 测试
  pages/RunsPage       仪表盘指标 + 运行表格，5 秒可见性感知轮询
  pages/RunDetailPage  TaskProgress、KV 信息、确认取消、重放
  components/StreamPanel  EventSource SSE 面板（token 计数、计时、停止/重跑）
manifest.json          壳 manifest（向 gateway 注册）
vendor/@fusion/        内嵌 web-ui + web-sdk 源码（见下）
```

## 内嵌依赖

`@fusion/web-ui` 与 `@fusion/web-sdk` 为从私有 fusion-web-shell 仓库固定
版本内嵌的源码包（commit `e2fb1cf6c876c56e844b72642a8e677c9455344f`），
保证 CI 自包含。`package.json` 以 `file:vendor/@fusion/<pkg>` 引用 —— 无
`workspace:*`、无需私有 registry。更新时重新 vendor 新源码树并更新此 pin。
