import logging
from typing import Any, Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class TeamDispatcher(SubDispatcher):
    def __init__(self, daemon: Any):
        super().__init__(daemon)
        self._swarm = None
        self._plaza = None
        self._fmp = None
        self._orchestrator = None

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

    def _serialize(self, obj):
        if obj is None:
            return None
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        try:
            import dataclasses
            return dataclasses.asdict(obj)
        except Exception:
            return str(obj)

    def _get_fmp(self):
        if self._fmp is None:
            from ..fmp_router import FMProtocol
            self._fmp = FMProtocol("daemon")
            logger.info("FMProtocol created")
        return self._fmp

    def _get_swarm(self):
        if self._swarm is None:
            from ..swarm_router import SwarmRouter
            self._swarm = SwarmRouter(fmp=self._get_fmp())
            logger.info("SwarmRouter created (shared fmp)")
        return self._swarm

    def _get_plaza(self):
        if self._plaza is None:
            from ..plaza import Plaza
            self._plaza = Plaza()
            logger.info("Plaza created")
        return self._plaza

    def _get_orchestrator(self):
        if self._orchestrator is None:
            from ..orchestrator import MultiAgentOrchestrator
            from tools import create_default_registry
            registry = create_default_registry()
            self._orchestrator = MultiAgentOrchestrator(
                tool_registry=registry,
                llm_gateway=self._daemon._gateway,
                swarm_router=self._get_swarm(),
                plaza=self._get_plaza(),
                fmp=self._get_fmp(),
            )
            logger.info("MultiAgentOrchestrator created (swarm+plaza+fmp wired)")
        return self._orchestrator

    async def _handle_team_swarm_register(self, params: dict) -> dict:
        from ..swarm_router import SwarmAgent
        swarm = self._get_swarm()
        agent = SwarmAgent(
            id=params.get("id", ""),
            name=params.get("name", ""),
            capabilities=params.get("capabilities", []),
            handoff_targets=params.get("handoff_targets", []),
            max_hops=params.get("max_hops", 3),
        )
        swarm.register_agent(agent)
        return {"ok": True, "agent": self._serialize(agent)}

    async def _handle_team_swarm_agents(self, params: dict) -> dict:
        swarm = self._get_swarm()
        return {"agents": [self._serialize(a) for a in swarm._agents.values()]}

    async def _handle_team_swarm_delegate(self, params: dict) -> dict:
        swarm = self._get_swarm()
        delegation = swarm.delegate(
            params["delegator_id"],
            params.get("task", ""),
            capability=params.get("capability", ""),
            deliverable=params.get("deliverable", ""),
        )
        return {"delegation": self._serialize(delegation)}

    async def _handle_team_swarm_handoff(self, params: dict) -> dict:
        from ..swarm_router import HandoffContext
        swarm = self._get_swarm()
        ctx = HandoffContext(
            conversation=params.get("conversation", []),
            hop_count=params.get("hop_count", 0),
            task_id=params.get("task_id", ""),
        )
        new_ctx = swarm.handoff(params["from_id"], params["to_id"], ctx)
        return {"context": self._serialize(new_ctx)}

    async def _handle_team_swarm_evaluate(self, params: dict) -> dict:
        swarm = self._get_swarm()
        delegation = swarm.evaluate(params["delegation_id"], params.get("result", {}))
        return {"delegation": self._serialize(delegation)}

    async def _handle_team_swarm_escalate(self, params: dict) -> dict:
        swarm = self._get_swarm()
        delegation = swarm.escalate(params["delegation_id"], reason=params.get("reason", ""))
        return {"delegation": self._serialize(delegation)}

    async def _handle_team_swarm_stats(self, params: dict) -> dict:
        swarm = self._get_swarm()
        return {
            "agents": len(swarm._agents),
            "delegations": len(swarm._delegations),
            "handoff_log": len(swarm._handoff_log),
            "fmp_sent": swarm.fmp._stats["sent"],
        }

    async def _handle_team_plaza_create(self, params: dict) -> dict:
        plaza = self._get_plaza()
        ch = plaza.create_channel(params["name"], params.get("participants", []))
        return {"channel": ch.name, "participants": ch.participants}

    async def _handle_team_plaza_broadcast(self, params: dict) -> dict:
        plaza = self._get_plaza()
        msg = plaza.broadcast(
            params["channel"],
            params["sender"],
            params.get("content", ""),
            mentions=params.get("mentions"),
        )
        return {"message": self._serialize(msg)}

    async def _handle_team_plaza_messages(self, params: dict) -> dict:
        plaza = self._get_plaza()
        msgs = plaza._messages.get(params["channel"], [])
        return {"messages": [self._serialize(m) for m in msgs]}

    async def _handle_team_plaza_channels(self, params: dict) -> dict:
        plaza = self._get_plaza()
        return {"channels": [ch.name for ch in plaza.list_channels()]}

    async def _handle_team_plaza_break_in(self, params: dict) -> dict:
        plaza = self._get_plaza()
        msg = plaza.human_break_in(params["channel"], params.get("content", ""))
        return {"message": self._serialize(msg)}

    async def _handle_team_plaza_circuit(self, params: dict) -> dict:
        plaza = self._get_plaza()
        return {"tripped": plaza.check_circuit_breaker(params["channel"])}

    async def _handle_team_fmp_register(self, params: dict) -> dict:
        from ..fmp_router import AgentInfo
        fmp = self._get_fmp()
        fmp.register_agent(AgentInfo(
            id=params.get("id", ""),
            name=params.get("name", ""),
            capabilities=params.get("capabilities", []),
        ))
        return {"ok": True}

    async def _handle_team_fmp_send(self, params: dict) -> dict:
        fmp = self._get_fmp()
        msg = fmp.send(
            recipient=params.get("recipient", ""),
            message_type=params.get("message_type", "request"),
            payload=params.get("payload"),
            mention_targets=params.get("mention_targets"),
            priority=params.get("priority", 5),
            round_number=params.get("round_number", 0),
        )
        return {"message": self._serialize(msg)}

    async def _handle_team_fmp_stats(self, params: dict) -> dict:
        fmp = self._get_fmp()
        return {"stats": dict(fmp._stats), "agents": len(fmp._agents), "message_log": len(fmp._message_log)}

    async def _handle_team_orchestrate(self, params: dict) -> dict:
        from ..orchestrator import AgentConfig
        orch = self._get_orchestrator()
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
            res = await orch.broadcast(agents, input_text, merge_strategy=params.get("merge_strategy", "concat"))
        elif pattern == "master_worker":
            res = await orch.master_worker(build(params["supervisor"]), agents, input_text)
        elif pattern == "supervisor":
            res = await orch.supervisor(build(params["supervisor"]), agents, input_text, max_rounds=params.get("max_rounds", 5))
        else:
            return {"error": f"unknown pattern: {pattern}"}
        return {"results": res.results, "errors": res.errors, "summary": res.summary, "total_duration": res.total_duration}

    async def _handle_team_set_limits(self, params: dict) -> dict:
        orch = self._get_orchestrator()
        result = orch.set_limits(
            max_concurrent=params.get("max_concurrent"),
            max_depth=params.get("max_depth"),
        )
        return result

    async def _handle_team_get_limits(self, params: dict) -> dict:
        orch = self._get_orchestrator()
        return orch.get_limits()
