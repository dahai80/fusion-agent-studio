---
doc_id: fusion-agent-studio/plan/multi-end-sync-2026-11
product: fusion-agent-studio
class: core
entry: developer
doc_type: architecture
title: {zh: 多端同步设计输入：daemon/client 分层与增量复制（Issue #796）, en: Multi-End Sync Design Inputs - Daemon/Client Layering and Incremental Replication (Issue #796)}
audience: maintainer
status: draft
version: 0.1
updated: 2026-11-22
owner: "@dahai"
source_of_truth:
  - docs/plans/multi-end-sync-design-2026-11.md
  - agent_runtime/daemon_server.py
  - agent_runtime/persistence.py
---

# 多端同步设计输入（Issue #796）

> 输入：`~/fusion/fusion-insight/docs/paseo-analysis-2026-11-22.md` §1、§2、§5.3（A1、A2）。
> 本文档是 RFC 输入，不是实现承诺；Electron/Expo 栈明确不采纳（fusion-studio 保持 SwiftUI 原生）。

## 1. 背景与现状盘点

Paseo 的两条成熟实践被Issue #796标记为AS多端同步的设计输入：

- **A1 daemon/client 分离**：一个双方都不依赖方向的线协议包（Paseo `packages/protocol`，zod schema SSOT，server 只被依赖不反向依赖）；daemon 管 agent 进程，app/CLI/desktop 全是薄协议客户端。
- **A2 directory-sync 增量复制**：sequence + cursor + tombstone；daemon 只留 latest projection + 有界 tombstone 集合（不存事件日志）；cursor 过期回退全量快照（Paseo `architecture.md:131-158`）。

AS 当前 serving 面（2026-11-22 实盘）：

| 表面 | 位置 | 现状 |
|---|---|---|
| UDS JSON-RPC | `agent_runtime/daemon_server.py`（约 5300 行），`/tmp/fusion-studio.sock` | 主 API；`_dispatch()` 路由到 16 个 subdispatcher，方法名 `module.action` |
| REST | `agent_runtime/api_server.py`（约 3300 行），`:11442` `/api/v1` | 与 UDS 部分重复、部分独有 |
| WS 事件 | `daemon_server.py:54` `WS_PORT = 11433`（registry `fusion-agent-studio-ws`） | `FUSION_ENABLE_WS` 默认开；`FUSION_WS_TOKEN` 未设时降级回关 |
| Web 桥 | fusion-web-shell `/v1/rpc` bridge + agent SSE | v0.7.0 落地，allowlist 行控制 |
| 客户端 | fusion-studio（SwiftUI UDS）、web GUI、`agent_runtime/sdk/`、CLI | 各自手写协议调用，无共享 schema |

核心缺口：**协议无 SSOT**。同一业务动作在 UDS dispatch、REST route、WS payload 三处各有一份形状定义，四个客户端各自维护调用代码；schema 漂移只能靠测试兜底。第二个缺口：多窗口/多设备状态一致靠"WS 丢了就全量刷新"，无增量语义。

## 2. A1 设计输入：daemon/client 分层与方向中性协议

### 2.1 目标分层

```text
agent_runtime/protocol/        # 线协议 SSOT（纯 schema + 方法目录 + 版本）
    schema.py                  # 每个方法的 request/response 模型
    methods.py                 # 方法目录：名字、版本、分页/流式标记
    capabilities.py            # 能力协商矩阵
agent_runtime/daemon_server.py # server 侧实现（依赖 protocol/）
fusion-studio / web / sdk / cli# client 侧（依赖 protocol/，互不依赖）
```

分层规则（对齐 Paseo `architecture.md:105-110` 的依赖方向）：

1. `protocol/` 零反向依赖：不 import `daemon_server`、`api_server`、`runtime`、任何 client。只依赖 pydantic/stdlib。
2. daemon 是协议的**一个**实现者，不是协议的所有者；新增方法先落 `protocol/methods.py`，再实现 handler。
3. 客户端之间互不依赖；SwiftUI 端通过代码生成或手工镜像 schema（见 2.4）。

### 2.2 方法目录与命名

沿用既有 `module.action` 纪律（与 Paseo `docs/rpc-namespacing.md` 的 dotted 命名同构）。`methods.py` 每行声明：方法名、request/response 模型、`streaming`（是否事件流）、`page`（是否 cursor 分页）、`since`（协议版本）。UDS 与 REST 从同一目录生成路由表，消灭"三处形状"。

### 2.3 能力协商

Paseo 用 capability flags 矩阵 + 可选方法存在性双通道（`agent-sdk-types.ts:175-188`）。AS 取：

- 服务端版本已有权威面：`/health` version-verify 路由（v0.18.11）。客户端连接时先取版本 + 方法目录子集。
- 客户端上报自身能力（如 web 桥不支持 UDS 独占方法），服务端按能力过滤广播，不按 UA 猜。
- 缺席的能力**明确拒绝**，不做静默降级模拟（对齐 insight R5 铁律精神：显式缺失才降级，无效值必须拒绝）。

### 2.4 迁移路径（不做大爆炸）

1. P1：`protocol/` 目录落地，先收编 5 个高频方法（task.submit / agent.create / graph.execute / chat.send / session.list）的 schema。
2. P2：REST `/api/v1` 对应路由改为从 schema 生成校验，行为不变。
3. P3：web 桥 allowlist 行改为方法目录驱动（v0.14.12 的 13 行 allowlist 是手工维护痛点）。
4. P4：SwiftUI 端镜像 schema；可选代码生成，不强制。

## 3. A2 设计输入：directory-sync 增量复制

### 3.1 数据模型

对每个可同步集合（graphs、sessions、tasks、checkpoints、skills、marketplace 目录）维护：

```text
seq        # 单调递增整数，每次 upsert/delete 时分配（daemon 内 SQLite 单写者，天然有序）
cursor     # 客户端已见到的最大 seq
tombstone  # 已删除记录的 {id, seq} 集合，有界（默认 1000 条或 24h，先到为准）
epoch      # 快照代次；daemon 全量重建/重启清 seq 时递增
```

关键取舍（Paseo 实证）：**不存事件日志**。daemon 只保留每条记录的 latest projection + 有界 tombstone；同步语义用 "seq 范围内查差异" 表达，存储 O(记录数 + tombstone 界)，不随历史增长。

### 3.2 同步流程

```text
client -> daemon: changes?collection=graphs&cursor=N&epoch=E
daemon:
  E != 当前 epoch          -> {reset: true, snapshot: 全量, next_cursor: 当前 seq}
  有差异 (seq > N)          -> {items: 变更记录, deleted: tombstone 命中的 id, next_cursor: max seq}
  无差异且 N < 下界         -> {items: [], next_cursor: N}          # 正常空轮
  N < tombstone 下界        -> {reset: true, snapshot: 全量, ...}   # cursor 过期回退
```

- `reset` 是唯一的全量信号；客户端收到后丢弃本地缓存重建，再用新 cursor 增量。
- WS 事件（11433）保持只做**及时性提示**（"该集合有变化，来拉"），权威永远是 `changes` 查询——对齐 Paseo "live stream 只管及时性、权威 fetch 是 SSOT" 三角（insight N1）。
- 断线重连 = 带 cursor 重新 `changes`，不再"丢了就全量刷新"。

### 3.3 AS 落点

- `agent_runtime/persistence.py`（AgentStore/TaskStore，约 1400 行）：各集合表加 `seq` 列 + 单调分配器；tombstone 表加 FIFO 界。
- `agent_runtime/daemon_server.py` + `api_server.py`：新增 `sync.changes` 方法（UDS/REST 同名）。
- 多窗口现状（web shell 多 tab + SwiftUI 多窗口）靠 WS 推送 + 轮询混合，事件丢失路径是全量刷新；本模式落地后统一为 cursor 续传。
- 跨设备（fusion-remote 查岗场景）同协议复用，无需独立同步通道。

## 4. 交付批次建议

| 批次 | 内容 | 验收 |
|---|---|---|
| S1 | `sync.changes` 方法 + AgentStore seq/epoch 落地（A2 最小闭环，先不做 protocol/） | 单集合（graphs）增量复制测试：写入/删除/断线重连/tombstone 过期回退全路径 |
| S2 | `protocol/` 目录 + 5 高频方法 schema 收编（A1 P1） | schema 零反向依赖（import 断言测试）；UDS/REST 校验同源 |
| S3 | tombstone 有界化 + 多集合铺开 + web 桥接入 | 多窗口并发增量同步 E2E；allowlist 由方法目录驱动 |
| S4 | SwiftUI 端镜像 schema + capability 协商 | fusion-studio 连接时能力面可见 |

S1 先行的理由：A2 不依赖 A1（sync.changes 可以先以现有 dispatch 方式落地），且直接消除"WS 丢事件全量刷新"这一用户可见缺陷；protocol/ 收编是大面积触点，放后。

## 5. 风险与不抄清单

- **不引入 Electron/Expo**（issue 明示）：分层只取"协议包方向中性"，客户端栈不动。
- **不抄 Hub 多机模式**：fusion 100% offline 单机场景不需要（insight §7 已判）。
- **seq 分配性能**：SQLite 单写者下 `MAX(seq)+1` 需在写事务内完成，避免重读竞争；批次 S1 内验证。
- **tombstone 界与 cursor 过期窗口**：界太小 → 频繁全量回退；界太大 → 删除记录元数据常驻。默认 1000/24h 作为初始参数，E2E 后调。
- **schema 镜像漂移（SwiftUI）**：P4 前手工镜像可能漂移；过渡期靠 `/health` 版本 + 方法目录版本号在连接时校验，版本不匹配显式拒绝。
