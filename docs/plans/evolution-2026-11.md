---
doc_id: fusion-agent-studio/plan/evolution-2026-11
product: fusion-agent-studio
class: core
entry: developer
doc_type: architecture
title: {zh: 能力补齐与超越计划（2026-11）, en: Capability Catch-up and Surpass Plan (2026-11)}
audience: maintainer
status: stable
version: 1.0
updated: 2026-11-12
owner: "@dahai"
source_of_truth:
  - docs/plans/evolution-2026-11.md
---

# fusion-agent-studio 能力补齐与超越计划（2026-11）

> 输入：`~/fusion/insight/fusion-agent-studio-evolution-2026-11.md`（业界洞察 × 现状盘点）。
> 本文档 = 该洞察的落地计划：把差距（A1-A9）映射为可交付的 issue 批次、验收标准与防线。
> 状态：PROPOSAL。基线版本：v0.14.30。

## 1. 总原则

1. 补齐优先于超越：先消灭 🔴（A1 eval runner / A2 预算一等公民），再做 🟡，⚪ 结构机会最后。
2. 只加不破：所有新能力走"新增模块 + RPC 方法 + web 页"路径，不改既有 RPC 契约；UDS 主接口不动。
3. 决策内核单一来源：预算/审批/压缩一律经 fusion-harness，studio 只做呈现与编排，不写第二套 funnel。
4. 每项能力带可执行证明：新控制面能力必须配 proof 脚本（对齐既有 10 个 proof 的文化）。
5. 真实模型验收：涉及 LLM 的验收在 fusion-mlx :11434 真实加载模型下做，不用 mock 收尾。

## 2. 基线快照（来自洞察 §5，2026-11-12）

- 已达标：AgentGraph 11 节点 / 真流 / session fork / 子 agent + swarm / ACP / MCP 双向 / 23 web 页 / 3,998 测试 / trace→SFT→fusion-trainer 闭环（独有）。
- 缺口：A1 eval runner（🔴）、A2 预算（🔴）、A3 OTel SDK（🟡）、A4 Inspector 页签（🟡）、A5 审批 webhook（🟡）、A6 playground（🟡）、A7 资产版本注册表（🟡）、A8 dev/生产分层（⚪）、A9 WS 默认关 + 云执行（🟡）。

## 3. 阶段一：成本与 eval 补课（v0.15.x - v0.16.x，0-3 个月）

### 3.1 E1 — 会话级预算强制（A2，🔴）

- 落点：`agent_runtime/budget_engine.py`（新）+ run/session 对象扩展。
- 方案：
  - budget 进 run 对象：`RunBudget{max_cost_usd, max_tokens, max_wall_s}`，随 `task.submit` / `agent.run` 传入，落 TaskStore。
  - 强制点在 harness funnel 消费侧：每轮 LLM/工具调用后记账（复用 token_budget.estimate_cost），超限 → 停止新调用 → run 转入 parked（复用 #646 parked-run 机制）+ 发 `run.budget_reached` 事件。
  - webhook 出口：复用 #455 webhook 基建，事件类型 `run.budget_reached` / `run.status_idled` / `run.status_terminated`（对齐 Managed Agents 命名）。
- 验收：pytest 覆盖（记账准确性、parked 转换、webhook 触发幂等）+ `tests/proof/prove_budget_enforcement.py`（100 轮工作负载，预算截断点误差 ≤ 1 轮）。
- 工作量：~2 周。

### 3.2 E2 — 通用 eval runner（A1，🔴）

- 落点：`agent_runtime/eval_runner.py`（新）+ `evals/` 目录（数据集 YAML + 评分器）。
- 方案：
  - 数据集 = case 列表（input / expected / 评分器引用）；评分器内置 4 类：exact、contains、LLM-judge（走本地模型）、tool-sequence。
  - **trace 重放进实验**：`eval.from_trace(trace_id)` 把一条历史 run 的事件流转为 case（input + 实际输出），改参重跑比对 — Phoenix Playground 模式的最小实现。
  - reviewer pack（#546）并入为数据集格式之一。
  - RPC：`eval.run` / `eval.list` / `eval.from_trace`；web：EvalsPage（跑批 + 结果对比表）。
  - 与 fusion-bench 分工：agent-studio 管 agent 运行时 eval（图/工具/会话级），bench 管模型基准；边界写进两边 README。
- 验收：pytest（评分器、from_trace 还原保真）+ proof `prove_eval_runner.py`（固定 20-case 集，两版 prompt 对比得分可复现）。
- 工作量：~3 周。

### 3.3 E3 — OTel SDK 迁移（A3，🟡）

- 落点：`agent_runtime/telemetry.py` 改造。
- 方案：
  - 引入 `opentelemetry-sdk` + `opentelemetry-exporter-otlp`，span 命名对齐 GenAI semconv（`gen_ai.agent.run` / `gen_ai.tool.call` / MCP spans）。
  - 手写 OTLP 推送路径改为 SDK exporter 插件；Jaeger/Tempo 兼容性回归（现有推送目标不变）。
  - 内存 span 环形缓冲保留（本地 UI 消费面不变），SDK 只替换出口编码。
  - 与 fusion-cowork / fusion-mlx 的 trace 连单链：traceparent 透传（入站请求带则延续）。
- 验收：pytest（span 命名 semconv 合规、traceparent 透传）+ 本地 Tempo 实测一条完整链（mlx → gateway → agent-studio）。
- 工作量：~2 周。

### 3.4 阶段一交付节点

| 版本 | 内容 | 出口标准 |
|---|---|---|
| v0.15.0 | E1 预算强制 + webhook | proof 8 变 9，全绿 |
| v0.15.x | E2 eval runner | EvalsPage 上线，from_trace 可用 |
| v0.16.0 | E3 OTel SDK | Tempo 单链实测截图 + 测试全绿 |

## 4. 阶段二：Inspector 与审批（v0.17.x - v0.18.x，3-9 个月）

### 4.1 E4 — Inspector 页签化（A4）

- RunDetail 重构为 5 页签：Summary / Events / Tools / Threads / Resources（对齐 Managed Agents）。
  - Tools：调用数 / 失败率 / 中位时长，行点击跳转该调用事件。
  - Threads：per-thread lane 时间线 + minimap（多 agent / 子 agent 嵌套树，数据源 parent_tool_use_id 等价字段）。
  - 事件深链：`?event={id}` 直达。
- 数据面：telemetry span 聚合 RPC `runs.tool_stats` / `runs.thread_lanes`。
- 验收：组件测试 + 1 条 e2e（多子 agent run 的 lane 呈现 + 深链定位）。
- 工作量：~3 周。

### 4.2 E5 — 审批 webhook + 离线评审员（A5）

- webhook 化：E1 的事件出口补齐审批侧（`approval.requested` / `approval.resolved`）。
- 离线评审员：带外 job（TaskStore 定时任务）读指定 run 的事件流，LLM-judge 对照 permission 规则标记越权行为，产出评审报告落 audit_logger；安全侧接 fusion-security（上游 issue 先行）。
- 验收：proof `prove_offline_reviewer.py`（注入 3 条越权样例，评审员 ≥ 2 条命中且零误报基线记录）。
- 工作量：~3 周。

### 4.3 E6 — prompt playground + 资产版本注册表（A6 + A7）

- playground：web PlaygroundPage，从 trace 选一条 LLM 调用 → 改 system/prompt/参数 → 重放比对（本地模型，零外发）。
- 注册表：prompt/skill/agent 模板版本化（`asset.put` / `asset.list` / `asset.checkout`，SQLite 存版本链），marketplace 包格式对齐。
- 验收：pytest（版本链不可变、checkout 幂等）+ e2e（trace 重放改参 → 结果 diff 呈现）。
- 工作量：~4 周。

### 4.4 E7 — WS 默认开

- `FUSION_ENABLE_WS` 默认 1，端口 11437 注册进 port-registry（已在）；启动失败降级回关 + 日志告警（不阻断 daemon）。
- 验收：start.sh status 显示 WS 态；关端口的机器启动不受影响。
- 工作量：~2 天。

## 5. 阶段三：控制面分层与超越（v0.19+，9-18 个月）

### 5.1 E8 — dev UI / 生产面分层（A8，⚪ 结构机会 = 超越点）

- studio 23 页拆两层交付：
  - dev 层（画布 / 调试 / playground / eval）：本地 daemon 直连，样例级。
  - 生产层（审批 / 预算 / 审计 / marketplace / 治理）：挂 fusion-web-shell 统一登录 + RBAC（复用 fusion-identity），同源 iframe。
- 这是与"每家都发两半"的业界惯例对齐，也是 fusion-web-shell 既有投入的收口。
- 验收：web-shell 挂载生产层页面走 gateway 登录；本地 dev 层行为不回归。

### 5.2 E9 — 调度会话 + 单机多进程 worker（A9，本地优先版）

- 不上云：worker 与 UI 进程分离（daemon 派生 worker 进程池），定时触发复用 cowork scheduler；每 worker 独立工作区目录。
- 保留云执行为可选后端（ACP/远程 daemon），默认拓扑永远 localhost — 防线 #1。
- 验收：proof `prove_worker_pool.py`（4 worker 并发 20 会话，零串扰、parked 恢复正确）。

### 5.3 超越点（业界无对应物，持续加厚）

| # | 超越点 | 依据 | 动作 |
|---|---|---|---|
| S1 | trace→SFT→fusion-trainer 闭环 | 洞察 §6.1：业界无对应物 | E2 的 eval 数据集与 SFT 导出共用 case 格式，eval 失败 case 一键转 SFT 训练样本 |
| S2 | 本地优先控制室 | 数据不出机 vs LangSmith Cloud | E1/E5 的预算与评审全部本地闭环；webhook 目标支持 localhost |
| S3 | harness 单一来源 | 洞察 §6.1 | E1 预算强制直接落在 harness funnel，证明纪律可扩展 |
| S4 | 治理 UI 可监管面 | 论文 2512.00742 方向 | E5 离线评审报告 + audit_logger 留痕按"监管可取证"格式设计 |
| S5 | proof 脚本文化 | 10 个可执行证明 | 每阶段新能力 +1 proof（E1/E2/E5/E9 各一），出口标准绑定 |

## 6. 防线（不可失守，评审门槛）

1. 本地优先不破：新功能默认拓扑 localhost（11434/11432/11442），云后端只能 opt-in。
2. harness 单一来源：预算/审批/压缩不重实现，PR 评审检查 import 来源。
3. UDS JSON-RPC 主接口只加不改：fusion-studio SwiftUI 契约回归测试常驻。
4. 数据主权：trace/审计落 `~/.fusion`，无遥测外发；OTel 出口目标仅本地/内网。
5. 每项控制面能力配 proof；CI（#670 改造后） proof job 保持全绿。
6. 上游分工：fusion-bench（模型基准）/ fusion-security（评审员安全规则）/ fusion-cowork（scheduler）不重复造，跨界先提 issue。

## 7. 风险与排序依据

- E1 先于 E2：预算是控制室地基（webhook 事件源），E5/E8 依赖其事件类型。
- E3 可与 E2 并行（telemetry 独立模块，无契约耦合）。
- E4 数据面依赖 E3 的 semconv span 命名稳定，故排阶段二。
- A9 云执行主动降级为"可选后端"：本地优先是差异化根基，不追 LangSmith Cloud 形态。
- 工作量口径：单人节奏估算；每项独立可发布，不阻塞其余。

## 8. 立即行动（本计划批准后第一批 issue）

1. issue: session budget enforcement + budget_reached webhook（E1）
2. issue: eval runner + from_trace（E2）
3. issue: OTel SDK 迁移 + GenAI semconv（E3）
4. issue: WS default-on（E7，小件先行）
