"""Sub-dispatcher: TeamDispatcher."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class TeamDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "team.swarm_register": self._handle_team_swarm_register,
            "team.swarm_agents": self._handle_team_swarm_agents,
            "team.swarm_delegate": self._handle_team_swarm_delegate,
            "team.swarm_handoff": self._handle_team_swarm_handoff,
            "team.swarm_evaluate": self._handle_team_swarm_evaluate,
            "team.swarm_escalate": self._handle_team_swarm_escalate,
            "team.swarm_stats": self._handle_team_swarm_stats,
            "team.plaza_create": self._handle_team_plaza_create,
            "team.plaza_broadcast": self._handle_team_plaza_broadcast,
            "team.plaza_messages": self._handle_team_plaza_messages,
            "team.plaza_channels": self._handle_team_plaza_channels,
            "team.plaza_break_in": self._handle_team_plaza_break_in,
            "team.plaza_circuit": self._handle_team_plaza_circuit,
            "team.fmp_register": self._handle_team_fmp_register,
            "team.fmp_send": self._handle_team_fmp_send,
            "team.fmp_stats": self._handle_team_fmp_stats,
            "team.orchestrate": self._handle_team_orchestrate,
            "team.set_limits": self._handle_team_set_limits,
            "team.get_limits": self._handle_team_get_limits,
        }

    async def _handle_team_swarm_register(self, params: dict) -> dict:
        from ..swarm_router import SwarmAgent

        swarm = self._daemon._get_swarm()
        agent = SwarmAgent(
            id=params.get("id", ""),
            name=params.get("name", ""),
            capabilities=params.get("capabilities", []),
            handoff_targets=params.get("handoff_targets", []),
            max_hops=params.get("max_hops", 3),
        )
        swarm.register_agent(agent)
        return {"ok": True, "agent": self._daemon._serialize(agent)}

    async def _handle_team_swarm_agents(self, params: dict) -> dict:
        swarm = self._daemon._get_swarm()
        return {"agents": [self._daemon._serialize(a) for a in swarm._agents.values()]}

    async def _handle_team_swarm_delegate(self, params: dict) -> dict:
        swarm = self._daemon._get_swarm()
        delegation = swarm.delegate(
            params["delegator_id"],
            params.get("task", ""),
            capability=params.get("capability", ""),
            deliverable=params.get("deliverable", ""),
        )
        return {"delegation": self._daemon._serialize(delegation)}

    async def _handle_team_swarm_handoff(self, params: dict) -> dict:
        from ..swarm_router import HandoffContext

        swarm = self._daemon._get_swarm()
        ctx = HandoffContext(
            conversation=params.get("conversation", []),
            hop_count=params.get("hop_count", 0),
            task_id=params.get("task_id", ""),
        )
        new_ctx = swarm.handoff(params["from_id"], params["to_id"], ctx)
        return {"context": self._daemon._serialize(new_ctx)}

    async def _handle_team_swarm_evaluate(self, params: dict) -> dict:
        swarm = self._daemon._get_swarm()
        delegation = swarm.evaluate(params["delegation_id"], params.get("result", {}))
        return {"delegation": self._daemon._serialize(delegation)}

    async def _handle_team_swarm_escalate(self, params: dict) -> dict:
        swarm = self._daemon._get_swarm()
        delegation = swarm.escalate(
            params["delegation_id"], reason=params.get("reason", "")
        )
        return {"delegation": self._daemon._serialize(delegation)}

    async def _handle_team_swarm_stats(self, params: dict) -> dict:
        swarm = self._daemon._get_swarm()
        return {
            "agents": len(swarm._agents),
            "delegations": len(swarm._delegations),
            "handoff_log": len(swarm._handoff_log),
            "fmp_sent": swarm.fmp._stats["sent"],
        }

    async def _handle_team_plaza_create(self, params: dict) -> dict:
        plaza = self._daemon._get_plaza()
        ch = plaza.create_channel(params["name"], params.get("participants", []))
        return {"channel": ch.name, "participants": ch.participants}

    async def _handle_team_plaza_broadcast(self, params: dict) -> dict:
        plaza = self._daemon._get_plaza()
        msg = plaza.broadcast(
            params["channel"],
            params["sender"],
            params.get("content", ""),
            mentions=params.get("mentions"),
        )
        return {"message": self._daemon._serialize(msg)}

    async def _handle_team_plaza_messages(self, params: dict) -> dict:
        plaza = self._daemon._get_plaza()
        msgs = plaza._messages.get(params["channel"], [])
        return {"messages": [self._daemon._serialize(m) for m in msgs]}

    async def _handle_team_plaza_channels(self, params: dict) -> dict:
        plaza = self._daemon._get_plaza()
        return {"channels": [ch.name for ch in plaza.list_channels()]}

    async def _handle_team_plaza_break_in(self, params: dict) -> dict:
        plaza = self._daemon._get_plaza()
        msg = plaza.human_break_in(params["channel"], params.get("content", ""))
        return {"message": self._daemon._serialize(msg)}

    async def _handle_team_plaza_circuit(self, params: dict) -> dict:
        plaza = self._daemon._get_plaza()
        return {"tripped": plaza.check_circuit_breaker(params["channel"])}

    async def _handle_team_fmp_register(self, params: dict) -> dict:
        from ..fmp_router import AgentInfo

        fmp = self._daemon._get_fmp()
        fmp.register_agent(
            AgentInfo(
                id=params.get("id", ""),
                name=params.get("name", ""),
                capabilities=params.get("capabilities", []),
            )
        )
        return {"ok": True}

    async def _handle_team_fmp_send(self, params: dict) -> dict:
        fmp = self._daemon._get_fmp()
        msg = fmp.send(
            recipient=params.get("recipient", ""),
            message_type=params.get("message_type", "request"),
            payload=params.get("payload"),
            mention_targets=params.get("mention_targets"),
            priority=params.get("priority", 5),
            round_number=params.get("round_number", 0),
        )
        return {"message": self._daemon._serialize(msg)}

    async def _handle_team_fmp_stats(self, params: dict) -> dict:
        fmp = self._daemon._get_fmp()
        return {
            "stats": dict(fmp._stats),
            "agents": len(fmp._agents),
            "message_log": len(fmp._message_log),
        }

    async def _handle_team_orchestrate(self, params: dict) -> dict:
        from ..orchestrator import AgentConfig

        orch = self._daemon._get_orchestrator()
        pattern = params.get("pattern", "sequential")
        input_text = params.get("input", "")

        def build(spec):
            graph = self._daemon.store.load_graph(spec["graph_id"])
            return AgentConfig(name=spec.get("name", spec["graph_id"]), graph=graph)

        agents = [build(s) for s in params.get("agents", [])]
        if pattern == "sequential":
            res = await orch.sequential(agents, input_text)
        elif pattern == "parallel":
            res = await orch.parallel(agents, input_text)
        elif pattern == "handoff":
            res = await orch.handoff(agents, input_text)
        elif pattern == "broadcast":
            res = await orch.broadcast(
                agents,
                input_text,
                merge_strategy=params.get("merge_strategy", "concat"),
            )
        elif pattern == "master_worker":
            res = await orch.master_worker(
                build(params["supervisor"]), agents, input_text
            )
        elif pattern == "supervisor":
            res = await orch.supervisor(
                build(params["supervisor"]),
                agents,
                input_text,
                max_rounds=params.get("max_rounds", 5),
            )
        else:
            return {"error": f"unknown pattern: {pattern}"}
        return {
            "results": res.results,
            "errors": res.errors,
            "summary": res.summary,
            "total_duration": res.total_duration,
        }

    async def start(self) -> None:
        if os.path.exists(self._daemon.socket_path):
            os.unlink(self._daemon.socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self._daemon.socket_path
        )
        # #209: 0o666→0o600 同 UID 限流, 与 daemon_server.start() 对齐.
        os.chmod(self._daemon.socket_path, 0o600)

        self._daemon._ws_server = None
        if self._daemon.ws_port:
            self._daemon._ws_server = await asyncio.start_server(
                self._handle_ws_client, "127.0.0.1", self._daemon.ws_port
            )

        self._cluster_task: asyncio.Task | None = None
        if self._daemon.cluster_port:
            try:
                import uvicorn

                from ..cluster_server import app as cluster_app

                config = uvicorn.Config(
                    cluster_app,
                    host="127.0.0.1",
                    port=self._daemon.cluster_port,
                    log_level="warning",
                )
                cluster_server = uvicorn.Server(config)
                self._cluster_task = asyncio.create_task(cluster_server.serve())
                logger.info(
                    "Cluster API server started on port %d", self._daemon.cluster_port
                )
            except Exception as e:
                logger.warning("Cluster API server failed to start: %s", e)

        self._http_task: asyncio.Task | None = None
        if self._daemon.http_port:
            try:
                import uvicorn as uvicorn2

                from ..api_server import app as fastapi_app

                http_config = uvicorn2.Config(
                    fastapi_app,
                    host="127.0.0.1",
                    port=self._daemon.http_port,
                    log_level="warning",
                )
                http_server = uvicorn2.Server(http_config)
                self._http_task = asyncio.create_task(http_server.serve())
                logger.info(
                    "FastAPI HTTP server started on port %d", self._daemon.http_port
                )
            except Exception as e:
                logger.warning("FastAPI HTTP server failed to start: %s", e)

        self._daemon._running = True
        logger.info(
            "Daemon listening on %s + WS on %d + Cluster on %d + HTTP on %d",
            self._daemon.socket_path,
            self._daemon.ws_port,
            self._daemon.cluster_port,
            self._daemon.http_port,
        )

    async def stop(self) -> None:
        self._daemon._running = False
        for task in self._daemon._active_executions.values():
            if not task.done():
                task.cancel()
        if hasattr(self, "_cron_manager") and self._cron_manager:
            self._cron_manager.close()
        if hasattr(self, "_vector_strategy") and self._vector_strategy:
            await self._vector_strategy.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._daemon._ws_server:
            self._daemon._ws_server.close()
            await self._daemon._ws_server.wait_closed()
        if self._cluster_task and not self._cluster_task.done():
            self._cluster_task.cancel()
        if self._daemon._mlx_process and self._daemon._mlx_process.poll() is None:
            self._daemon._mlx_process.terminate()
            self._daemon._mlx_process = None
        if os.path.exists(self._daemon.socket_path):
            os.unlink(self._daemon.socket_path)
        self._daemon.store.close()
        logger.info("Daemon stopped")

    async def run_forever(self) -> None:
        await self.start()
        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        def _signal_handler():
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        await stop_event.wait()
        await self.stop()

    async def _handle_team_set_limits(self, params: dict) -> dict:
        orch = self._daemon._get_orchestrator()
        result = orch.set_limits(
            max_concurrent=params.get("max_concurrent"),
            max_depth=params.get("max_depth"),
        )
        return result

    async def _handle_team_get_limits(self, params: dict) -> dict:
        orch = self._daemon._get_orchestrator()
        return orch.get_limits()
