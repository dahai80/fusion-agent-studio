---
doc_id: fusion-agent-studio/troubleshooting
product: fusion-agent-studio
class: platform
entry: service
doc_type: troubleshooting
title: {zh: 故障排查, en: Troubleshooting}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - start.sh
  - CLAUDE.md
  - docs/guides/01-start-daemon.md
---

# 故障排查 / Troubleshooting

中文：先看日志，再查进程与 socket。服务日志固定在 `logs/stdout.log` 与 `logs/stderr.log`（默认 10MB 滚动、保留 5 个归档）；老归档是 `*.0`、`*.1`。

English: check logs first, then process and socket. Service logs are at `logs/stdout.log` and `logs/stderr.log` (rotated at 10MB, 5 archives kept); old archives are `*.0`, `*.1`.

## 症状速查 / Symptom index

### 1. start.sh start 失败："port not registered — refusing to start"

中文：这是 #526 register-then-start 自检：HTTP 口 11442 未登记在 `~/fusion/architecture/port-registry.yaml`。修复：在登记表中补 `fusion-agent-studio` 条目后重试；独立部署可用 `FUSION_SKIP_REGISTRY_CHECK=1` 旁路（会告警），登记表文件缺失时仅 warn 放行。

English: this is the #526 register-then-start self-check: HTTP port 11442 is not registered in `~/fusion/architecture/port-registry.yaml`. Fix: add the `fusion-agent-studio` entry and retry; standalone deployments may bypass with `FUSION_SKIP_REGISTRY_CHECK=1` (warns). A missing registry file only warns.

### 2. start.sh 超时 "timeout after 60s waiting for socket"

中文：进程启动 60 秒内未产出 socket。先看 `logs/stderr.log` 末尾 20 行（start.sh 失败时会自动打印）。常见原因：Python 依赖缺失（fusion-harness / fusion-core 未装）、`~/.fusion-agent-studio/env.local` 中有错误 env、venv 路径不对。验证依赖：`python3 -c "import agent_runtime"`。

English: the process did not produce the socket within 60s. Read the last 20 lines of `logs/stderr.log` (start.sh prints them on failure). Common causes: missing dependencies (fusion-harness / fusion-core not installed), a broken env var in `~/.fusion-agent-studio/env.local`, or a wrong venv path. Verify the install: `python3 -c "import agent_runtime"`.

### 3. status 显示 not running 但进程存在（或反之）

中文：macOS 上 setproctitle 会改写 argv，start.sh 同时匹配 `agent_runtime.daemon_server` 与 `fusion-agent-studio daemon` 两种进程名（#490）。用 `pgrep -f agent_runtime.daemon_server` 确认；若 PID 文件丢失，杀掉进程并 `rm -f .fusion-agent-studio.pid` 后重启。

English: setproctitle rewrites argv on macOS; start.sh matches both `agent_runtime.daemon_server` and `fusion-agent-studio daemon` (#490). Confirm with `pgrep -f agent_runtime.daemon_server`; if the PID file is lost, kill the process, `rm -f .fusion-agent-studio.pid`, and start again.

### 4. socket 残留 / stale socket

中文：崩溃后 `/tmp/fusion-studio.sock` 可能残留。start.sh start 会自动 `rm -f`；手动清理：`rm -f /tmp/fusion-studio.sock`。生产建议用 `FUSION_SOCKET_DIR` 私有目录（0700）避免 /tmp TOCTOU（#209）。

English: a crashed run can leave `/tmp/fusion-studio.sock` behind. start.sh start removes it automatically; manual cleanup: `rm -f /tmp/fusion-studio.sock`. For production, use `FUSION_SOCKET_DIR` private dir (0700) to avoid /tmp TOCTOU (#209).

### 5. 401 / 认证失败

中文：配置了 `FUSION_AGENT_STUDIO_API_KEY`（或 apikeys 管理）后，REST 端点要求 Bearer。union-web 场景下 gateway 注入的模块 key 存于 `~/.fusion-agent-studio/web_module_key`——daemon 与 gateway 必须用同一文件/同值；不一致时删除该文件并重启 daemon（会重新生成）。UDS 侧 `FUSION_AUTH_ENFORCE` 开启后需带认证调用。key 解析单点在 `fusion_core.config.client_key()`：`FUSION_CLIENT_KEY` 优先，弃用别名会带告警生效而非静默丢弃（#509）。

English: once `FUSION_AGENT_STUDIO_API_KEY` is set (or keys are managed via apikeys), REST endpoints require Bearer auth. In the union-web scenario the gateway injects the module key stored at `~/.fusion-agent-studio/web_module_key` — daemon and gateway must share the same file/value; if they diverge, delete the file and restart the daemon (it regenerates). With `FUSION_AUTH_ENFORCE` on, UDS calls need auth. Key resolution is centralized in `fusion_core.config.client_key()`: `FUSION_CLIENT_KEY` wins, deprecated aliases still work with a warning instead of being silently dropped (#509).

### 6. union-web 菜单不展示本服务

中文：start.sh 日志出现 "FUSION_ADMIN_KEY unset — union-web menu registration skipped"。设置 `FUSION_ADMIN_KEY` 或写入 `~/.fusion-agent-studio/gateway_admin_key`，并确认 `FUSION_SHELL_GATEWAY_URL`（默认 `http://127.0.0.1:11432`）指向 gateway。

English: the start.sh log shows "FUSION_ADMIN_KEY unset — union-web menu registration skipped". Set `FUSION_ADMIN_KEY` or write `~/.fusion-agent-studio/gateway_admin_key`, and confirm `FUSION_SHELL_GATEWAY_URL` (default `http://127.0.0.1:11432`) points at the gateway.

### 7. WS 端口冲突 / WS port conflicts

中文：`FUSION_ENABLE_WS=1` 时若 `FUSION_WS_PORT` 与 fusion-memory fm-server（11435）或 fusion-cowork sync（11437）配置冲突，start.sh 会告警并要求改其一。默认 11433 已登记为 `fusion-agent-studio-ws`。

English: with `FUSION_ENABLE_WS=1`, if `FUSION_WS_PORT` conflicts with fusion-memory fm-server (11435) or fusion-cowork sync (11437) settings, start.sh warns and one side must change. Default 11433 is registered as `fusion-agent-studio-ws`.

### 8. 模型调用失败 / LLM calls fail

中文：默认上游是 fusion-gateway :11432；直连用 `FUSION_MLX_PORT=11434`。注意 `MLX_BASE_URL` 在 import 时冻结，改 env 后需重启 daemon（运行时热切走 `_resolve_mlx_base_url`）。先用 `curl http://127.0.0.1:11432/v1/models` 验证上游。模型文件在 `~/.fusion-mlx/models`，下载走镜像 `https://hf-mirror.com`。

English: the default upstream is fusion-gateway :11432; use `FUSION_MLX_PORT=11434` to go direct. Note `MLX_BASE_URL` is frozen at import time — restart the daemon after changing env (runtime switching goes through `_resolve_mlx_base_url`). Verify the upstream first: `curl http://127.0.0.1:11432/v1/models`. Model files live in `~/.fusion-mlx/models`; download via the mirror `https://hf-mirror.com`.

### 9. 插件未加载 / plugins not loaded

中文：`FUSION_PLUGINS_ENABLE` 被显式设为 0 时自动加载关闭；插件目录是 `~/.fusion-agent-studio/plugins/`，需含 `BaseTool` 子类。第三方插件的运行依赖（cv2/playwright 等）不在本仓库依赖内——用 `pip install -e ".[plugins-extra]"` 对齐（issue #164），否则静默加载失败。

English: plugin auto-load is off when `FUSION_PLUGINS_ENABLE` is explicitly 0; the plugin dir is `~/.fusion-agent-studio/plugins/` and files must contain `BaseTool` subclasses. Runtime deps of third-party plugins (cv2/playwright etc.) are not in this repo's dependencies — align them with `pip install -e ".[plugins-extra]"` (issue #164), otherwise loading fails silently.

### 10. 测试与 lint / tests and lint

中文：默认测试排除 e2e（`-m "not e2e"`）；CI 跑 `ruff check agent_runtime tools server tests` + 全量 pytest + 三个 proof 脚本。涉及真实模型推理的测试需确认 :11434 可用。e2e 浏览器测试用 `pytest -m e2e`（需要 Playwright 与 stub MLX）。

English: the default test run excludes e2e (`-m "not e2e"`); CI runs `ruff check agent_runtime tools server tests` plus full pytest plus three proof scripts. Tests involving real model inference need :11434 up. Browser e2e runs with `pytest -m e2e` (needs Playwright and a stub MLX).
