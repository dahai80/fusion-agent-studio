# Fusion-MLX Agent Studio 引导式使用指南

本目录是一组**场景化、引导式**使用文档，面向初次使用 fusion-agent-studio 的客户与开发者。不同于顶层 [README](../../README.md) 的"功能清单"视角，这里按**真实使用路径**组织：从启动服务到创建智能体、配置、装技能、接 MCP、触发定时任务、程序化集成、部署发布，一步步带你跑通。

## 适用读者

- 想在本机 Mac 上**跑起来一个智能体**的开发者
- 想把 fusion-agent-studio **集成进脚本 / 定时任务 / 其他服务**的工程师
- 想了解**智能体 → 技能 → 工具 → MCP** 如何串起来的产品 / 运维同学

## 阅读顺序

按编号顺序阅读，每篇都可独立查阅：

| 编号 | 文档 | 场景 |
|------|------|------|
| 01 | [启动 Daemon](./01-start-daemon.md) | 装好后第一步：启动后台服务、验证健康、开机自启 |
| 02 | [创建你的第一个 Agent](./02-create-agent.md) | 用 daemon RPC 建一个智能体图（start→llm→end）并执行 |
| 03 | [配置不同类型的 Agent](./03-configure-agents.md) | 切模型、配工具、设系统提示、调温度/迭代上限、灵魂设定 |
| 04 | [给 Agent 安装 Skill](./04-install-skills.md) | 编写技能文件、`agent.add_skill`、`skill.execute` 终端/生成步骤 |
| 05 | [Agent 调用 MCP 服务](./05-use-mcp.md) | 注册外部 MCP server（http/stdio/sse）、发现资源与提示 |
| 06 | [触发与定时任务](./06-triggers-cron.md) | cron 定时、task.submit、project 聚合、事件触发 |
| 07 | [程序化集成（SDK）](./07-sdk-programmatic.md) | Python SDK 建模、query 流式、注册自定义工具 |
| 08 | [部署与发布](./08-deploy-publish.md) | 导出包、发布到 marketplace、HTTP API 端点、launchd 持久化 |
| 09 | [切换记忆后端](./09-memory-backend.md) | 把记忆存储切到 fusion-memory、查后端、降级与端口冲突 |

## 通用约定（所有文档共用）

**交互方式**：本系列文档以 **daemon RPC（UDS JSON-RPC 2.0）** 为主示例。daemon 是 fusion-studio GUI 背后的真实通道，也是脚本 / 定时任务 / 服务集成的入口。进程内 Python API（`import AgentRuntime`）作为对照在少数篇目补充。

**Socket 路径**：默认 `/tmp/fusion-studio.sock`。可用环境变量覆盖：
- `FUSION_STUDIO_SOCKET`：完整 socket 路径（最高优先级）
- `FUSION_SOCKET_DIR`：私有目录（`0700`，防 `/tmp` 竞态），socket 置于其下

**RPC 帧格式**：单行 JSON-RPC 2.0 请求，示例：
```json
{"jsonrpc": "2.0", "id": 1, "method": "agent.list", "params": {}}
```
响应：
```json
{"jsonrpc": "2.0", "id": 1, "result": {"status": "ok", "agents": [...]}}
```

**前置依赖**：所有 RPC 调用前，daemon 须已启动（见 01 篇），且模型推理服务 fusion-mlx 须在 `localhost:11434` 运行（LLM 类节点需要）。

**安全默认**：daemon 启动默认开启注入检测 + 安全等级 L2（注入拦截、危险操作预览）。写工具（terminal / file_write 等）受安全网关约束，详见各篇相关说明。

## 最小 RPC 调用脚手架

后续每篇示例都基于这个最小 Python 调用器（裸 stdlib，无第三方依赖）：

```python
import asyncio
import json
import os

SOCKET = os.environ.get("FUSION_STUDIO_SOCKET", "/tmp/fusion-studio.sock")

async def rpc(method, params=None, msg_id=1):
    reader, writer = await asyncio.open_unix_connection(SOCKET)
    req = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        req["params"] = params
    writer.write(json.dumps(req).encode() + b"\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=30.0)
    writer.close()
    await writer.wait_closed()
    return json.loads(line)

# 用法
async def main():
    print(await rpc("agent.list"))

asyncio.run(main())
```

把上面这段存为 `rpc.py`，后续文档示例直接 `from rpc import rpc` 即可复用。

---

下一篇：[01 启动 Daemon](./01-start-daemon.md)
