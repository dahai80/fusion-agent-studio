from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


class AgentClient:
    def __init__(self, socket_path: str = "", default_timeout: float | None = None):
        if not socket_path:
            import os

            socket_path = os.path.expanduser("~/.fusion-agent-studio/daemon.sock")
        self.socket_path = socket_path
        # #207: per-call timeout fallback. None keeps legacy hang-forever so
        # existing callers (cron handler 调 daemon 内部直调不经此 client)不变.
        # CLI/cron-触发脚本传 default_timeout=120 或 per-call timeout 防 daemon
        # hung 时 readline 永久 await 挂死进程.
        self.default_timeout = default_timeout
        self._request_id = 0
        logger.info("AgentClient init, socket=%s", socket_path)

    async def call(
        self,
        method: str,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {},
        }
        # per-call timeout 优先, 否则回退 __init__ default_timeout, 均空则永久等待(向后兼容).
        effective_timeout = timeout if timeout is not None else self.default_timeout
        logger.debug("RPC call: %s (timeout=%s)", method, effective_timeout)

        import asyncio

        try:
            reader, writer = await self._connect()
            data = json.dumps(request) + "\n"
            writer.write(data.encode())
            await writer.drain()

            # #207: wait_for 包 readline. daemon 接请求但不回 (事件循环卡死 /
            # graph 长任务 / handler 内死等) 时 readline 永久 await, 调用方挂死.
            if effective_timeout is not None:
                response_data = await asyncio.wait_for(reader.readline(), timeout=effective_timeout)
            else:
                response_data = await reader.readline()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

            if not response_data:
                logger.error("Empty response for %s", method)
                return {"error": "Empty response from daemon"}

            response = json.loads(response_data.decode())

            if "error" in response:
                logger.error("RPC error for %s: %s", method, response["error"])
                return {
                    "error": response["error"].get("message", str(response["error"]))
                }

            return response.get("result", {})

        except asyncio.TimeoutError:
            logger.error(
                "RPC %s timeout after %ss (daemon hung or no response)",
                method,
                effective_timeout,
            )
            return {"error": f"rpc timeout {effective_timeout}s for {method}"}
        except (ConnectionRefusedError, FileNotFoundError):
            # ConnectionRefused: socket 存在但 daemon 未监听; FileNotFoundError:
            # socket 文件不存在 (daemon 从未起/已清理). 统一友好消息.
            logger.error("Daemon not running at %s", self.socket_path)
            return {"error": f"Daemon not running at {self.socket_path}"}
        except Exception as e:
            logger.exception("RPC call failed for %s", method)
            return {"error": str(e)}

    async def _connect(self):
        import asyncio

        # asyncio StreamReader 默认 limit=64KB, graph.execute 全量 events 等大响应
        # 单行 JSON 会超限抛 LimitOverrunError. 提高到 16MB 覆盖业务 DAG 响应.
        reader, writer = await asyncio.open_unix_connection(
            self.socket_path, limit=2**24
        )
        return reader, writer

    async def ping(self) -> dict:
        return await self.call("ping")

    async def list_agents(self) -> dict:
        return await self.call("agent.list")

    async def create_agent(self, name: str, **kwargs) -> dict:
        params = {"name": name, **kwargs}
        return await self.call("agent.create", params)

    async def execute_agent(self, agent_id: str, input_text: str) -> dict:
        return await self.call(
            "agent.execute", {"agent_id": agent_id, "input": input_text}
        )

    async def list_tools(self) -> dict:
        return await self.call("tool.list")

    async def create_workflow(self, name: str, phases: list) -> dict:
        return await self.call("workflow.create", {"name": name, "phases": phases})

    async def execute_workflow(self, workflow_id: str, input_text: str = "") -> dict:
        return await self.call(
            "workflow.execute", {"workflow_id": workflow_id, "input": input_text}
        )

    async def register_hook(self, event: str, action: str = "", **kwargs) -> dict:
        # C12: SDK hook 注册门面 -> hooks.register RPC.
        params = {"event": event, "action": action, **kwargs}
        return await self.call("hooks.register", params)

    async def list_hooks(self) -> dict:
        return await self.call("hooks.list")

    async def store_memory(self, agent_id: str, content: str, **kwargs) -> dict:
        # C12: SDK memory 存储门面 -> memory.store RPC.
        params = {"agent_id": agent_id, "content": content, **kwargs}
        return await self.call("memory.store", params)

    async def register_tool(self, tool_dict: dict) -> dict:
        # C12: SDK Tool daemon 注册门面.
        # Python handler 工具 -> tool.register_python (源码 exec);
        # schema-only -> tool.dynamic_register (terminal/shell).
        if tool_dict.get("source"):
            return await self.call("tool.register_python", tool_dict)
        return await self.call("tool.dynamic_register", tool_dict)

    async def unregister_tool(self, name: str) -> dict:
        return await self.call("tool.dynamic_unregister", {"name": name})

    async def configure_agent(self, agent_id: str, config: dict) -> dict:
        # C12: SDK agent 配置门面 -> agent.configure RPC.
        return await self.call(
            "agent.configure", {"agent_id": agent_id, "config": config}
        )
