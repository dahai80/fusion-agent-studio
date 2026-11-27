from __future__ import annotations

import json
import logging

from ..paths import studio_home

logger = logging.getLogger(__name__)


class AgentClient:
    def __init__(self, socket_path: str = "", default_timeout: float | None = None):
        if not socket_path:
            socket_path = str(studio_home() / "daemon.sock")
        self.socket_path = socket_path
        # #207: per-call timeout fallback. None keeps legacy hang-forever so
        # existing callers (cron handlers calling the daemon directly, bypassing
        # this client) stay unchanged. CLI/cron-triggered scripts pass
        # default_timeout=120 or a per-call timeout to avoid hanging forever
        # on readline when the daemon hangs.
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
        # Per-call timeout wins, else fall back to __init__ default_timeout;
        # when both are unset wait forever (backward compatible).
        effective_timeout = timeout if timeout is not None else self.default_timeout
        logger.debug("RPC call: %s (timeout=%s)", method, effective_timeout)

        import asyncio

        try:
            reader, writer = await self._connect()
            data = json.dumps(request) + "\n"
            writer.write(data.encode())
            await writer.drain()

            # #207: wrap readline in wait_for. When the daemon accepts the
            # request but never replies (event loop stuck / long-running graph
            # task / handler dead-wait), readline awaits forever and the caller
            # hangs.
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
                return {"error": response["error"].get("message", str(response["error"]))}

            return response.get("result", {})

        except asyncio.TimeoutError:
            logger.error(
                "RPC %s timeout after %ss (daemon hung or no response)",
                method,
                effective_timeout,
            )
            return {"error": f"rpc timeout {effective_timeout}s for {method}"}
        except (ConnectionRefusedError, FileNotFoundError):
            # ConnectionRefused: socket exists but daemon is not listening;
            # FileNotFoundError: socket file missing (daemon never started or
            # was cleaned up). Return one unified friendly message.
            logger.error("Daemon not running at %s", self.socket_path)
            return {"error": f"Daemon not running at {self.socket_path}"}
        except Exception as e:
            logger.exception("RPC call failed for %s", method)
            return {"error": str(e)}

    async def _connect(self):
        import asyncio

        # asyncio StreamReader defaults to limit=64KB; large single-line JSON
        # responses such as full graph.execute event dumps would exceed it and
        # raise LimitOverrunError. Raise to 16MB to cover business DAG replies.
        reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=2**24)
        return reader, writer

    async def ping(self) -> dict:
        return await self.call("ping")

    async def list_agents(self) -> dict:
        return await self.call("agent.list")

    async def create_agent(self, name: str, **kwargs) -> dict:
        params = {"name": name, **kwargs}
        return await self.call("agent.create", params)

    async def execute_agent(self, agent_id: str, input_text: str) -> dict:
        return await self.call("agent.execute", {"agent_id": agent_id, "input": input_text})

    async def list_tools(self) -> dict:
        return await self.call("tool.list")

    async def create_workflow(self, name: str, phases: list) -> dict:
        return await self.call("workflow.create", {"name": name, "phases": phases})

    async def execute_workflow(self, workflow_id: str, input_text: str = "") -> dict:
        return await self.call(
            "workflow.execute", {"workflow_id": workflow_id, "input": input_text}
        )

    async def register_hook(self, event: str, action: str = "", **kwargs) -> dict:
        # C12: SDK hook registration facade -> hooks.register RPC.
        params = {"event": event, "action": action, **kwargs}
        return await self.call("hooks.register", params)

    async def list_hooks(self) -> dict:
        return await self.call("hooks.list")

    async def store_memory(self, agent_id: str, content: str, **kwargs) -> dict:
        # C12: SDK memory store facade -> memory.store RPC.
        params = {"agent_id": agent_id, "content": content, **kwargs}
        return await self.call("memory.store", params)

    async def register_tool(self, tool_dict: dict) -> dict:
        # C12: SDK Tool daemon registration facade.
        # Python-handler tools -> tool.register_python (source exec'd);
        # schema-only -> tool.dynamic_register (terminal/shell).
        if tool_dict.get("source"):
            return await self.call("tool.register_python", tool_dict)
        return await self.call("tool.dynamic_register", tool_dict)

    async def unregister_tool(self, name: str) -> dict:
        return await self.call("tool.dynamic_unregister", {"name": name})

    async def configure_agent(self, agent_id: str, config: dict) -> dict:
        # C12: SDK agent configuration facade -> agent.configure RPC.
        return await self.call("agent.configure", {"agent_id": agent_id, "config": config})
