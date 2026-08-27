# 01 启动 Daemon

> 场景：你已经 `pip install -e .` 装好 fusion-agent-studio，现在要把它跑起来。

fusion-agent-studio 的核心是一个后台 **daemon**——一个监听 Unix Domain Socket 的 JSON-RPC 2.0 服务。GUI（fusion-studio）、脚本、定时任务、其他服务都通过这个 socket 跟它通信。本篇带你启动它、验证健康、配置开机自启。

## 本篇你将完成

- 启动 daemon 并确认 socket 就绪
- 查看 / 停止 / 重启
- 配置开机自启（launchd）
- 理解关键环境变量

---

## 1. 前置：fusion-mlx 模型服务

daemon 本身不加载模型——所有 LLM 推理走 HTTP 调用 `fusion-mlx`。**LLM 类节点执行前，fusion-mlx 必须已在运行**。纯工具节点 / 无 LLM 的图不强依赖它，但绝大多数智能体都会用到。

```bash
# 启动模型服务（终端 1）
~/claude-home/fusion-mlx/start.sh start
~/claude-home/fusion-mlx/start.sh status   # 确认在 localhost:11434
```

> 模型下载走镜像站 `https://hf-mirror.com`，缓存于 `~/.fusion-mlx/models`。

## 2. 启动 daemon

在 fusion-agent-studio 仓库根目录：

```bash
./start.sh start
```

成功输出：
```
[INFO]  safety: injection=1 level=L2
[INFO]  starting agent-studio daemon (socket=/tmp/fusion-studio.sock)...
[INFO]  launched (PID 12345), waiting for socket...
[INFO]  agent-studio running (PID 12345), socket ready
```

`start.sh` 会等待最多 60 秒，直到 socket 文件就绪才返回 0。若 60 秒未就绪，打印最近 20 行 stderr 后退出 1。

## 3. 验证健康

```bash
./start.sh status
```
```
running (PID 12345, socket=/tmp/fusion-studio.sock)
```
未运行时打印 `not running` 并退出 1。健康判定 = 进程存活 **且** socket 文件存在。

进一步用 RPC 探活（最小调用器见 [索引页](./README.md#最小-rpc-调用脚手架)）：

```python
from rpc import rpc
import asyncio
async def main():
    r = await rpc("system.offline_status")
    print(r)
asyncio.run(main())
```

## 4. 停止 / 重启

```bash
./start.sh stop       # 优雅停止：SIGTERM，最多等 10s，再 SIGKILL，清 socket
./start.sh restart    # stop + start
```

## 5. 开机自启（launchd）

cron 定时任务依赖 daemon 常驻——daemon 被关停后没人拉起，定时任务就会错过。launchd 让 daemon **开机自启 + 崩溃/被停后自动拉起**：

```bash
./start.sh install-launchd     # 安装并加载 LaunchAgent
./start.sh uninstall-launchd   # 卸载
```

安装后生成 `~/Library/LaunchAgents/com.fusion-agent-studio.server.plist`，关键配置：`RunAtLoad=true`（登录即启）、`KeepAlive=true`（崩溃重启）、工作目录指向仓库根、注入 `FUSION_SAFETY_INJECTION=1` / `FUSION_SAFETY_LEVEL=L2`。日志仍写到仓库 `logs/`。

> 注意：plist 里**故意不设** `FUSION_MLX_API_KEY`——daemon 统一读 `~/.fusion-mlx/settings.json` 的 `auth.api_key`，避免过期 key 导致 401。

## 6. 关键环境变量

| 变量 | 默认 | 作用 |
|------|------|------|
| `FUSION_STUDIO_SOCKET` | （空） | 完整 socket 路径，最高优先级覆盖 |
| `FUSION_SOCKET_DIR` | （空） | 私有目录（`0700`），socket 置其下，防 `/tmp` 竞态 |
| `FUSION_SAFETY_LEVEL` | `L2` | 安全等级：L1 自动 / L2 预览 / L3 人工审批 |
| `FUSION_SAFETY_INJECTION` | `1` | 注入检测开关（14 模式正则） |
| `FUSION_TASK_CONCURRENCY` | `5` | task 并发上限声明（对齐 fusion-event 背压） |
| `FUSION_LOG_MAX_SIZE` | `10485760` | 单日志文件滚动阈值（字节，默认 10MB） |
| `FUSION_LOG_KEEP_COUNT` | `5` | 保留归档日志份数 |

socket 默认 `/tmp/fusion-studio.sock`。`start.sh` 与 daemon 端读同一套 env，**调用方与 daemon 须用相同 socket 路径**，否则连不上。

---

下一篇：[02 创建你的第一个 Agent](./02-create-agent.md)
