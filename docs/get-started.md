---
doc_id: fusion-agent-studio/get-started
product: fusion-agent-studio
class: platform
entry: service
doc_type: get-started
title: {zh: 快速上手, en: Get Started}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - CLAUDE.md
  - start.sh
  - pyproject.toml
---

# 快速上手 / Get Started

## 前置条件 / Prerequisites

中文：macOS（Apple Silicon）、Python 3.11+、本地运行的 fusion-mlx（:11434，可经 fusion-gateway :11432 代理）。fusion-harness 与 fusion-core 是硬依赖，不在 PyPI 上，需从 fusion 工作区安装。

English: macOS (Apple Silicon), Python 3.11+, and a local fusion-mlx server (:11434, usually proxied by fusion-gateway on :11432). fusion-harness and fusion-core are hard dependencies and are not on PyPI — install them from the fusion workspace.

## 安装 / Install

```bash
cd ~/fusion/fusion-agent-studio
source ~/fusion/.venv/bin/activate
pip install -e ~/fusion/fusion-harness
pip install -e ".[test]"          # add ,[rag] for sqlite-vec, ,[plugins-extra] for plugin runtime deps
pytest tests/ -x -q               # verify the install
```

中文：`pip install -e .` 会从工作区 venv 解析 fusion-harness / fusion-core；若用独立 venv，需先以 `pip install -e` 安装这两个仓库。

English: `pip install -e .` resolves fusion-harness / fusion-core from the workspace venv; with an isolated venv, install both repos with `pip install -e` first.

## 启动服务 / Start the service

```bash
./start.sh start      # daemon: UDS socket + HTTP :11442
./start.sh status     # exit 0 = running
./start.sh stop
./start.sh restart
```

中文：start.sh 启动前会校验 :11442 已登记在 workspace `architecture/port-registry.yaml`，未登记则拒绝启动（`FUSION_SKIP_REGISTRY_CHECK=1` 可旁路并告警）。安全默认值：`FUSION_SAFETY_INJECTION=1`、`FUSION_SAFETY_LEVEL=L2`、`FUSION_PLUGINS_ENABLE=1`。日志在 `logs/stdout.log` 与 `logs/stderr.log`，默认 10MB 滚动、保留 5 个归档。

English: before starting, start.sh verifies that :11442 is registered in the workspace `architecture/port-registry.yaml` and refuses to start otherwise (`FUSION_SKIP_REGISTRY_CHECK=1` bypasses with a warning). Safety defaults: `FUSION_SAFETY_INJECTION=1`, `FUSION_SAFETY_LEVEL=L2`, `FUSION_PLUGINS_ENABLE=1`. Logs are at `logs/stdout.log` and `logs/stderr.log`, rotated at 10MB with 5 archives kept.

## 健康检查 / Health check

```bash
curl -s http://127.0.0.1:11442/health
```

中文：返回 200 表示守护进程功能就绪。WebSocket 事件端口 11433 默认关闭，需 `FUSION_ENABLE_WS=1` 才启动。

English: a 200 response means the daemon is functionally ready. The WebSocket event port 11433 is off by default and starts only with `FUSION_ENABLE_WS=1`.

## 第一个图 / First graph

```bash
python3 - <<'PY'
import asyncio
from agent_runtime import AgentRuntime, AgentGraph, NodeConfig

async def main():
    graph = AgentGraph()
    graph.add_node(NodeConfig(id="start", type="start"))
    graph.add_node(NodeConfig(id="llm", type="llm", config={"prompt": "Say hello."}))
    graph.add_edge("start", "llm")
    graph.add_node(NodeConfig(id="end", type="end"))
    graph.add_edge("llm", "end")
    rt = AgentRuntime()
    async for ev in rt.execute_graph(graph, "hello"):
        print(ev.type)

asyncio.run(main())
PY
```

中文：也可以直接用 REST API 提交图：`POST /v1/graphs` 保存，`POST /v1/graphs/{graph_id}/execute` 执行（见 docs/api-reference.md）。LLM 调用默认经 fusion-gateway :11432 发往 fusion-mlx（直连可用 `FUSION_MLX_PORT=11434` 覆盖），请确认上游可用。

English: you can also submit graphs over the REST API: `POST /v1/graphs` to save, then `POST /v1/graphs/{graph_id}/execute` to run (see docs/api-reference.md). LLM calls go through fusion-gateway :11432 by default (set `FUSION_MLX_PORT=11434` to go direct) — make sure the upstream is up.

## 常驻运行 / Keep it running

```bash
./start.sh install-launchd     # auto-start on login, restart on crash
./start.sh uninstall-launchd
```

中文：launchd 安装后由 KeepAlive 拉起，配合 fusion-supervisor 纳管时沿用本 start.sh，不要另造别名。

English: once installed, launchd KeepAlive restarts the daemon. When enrolling with fusion-supervisor, reuse this start.sh instead of a custom wrapper.
