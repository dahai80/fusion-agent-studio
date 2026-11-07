---
doc_id: fusion-agent-studio/guide/14-cluster-observability
product: fusion-agent-studio
class: platform
entry: service
doc_type: guide
title: {zh: 集群可观测性, en: Cluster Observability}
audience: engineer
status: draft
version: 0.14.17
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - docs/guides/14-cluster-observability.md
---

# 14 集群运维与可观测

本篇覆盖 #583 补齐的三块 Web 面：集群运维面板、可观测页、会话
fork/inject 操作（此前均为 API/CLI-only）。

## 集群面板（#/cluster）

数据来源是 daemon 的同源代理 `/v1/cluster-ops/*`，转发到本仓的
cluster_server（`agent_runtime/cluster_server.py`，端口 11457，
`FUSION_CLUSTER_OPS_URL` 可覆盖）。集群不可达时页面直接显示 503
详情——不会静默降级。

- **任务**：集群任务列表 + 取消 / 降级（目标模型）/ 迁移（目标节点）
- **节点**：节点列表（状态 / 活跃任务 / 评分）
- **弹性**：autoscaler 配置查看 + JSON 字段覆盖保存
- **路由**：routing summary 查看 + 策略切换

代理端点一览：

| 方法 | 路径 | cluster_server 端点 |
|------|------|---------------------|
| GET | `/v1/cluster-ops/stats` | `/api/v1/cluster/stats` |
| GET | `/v1/cluster-ops/nodes` | `/api/nodes` |
| GET | `/v1/cluster-ops/nodes/{id}/metrics` | `/api/v1/nodes/{id}/metrics` |
| GET/PUT | `/v1/cluster-ops/autoscaler/config` | `/api/v1/autoscaler/config` |
| GET | `/v1/cluster-ops/observability/alerts` | `/api/v1/observability/alerts` |
| GET | `/v1/cluster-ops/observability/suggestions` | `/api/v1/observability/suggestions` |
| GET | `/v1/cluster-ops/observability/logs/export` | `/api/v1/observability/logs/export` |
| GET | `/v1/cluster-ops/tasks` | `/api/tasks` |
| POST | `/v1/cluster-ops/tasks` | `/api/tasks/submit` |
| POST | `/v1/cluster-ops/tasks/{id}/cancel` | `/api/tasks/{id}/cancel` |
| POST | `/v1/cluster-ops/tasks/{id}/degrade` | `/api/tasks/{id}/degrade` |
| POST | `/v1/cluster-ops/tasks/{id}/migrate` | `/api/tasks/{id}/migrate` |
| GET | `/v1/cluster-ops/routing/summary` | `/api/routing/summary` |
| POST | `/v1/cluster-ops/routing/strategy` | `/api/routing/strategy` |

鉴权规则与全站一致（审计 D-6）：GET 只读放行，PUT/POST 在配置了任意
API key 后强制鉴权。

## 可观测页（#/observability）

- 告警列表（等级 + 消息 + 节点）
- 路由建议列表
- 「导出日志」按钮下载 `cluster-logs.txt`（走代理端点）

## 会话 fork / 注入（运行详情页）

运行详情页（#/runs/{id}）在结果区显示「会话操作」卡片（会话 ID 来自
run result 的 step）：

- **分支会话**：`POST /v1/sessions/{id}/fork`，toast 显示新会话 ID
- **注入**：输入下一轮内容后 `POST /v1/sessions/{id}/inject`
  （等价 CLI `fusion-agent fork` / `fusion-agent inject`，见
  [13 CLI 与 SDK](./13-cli-and-sdk.md)）

## 相关文件

- 代理实现：`agent_runtime/api_cluster_ops.py`
- cluster_server：`agent_runtime/cluster_server.py`
- Web 页面：`web/src/pages/studio/ClusterPage.tsx`、
  `web/src/pages/studio/ObservabilityPage.tsx`
- 会话操作：`web/src/pages/RunDetailPage.tsx`（SessionOps）
