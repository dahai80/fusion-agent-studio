"""Daemon server — UDS + JSON-RPC 2.0 bridge for fusion-studio.

Listens on /tmp/fusion-studio.sock, newline-delimited messages.
Dispatches: ping, mlx.*, graph.*, hardware.*, knowledge.*, env.*,
            planner.*, rag.*, memory.*, safety.*, agent.*, marketplace.*

Callers: IPCClient.swift (fusion-studio GUI) connects to this via UDS.
API: JSON-RPC 2.0 with module.action method naming.
Data schemas: AgentStore for graph persistence, AgentRuntime for execution,
              AgentPackage for agent identity, subprocess.Popen for MLX lifecycle.

User instruction: "坚各个产品的边界和原则，fusion-studio的GUI基本定稿了，现在把功能做起来，开始吧"
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .graph import AgentGraph, Edge, NodeConfig
from .llm_gateway import LLMGateway
from .persistence import AgentStore
from .rag_pipeline import RAGConfig, RAGPipeline
from .runtime import AgentRuntime
from .chat_engine import ChatEngine
from .code_sandbox import CodeSandbox

logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/fusion-studio.sock"
WS_PORT = 11435
MLX_PORT = 11434
MLX_BASE_URL = f"http://127.0.0.1:{MLX_PORT}/v1"


class DaemonServer:
    def __init__(self, socket_path: str = SOCKET_PATH, ws_port: int = WS_PORT):
        self.socket_path = socket_path
        self.ws_port = ws_port
        self.store = AgentStore()
        self._gateway = LLMGateway()
        self._runtime: AgentRuntime | None = None
        self._mlx_process: subprocess.Popen | None = None
        self._active_executions: dict[str, asyncio.Task] = {}
        self._code_tasks: dict[str, dict] = {}
        self._server: asyncio.Server | None = None
        self._running = False
        self._planner = None
        self._memory = None
        self._safety = None
        self._rag: RAGPipeline | None = None
        self._agents: dict[str, dict] = {}
        self._marketplace = None
        self._chat_engine: ChatEngine | None = None
        self._orchestrator = None
        self._swarm = None
        self._plaza = None
        self._fmp = None
        self._ws_clients: list[asyncio.StreamWriter] = []
        self._ws_server: asyncio.Server | None = None
        self._connector_mgr = None
        self._apikey_mgr = None
        self._style_mgr = None

    def _get_runtime(self) -> AgentRuntime:
        if self._runtime is None:
            from tools import create_default_registry
            registry = create_default_registry()
            self._runtime = AgentRuntime(llm_gateway=self._gateway, tool_registry=registry)
            logger.info("AgentRuntime created with %d tools", len(registry._tools))
        return self._runtime

    def _get_chat_engine(self) -> ChatEngine:
        if self._chat_engine is None:
            self._chat_engine = ChatEngine(runtime=self._get_runtime(), store=self.store)
            logger.info("ChatEngine created")
        return self._chat_engine
    def _get_fmp(self):
        if self._fmp is None:
            from .fmp_router import FMProtocol
            self._fmp = FMProtocol("daemon")
            logger.info("FMProtocol created")
        return self._fmp

    def _get_swarm(self):
        if self._swarm is None:
            from .swarm_router import SwarmRouter
            self._swarm = SwarmRouter(fmp=self._get_fmp())
            logger.info("SwarmRouter created (shared fmp)")
        return self._swarm

    def _get_plaza(self):
        if self._plaza is None:
            from .plaza import Plaza
            self._plaza = Plaza()
            logger.info("Plaza created")
        return self._plaza

    def _get_orchestrator(self):
        if self._orchestrator is None:
            from .orchestrator import MultiAgentOrchestrator
            from tools import create_default_registry
            registry = create_default_registry()
            self._orchestrator = MultiAgentOrchestrator(
                tool_registry=registry,
                llm_gateway=self._gateway,
                swarm_router=self._get_swarm(),
                plaza=self._get_plaza(),
                fmp=self._get_fmp(),
            )
            logger.info("MultiAgentOrchestrator created (swarm+plaza+fmp wired)")
        return self._orchestrator

    def _get_compactor(self):
        rt = self._get_runtime()
        if rt.compactor is None:
            from .compactor import Compactor
            rt.compactor = Compactor(memory_engine=rt.memory_engine)
        return rt.compactor

    def _get_hooks(self):
        rt = self._get_runtime()
        if rt.hooks is None:
            from .hooks import HookEngine
            rt.hooks = HookEngine()
            logger.info("HookEngine created")
        return rt.hooks

    def _get_connector_manager(self):
        if self._connector_mgr is None:
            from .connectors import ConnectorManager
            base = Path.home() / ".fusion-agent-studio" / "connectors"
            self._connector_mgr = ConnectorManager(base)
            logger.info("ConnectorManager created at %s", base)
        return self._connector_mgr

    def _get_apikey_manager(self):
        if self._apikey_mgr is None:
            from .apikey_manager import ApiKeyManager
            base = Path.home() / ".fusion-agent-studio" / "apikeys"
            self._apikey_mgr = ApiKeyManager(base)
            logger.info("ApiKeyManager created at %s", base)
        return self._apikey_mgr

    def _get_style_manager(self):
        if self._style_mgr is None:
            from .style_manager import StyleManager
            base = Path.home() / ".fusion-agent-studio" / "styles"
            self._style_mgr = StyleManager(base)
            logger.info("StyleManager created at %s", base)
        return self._style_mgr

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

    async def _handle_team_swarm_register(self, params: dict) -> dict:
        from .swarm_router import SwarmAgent
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
        from .swarm_router import HandoffContext
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
        from .fmp_router import AgentInfo
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
        from .orchestrator import AgentConfig
        orch = self._get_orchestrator()
        pattern = params.get("pattern", "sequential")
        input_text = params.get("input", "")

        def build(spec):
            graph = self.store.load_graph(spec["graph_id"])
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

    async def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self.socket_path
        )
        os.chmod(self.socket_path, 0o666)

        self._ws_server = await asyncio.start_server(
            self._handle_ws_client, "127.0.0.1", self.ws_port
        )

        self._cluster_task: asyncio.Task | None = None
        try:
            from .cluster_server import app as cluster_app
            import uvicorn
            config = uvicorn.Config(cluster_app, host="0.0.0.0", port=9753, log_level="warning")
            cluster_server = uvicorn.Server(config)
            self._cluster_task = asyncio.create_task(cluster_server.serve())
            logger.info("Cluster API server started on port 9753")
        except Exception as e:
            logger.warning("Cluster API server failed to start: %s", e)

        self._running = True
        logger.info("Daemon listening on %s + WS on %d + Cluster on 9753", self.socket_path, self.ws_port)

    async def stop(self) -> None:
        self._running = False
        for task in self._active_executions.values():
            if not task.done():
                task.cancel()
        if hasattr(self, "_cron_manager") and self._cron_manager:
            self._cron_manager.close()
        if hasattr(self, "_vector_strategy") and self._vector_strategy:
            await self._vector_strategy.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        if self._cluster_task and not self._cluster_task.done():
            self._cluster_task.cancel()
        if self._mlx_process and self._mlx_process.poll() is None:
            self._mlx_process.terminate()
            self._mlx_process = None
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.store.close()
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

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("Client connected: %s", peer)
        buf = b""

        try:
            while self._running:
                data = await reader.read(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                        response = await self._dispatch(message)
                        resp_bytes = json.dumps(response).encode() + b"\n"
                        writer.write(resp_bytes)
                        await writer.drain()
                    except json.JSONDecodeError as e:
                        err = self._error_response(None, -32700, f"Parse error: {e}")
                        writer.write(json.dumps(err).encode() + b"\n")
                        await writer.drain()
                    except Exception as e:
                        logger.exception("Dispatch error")
                        err = self._error_response(None, -32603, f"Internal error: {e}")
                        writer.write(json.dumps(err).encode() + b"\n")
                        await writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Client handler error: %s", e)
        finally:
            writer.close()
            await writer.wait_closed()
            logger.info("Client disconnected: %s", peer)

    async def _dispatch(self, message: dict) -> dict:
        msg_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params", {})

        if "jsonrpc" not in message or message["jsonrpc"] != "2.0":
            return self._error_response(msg_id, -32600, "Invalid Request: missing jsonrpc 2.0")

        if not method:
            return self._error_response(msg_id, -32601, "Method not found: empty method")

        handler = self._get_handler(method)
        if handler is None:
            return self._error_response(msg_id, -32601, f"Method not found: {method}")

        try:
            result = await handler(params)
            return self._success_response(msg_id, result)
        except Exception as e:
            logger.exception("Handler error for %s", method)
            return self._error_response(msg_id, -32000, str(e))

    def _get_handler(self, method: str):
        handlers = {
            "ping": self._handle_ping,
            "mlx.start": self._handle_mlx_start,
            "mlx.stop": self._handle_mlx_stop,
            "mlx.restart": self._handle_mlx_restart,
            "mlx.status": self._handle_mlx_status,
            "mlx.health": self._handle_mlx_health,
            "mlx.set_model": self._handle_mlx_set_model,
            "mlx.infer": self._handle_mlx_infer,
            "graph.list": self._handle_graph_list,
            "graph.create": self._handle_graph_create,
            "graph.get": self._handle_graph_get,
            "graph.delete": self._handle_graph_delete,
            "graph.execute": self._handle_graph_execute,
            "graph.update": self._handle_graph_update,
            "tool.list": self._handle_tool_list,
            "tool.get": self._handle_tool_get,
            "session.list": self._handle_session_list,
            "knowledge.search": self._handle_knowledge_search,
            "knowledge.ingest": self._handle_knowledge_ingest,
            "knowledge.delete": self._handle_knowledge_delete,
            "knowledge.list": self._handle_knowledge_list,
            "knowledge.count": self._handle_knowledge_count,
            "env.health_check": self._handle_env_health_check,
            "env.repair": self._handle_env_repair,
            "env.repair_all": self._handle_env_repair_all,
            "planner.create_plan": self._handle_planner_create_plan,
            "planner.get_plan": self._handle_planner_get_plan,
            "planner.approve_plan": self._handle_planner_approve_plan,
            "planner.reject_plan": self._handle_planner_reject_plan,
            "planner.execute_step": self._handle_planner_execute_step,
            "planner.execute_plan": self._handle_planner_execute_plan,
            "planner.list_plans": self._handle_planner_list_plans,
            "planner.cancel_plan": self._handle_planner_cancel_plan,
            "verify.verify": self._handle_verify_verify,
            "rag.query": self._handle_rag_query,
            "rag.retrieve": self._handle_rag_retrieve,
            "rag.vector_search": self._handle_rag_vector_search,
            "cron.register": self._handle_cron_register,
            "cron.unregister": self._handle_cron_unregister,
            "cron.list": self._handle_cron_list,
            "cron.list_executions": self._handle_cron_list_executions,
            "tool.dynamic_register": self._handle_tool_dynamic_register,
            "tool.dynamic_unregister": self._handle_tool_dynamic_unregister,
            "memory.store": self._handle_memory_store,
            "memory.recall": self._handle_memory_recall,
            "memory.list_recent": self._handle_memory_list_recent,
            "memory.get": self._handle_memory_get,
            "memory.delete": self._handle_memory_delete,
            "memory.delete_scope": self._handle_memory_delete_scope,
            "memory.count": self._handle_memory_count,
            "memory.recall_relevant": self._handle_memory_recall_relevant,
            "memory.auto_forget": self._handle_memory_auto_forget,
            "safety.check": self._handle_safety_check,
            "safety.evaluate_action": self._handle_safety_evaluate_action,
            "safety.approve_action": self._handle_safety_approve_action,
            "safety.reject_action": self._handle_safety_reject_action,
            "safety.get_pending_actions": self._handle_safety_get_pending_actions,
            "safety.add_policy": self._handle_safety_add_policy,
            "template.list": self._handle_template_list,
            "template.get": self._handle_template_get,
            "template.instantiate": self._handle_template_instantiate,
            "deploy.export": self._handle_deploy_export,
            "deploy.import": self._handle_deploy_import,
            "deploy.list_formats": self._handle_deploy_list_formats,
            "agent.create": self._handle_agent_create,
            "agent.get": self._handle_agent_get,
            "agent.list": self._handle_agent_list,
            "agent.update": self._handle_agent_update,
            "agent.delete": self._handle_agent_delete,
            "agent.configure": self._handle_agent_configure,
            "agent.execute": self._handle_agent_execute,
            "agent.list_skills": self._handle_agent_list_skills,
            "agent.add_skill": self._handle_agent_add_skill,
            "agent.delete_skill": self._handle_agent_delete_skill,
            "skill.execute": self._handle_skill_execute,
            "research.adaptive": self._handle_research_adaptive,
            "agent.get_soul": self._handle_agent_get_soul,
            "agent.update_soul": self._handle_agent_update_soul,
            "agent.submit_code_task": self._handle_agent_submit_code_task,
            "agent.task_status": self._handle_agent_task_status,
            "agent.cancel_task": self._handle_agent_cancel_task,
            "agent.tasks": self._handle_agent_tasks,
            "agent.publish": self._handle_agent_publish,
            "agent.archive": self._handle_agent_archive,
            "agent.clone": self._handle_agent_clone,
            "agent.get_api_endpoint": self._handle_agent_get_api_endpoint,
            "agent.execute_stream": self._handle_agent_execute_stream,
            "marketplace.search": self._handle_marketplace_search,
            "marketplace.get": self._handle_marketplace_get,
            "marketplace.publish": self._handle_marketplace_publish,
            "marketplace.unpublish": self._handle_marketplace_unpublish,
            "marketplace.list_categories": self._handle_marketplace_list_categories,
            "marketplace.install": self._handle_marketplace_install,
            "marketplace.uninstall": self._handle_marketplace_uninstall,
            "chat.create": self._handle_chat_create,
            "chat.get": self._handle_chat_get,
            "chat.list": self._handle_chat_list,
            "chat.delete": self._handle_chat_delete,
            "chat.send": self._handle_chat_send,
            "chat.branch": self._handle_chat_branch,
            "chat.edit": self._handle_chat_edit,
            "chat.switch_branch": self._handle_chat_switch_branch,
            "chat.branches": self._handle_chat_branches,
            "chat.message_tree": self._handle_chat_message_tree,
            "budget.set": self._handle_budget_set,
            "budget.status": self._handle_budget_status,
            "safety.approve": self._handle_safety_approve,
            "safety.reject": self._handle_safety_reject,
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
            "hooks.list": self._handle_hooks_list,
            "hooks.register": self._handle_hooks_register,
            "hooks.test": self._handle_hooks_test,
            "context.compact": self._handle_context_compact,
            "context.usage": self._handle_context_usage,
            "connector.list": self._handle_connector_list,
            "connector.create": self._handle_connector_create,
            "connector.get": self._handle_connector_get,
            "connector.update": self._handle_connector_update,
            "connector.delete": self._handle_connector_delete,
            "connector.connect": self._handle_connector_connect,
            "connector.disconnect": self._handle_connector_disconnect,
            "connector.test": self._handle_connector_test,
            "dashboard.overview": self._handle_dashboard_overview,
            "apikey.create": self._handle_apikey_create,
            "apikey.list": self._handle_apikey_list,
            "apikey.revoke": self._handle_apikey_revoke,
            "apikey.rotate": self._handle_apikey_rotate,
            "apikey.update": self._handle_apikey_update,
            "analytics.agent_usage": self._handle_analytics_agent_usage,
            "style.list": self._handle_style_list,
            "style.get": self._handle_style_get,
            "style.create": self._handle_style_create,
            "style.apply": self._handle_style_apply,
            "alert.list": self._handle_alert_list,
            "alert.acknowledge": self._handle_alert_acknowledge,
        }
        return handlers.get(method)

    async def _handle_hooks_list(self, params: dict) -> dict:
        engine = self._get_hooks()
        return {"hooks": engine.list_hooks()}

    async def _handle_hooks_register(self, params: dict) -> dict:
        from .hooks import HookConfig
        engine = self._get_hooks()
        hook = HookConfig.from_dict(params)
        engine.register(hook)
        logger.info("hooks.register event=%s matcher=%s", hook.event, hook.matcher)
        return {"ok": True, "hook": hook.to_dict()}

    async def _handle_hooks_test(self, params: dict) -> dict:
        engine = self._get_hooks()
        event = params.get("event", "")
        payload = params.get("payload", {})
        result = await engine.fire(event, payload, tool_name=params.get("tool_name", ""))
        return {"result": self._serialize(result)}

    async def _handle_context_compact(self, params: dict) -> dict:
        compactor = self._get_compactor()
        messages = params.get("messages", [])
        level = params.get("level", "warning")
        compacted = compactor.compact(messages, level=level)
        before_tok = compactor.estimate_tokens(messages)
        after_tok = compactor.estimate_tokens(compacted)
        logger.info(
            "context.compact level=%s before_msgs=%d after_msgs=%d before_tok=%d after_tok=%d",
            level, len(messages), len(compacted), before_tok, after_tok,
        )
        return {
            "messages": compacted,
            "before_tokens": before_tok,
            "after_tokens": after_tok,
        }

    async def _handle_context_usage(self, params: dict) -> dict:
        compactor = self._get_compactor()
        messages = params.get("messages", [])
        tokens = compactor.estimate_tokens(messages)
        level = compactor.should_compact(messages)
        return {
            "tokens": tokens,
            "level": level,
            "context_window": compactor.config.context_window,
            "warning_threshold": compactor.config.warning_threshold(),
            "error_threshold": compactor.config.error_threshold(),
        }

    @staticmethod
    def _success_response(msg_id: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error_response(msg_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    # ── Handlers ──

    async def _handle_ping(self, params: dict) -> dict:
        return {"pong": True, "timestamp": time.time()}

    async def _handle_mlx_start(self, params: dict) -> dict:
        model = params.get("model", "")
        if self._mlx_process and self._mlx_process.poll() is None:
            return {"status": "already_running", "port": MLX_PORT}

        # 复用已在运行的 fusion-mlx (如 fusion-studio start.sh 启动的)，
        # 避免在已占用端口上再起子进程导致冲突 (bug1 联动)
        if await self._check_mlx_health():
            self._attach_mlx_client()
            logger.info("Reusing already-running fusion-mlx on port %d", MLX_PORT)
            return {"status": "already_running", "port": MLX_PORT,
                    "model": model, "external": True}

        cmd = [sys.executable, "-m", "fusion_mlx", "serve", "--port", str(MLX_PORT)]
        if model:
            cmd.append(model)

        logger.info("Starting fusion-mlx: %s", " ".join(cmd))
        try:
            self._mlx_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except FileNotFoundError:
            return {"status": "error", "message": "fusion-mlx not found"}

        healthy = await self._wait_mlx_healthy(timeout=30.0)
        if healthy:
            self._attach_mlx_client()
            return {"status": "started", "port": MLX_PORT, "model": model}
        else:
            return {"status": "error", "message": "fusion-mlx failed to start within 30s"}

    async def _handle_mlx_stop(self, params: dict) -> dict:
        if not self._mlx_process or self._mlx_process.poll() is not None:
            self._mlx_process = None
            return {"status": "already_stopped"}

        self._mlx_process.terminate()
        try:
            self._mlx_process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self._mlx_process.kill()
            self._mlx_process.wait(timeout=5.0)

        self._mlx_process = None
        self._detach_mlx_client()
        logger.info("fusion-mlx stopped")
        return {"status": "stopped"}

    async def _handle_mlx_restart(self, params: dict) -> dict:
        await self._handle_mlx_stop(params)
        return await self._handle_mlx_start(params)

    async def _handle_mlx_status(self, params: dict) -> dict:
        running = self._mlx_process is not None and self._mlx_process.poll() is None
        models = []
        if running:
            models = await self._list_mlx_models()

        return {
            "running": running,
            "port": MLX_PORT,
            "models": models,
            "pid": self._mlx_process.pid if running else None,
        }

    async def _handle_mlx_health(self, params: dict) -> dict:
        healthy = await self._check_mlx_health()
        return {"healthy": healthy, "port": MLX_PORT}

    async def _handle_mlx_set_model(self, params: dict) -> dict:
        model = params.get("model", "")
        if not model:
            return {"status": "error", "message": "model parameter required"}

        running = self._mlx_process is not None and self._mlx_process.poll() is None
        if running:
            await self._handle_mlx_stop({})
        result = await self._handle_mlx_start({"model": model})
        return result

    async def _handle_mlx_infer(self, params: dict) -> dict:
        messages = params.get("messages", [])
        model = params.get("model", "")
        temperature = params.get("temperature", 0.7)
        max_tokens = params.get("max_tokens", 2048)

        if not messages:
            return {"status": "error", "message": "messages parameter required"}

        healthy = await self._check_mlx_health()
        if not healthy:
            return {"status": "error", "message": "fusion-mlx not running or unreachable"}

        try:
            resp = await self._gateway.chat(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if resp.finish_reason == "error" and resp.usage.get("error"):
                return {"status": "error", "message": resp.usage["error"]}
            logger.info("mlx.infer completed, model=%s", resp.model or model)
            return {
                "status": "ok",
                "content": resp.content,
                "model": resp.model or model,
            }
        except Exception as e:
            logger.exception("mlx.infer failed")
            return {"status": "error", "message": str(e)}

    async def _handle_graph_list(self, params: dict) -> dict:
        graphs = self.store.list_graphs()
        enriched = []
        for g in graphs:
            entry = {
                "id": g["id"],
                "graph_id": g["id"],
                "name": g.get("name", ""),
                "description": g.get("description", ""),
                "node_count": g.get("node_count", 0),
                "edge_count": g.get("edge_count", 0),
                "nodes": {},
                "edges": [],
                "created_at": g.get("created_at", 0),
                "updated_at": g.get("updated_at", 0),
            }
            enriched.append(entry)
        return {"graphs": enriched}

    async def _handle_graph_create(self, params: dict) -> dict:
        name = params.get("name", "")
        description = params.get("description", "")
        graph_data = params.get("graph_data", {})

        if graph_data:
            graph = AgentGraph.from_dict(graph_data)
            if name:
                graph.name = name
            if description:
                graph.description = description
        else:
            graph = AgentGraph(name=name or "Untitled")
            graph.description = description

            nodes_data = params.get("nodes", [])
            for n in nodes_data:
                node_id = n.get("id", "")
                if not node_id:
                    continue
                node_config = NodeConfig(
                    type=n.get("type", "llm"),
                    label=n.get("label", ""),
                    model=n.get("model", ""),
                    system_prompt=n.get("system_prompt", ""),
                )
                graph.add_node(node_id, node_config)

            edges_data = params.get("edges", [])
            for e in edges_data:
                source_id = e.get("source_id", e.get("source", ""))
                target_id = e.get("target_id", e.get("target", ""))
                if source_id and target_id:
                    graph.add_edge(source_id, target_id, label=e.get("label", e.get("condition", "")))

        self.store.save_graph(graph)
        logger.info("Created graph %s: %s", graph.id, graph.name)

        return {
            "graph_id": graph.id,
            "name": graph.name,
            "description": graph.description,
            "nodes": {nid: n.to_dict() for nid, n in graph.nodes.items()},
            "edges": [e.to_dict() for e in graph.edges],
            "created_at": time.time(),
        }

    async def _handle_graph_get(self, params: dict) -> dict:
        graph_id = params.get("graph_id", "")
        graph = self.store.load_graph(graph_id)
        if graph is None:
            raise ValueError(f"Graph not found: {graph_id}")

        return {
            "graph_id": graph.id,
            "name": graph.name,
            "description": graph.description,
            "nodes": {nid: n.to_dict() for nid, n in graph.nodes.items()},
            "edges": [e.to_dict() for e in graph.edges],
            "start_node_id": graph.start_node_id,
            "version": graph.version,
        }

    async def _handle_graph_delete(self, params: dict) -> dict:
        graph_id = params.get("graph_id", "")
        deleted = self.store.delete_graph(graph_id)
        if not deleted:
            raise ValueError(f"Graph not found: {graph_id}")
        logger.info("Deleted graph %s", graph_id)
        return {"deleted": True}

    async def _handle_graph_execute(self, params: dict) -> dict:
        graph_id = params.get("graph_id", "")
        input_text = params.get("input", "")
        session_id = params.get("session_id", "")

        graph = self.store.load_graph(graph_id)
        if graph is None:
            raise ValueError(f"Graph not found: {graph_id}")

        rt = self._get_runtime()
        events = []

        async for event in rt.execute_graph(graph, input_text):
            ev_dict = event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
            events.append(ev_dict)

        logger.info("Graph %s executed: %d events", graph_id, len(events))
        return {
            "session_id": session_id or f"sess-{int(time.time())}",
            "events": events,
            "status": "completed",
        }

    async def _handle_graph_update(self, params: dict) -> dict:
        graph_id = params.get("graph_id", "")
        graph = self.store.load_graph(graph_id)
        if graph is None:
            raise ValueError(f"Graph not found: {graph_id}")

        if "name" in params:
            graph.name = params["name"]
        if "description" in params:
            graph.description = params["description"]

        nodes_data = params.get("nodes")
        if nodes_data is not None:
            graph.nodes.clear()
            graph.edges.clear()
            for n in nodes_data:
                nid = n.get("id", "")
                if not nid:
                    continue
                node_config = NodeConfig(
                    type=n.get("type", "llm"),
                    label=n.get("label", ""),
                    model=n.get("model", ""),
                    system_prompt=n.get("system_prompt", ""),
                )
                graph.add_node(nid, node_config)

        edges_data = params.get("edges")
        if edges_data is not None:
            graph.edges.clear()
            for e in edges_data:
                source_id = e.get("source_id", e.get("source", ""))
                target_id = e.get("target_id", e.get("target", ""))
                if source_id and target_id:
                    graph.add_edge(source_id, target_id, label=e.get("label", e.get("condition", "")))

        self.store.save_graph(graph)
        logger.info("Updated graph %s: %s", graph.id, graph.name)
        return {
            "graph_id": graph.id,
            "name": graph.name,
            "description": graph.description,
            "nodes": {nid: n.to_dict() for nid, n in graph.nodes.items()},
            "edges": [e.to_dict() for e in graph.edges],
        }

    def _get_tool_registry(self):
        from tools import create_default_registry
        if not hasattr(self, "_cached_tool_registry") or self._cached_tool_registry is None:
            self._cached_tool_registry = create_default_registry()
            logger.info("Cached default tool registry with %d tools", len(self._cached_tool_registry.tools))
        return self._cached_tool_registry

    async def _handle_tool_list(self, params: dict) -> dict:
        registry = self._get_tool_registry()
        tools = []
        for name, tool in registry.tools.items():
            schema = tool.get_schema()
            tools.append({
                "name": name,
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters", {}),
                "category": getattr(tool, "category", "built-in"),
                "enabled": True,
            })
        logger.info("Listed %d tools", len(tools))
        return {"tools": tools}

    async def _handle_tool_get(self, params: dict) -> dict:
        tool_name = params.get("name", "")
        registry = self._get_tool_registry()
        tool = registry.tools.get(tool_name)
        if tool is None:
            raise ValueError(f"Tool not found: {tool_name}")
        schema = tool.get_schema()
        return {
            "name": tool_name,
            "description": schema.get("description", ""),
            "parameters": schema.get("parameters", {}),
            "category": getattr(tool, "category", "built-in"),
            "enabled": True,
        }

    async def _handle_session_list(self, params: dict) -> dict:
        limit = params.get("limit", 50)
        sessions = self.store.list_sessions(limit=limit)
        return {"sessions": sessions}

        metrics: dict[str, Any] = {
            "platform": platform.system(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
        }

        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            metrics["memory_mb"] = round(usage.ru_maxrss / 1024 / 1024, 1)
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=2.0,
            )
            if result.returncode == 0:
                metrics["total_memory_gb"] = round(int(result.stdout.strip()) / 1024 / 1024 / 1024, 1)
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["vm_stat"],
                capture_output=True, text=True, timeout=2.0,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "Pages free" in line:
                        free_pages = int(line.split(":")[1].strip().rstrip("."))
                        metrics["free_memory_gb"] = round(free_pages * 16384 / 1024 / 1024 / 1024, 2)
        except Exception:
            pass

        metrics["mlx_running"] = (
            self._mlx_process is not None and self._mlx_process.poll() is None
        )

        return metrics

    async def _handle_knowledge_search(self, params: dict) -> dict:
        query = params.get("query", "")
        limit = params.get("limit", 5)
        try:
            from .knowledge_engine import KnowledgeEngine
            engine = KnowledgeEngine()
            results = engine.search(query, limit=limit)
            return {"results": [r.to_dict() for r in results]}
        except Exception as e:
            logger.warning("Knowledge search failed: %s", e)
            return {"results": [], "error": str(e)}

    async def _handle_knowledge_ingest(self, params: dict) -> dict:
        content = params.get("content", "")
        scope = params.get("scope", "default")
        metadata = params.get("metadata")
        if not content:
            return {"error": "content is required"}
        try:
            from .knowledge_engine import KnowledgeEngine
            engine = KnowledgeEngine()
            entry = engine.ingest(content, scope=scope, metadata=metadata)
            logger.info("knowledge.ingest: entry_id=%s scope=%s", entry.id, scope)
            return entry.to_dict()
        except Exception as e:
            logger.error("knowledge.ingest failed: %s", e)
            return {"error": str(e)}

    async def _handle_knowledge_delete(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"error": "entry_id is required"}
        try:
            from .knowledge_engine import KnowledgeEngine
            engine = KnowledgeEngine()
            ok = engine.delete(entry_id)
            logger.info("knowledge.delete: entry_id=%s ok=%s", entry_id, ok)
            return {"deleted": ok}
        except Exception as e:
            logger.error("knowledge.delete failed: %s", e)
            return {"error": str(e)}

    async def _handle_knowledge_list(self, params: dict) -> dict:
        scope = params.get("scope", "")
        limit = params.get("limit", 100)
        try:
            from .knowledge_engine import KnowledgeEngine
            engine = KnowledgeEngine()
            entries = engine.list_entries(scope=scope, limit=limit)
            return {"entries": [e.to_dict() for e in entries]}
        except Exception as e:
            logger.error("knowledge.list failed: %s", e)
            return {"entries": [], "error": str(e)}

    async def _handle_knowledge_count(self, params: dict) -> dict:
        scope = params.get("scope", "")
        try:
            from .knowledge_engine import KnowledgeEngine
            engine = KnowledgeEngine()
            n = engine.count(scope=scope)
            return {"count": n}
        except Exception as e:
            logger.error("knowledge.count failed: %s", e)
            return {"count": 0, "error": str(e)}

    async def _handle_env_health_check(self, params: dict) -> dict:
        checks: dict[str, Any] = {}

        checks["python"] = {
            "ok": True,
            "version": platform.python_version(),
        }

        mlx_running = self._mlx_process is not None and self._mlx_process.poll() is None
        checks["mlx_server"] = {
            "ok": mlx_running,
            "port": MLX_PORT,
        }

        mlx_reachable = await self._check_mlx_health()
        checks["mlx_api"] = {"ok": mlx_reachable}

        try:
            import httpx
            checks["httpx"] = {"ok": True}
        except ImportError:
            checks["httpx"] = {"ok": False, "message": "httpx not installed"}

        socket_ok = os.path.exists(self.socket_path)
        checks["daemon_socket"] = {"ok": socket_ok, "path": self.socket_path}

        model_dir = Path.home() / ".cache" / "huggingface"
        checks["model_cache"] = {
            "ok": model_dir.exists(),
            "path": str(model_dir),
        }

        all_ok = all(c.get("ok", False) for c in checks.values())
        return {"healthy": all_ok, "checks": checks}

    async def _handle_env_repair(self, params: dict) -> dict:
        item_id = params.get("item_id", "")
        logger.info("Repair requested for: %s", item_id)
        return {"repaired": False, "message": f"Repair not implemented for: {item_id}"}

    async def _handle_env_repair_all(self, params: dict) -> dict:
        logger.info("Repair all requested")
        return {"repaired": [], "message": "Repair all not yet implemented"}

    # ── Lazy engine accessors ──

    def _get_planner(self):
        if self._planner is None:
            from .planner import PlannerEngine
            self._planner = PlannerEngine(gateway=self._gateway)
            logger.info("PlannerEngine created (gateway=%s)", "enabled" if self._gateway._default_client else "stub")
        return self._planner

    def _get_memory(self):
        if self._memory is None:
            from .memory_engine import MemoryEngine
            self._memory = MemoryEngine(gateway=self._gateway)
            logger.info("MemoryEngine created at %s", self._memory.db_path)
        return self._memory

    def _get_safety(self):
        if self._safety is None:
            from .safety import SafetyGateway
            self._safety = SafetyGateway()
            logger.info("SafetyGuard created (L1)")
        return self._safety

    def _get_rag(self) -> RAGPipeline:
        if self._rag is None:
            try:
                from .knowledge_engine import KnowledgeEngine
                ke = KnowledgeEngine()
            except Exception:
                ke = None
                logger.warning("KnowledgeEngine unavailable, RAG will run without retrieval")
            self._rag = RAGPipeline(knowledge_engine=ke, gateway=self._gateway)
            logger.info("RAGPipeline created (knowledge=%s, gateway=%s)", "enabled" if ke else "none", "enabled" if self._gateway._default_client else "stub")
        return self._rag

    # ── Planner handlers ──

    async def _handle_planner_create_plan(self, params: dict) -> dict:
        task = params.get("task", "")
        if not task:
            return {"status": "error", "message": "task parameter required"}
        planner = self._get_planner()
        plan = await planner.create_plan(
            task=task,
            context=params.get("context", ""),
            files=params.get("files"),
        )
        logger.info("planner.create_plan: plan_id=%s steps=%d risk=%s", plan.id, len(plan.steps), plan.overall_risk)
        return {"plan": plan.to_dict()}

    async def _handle_planner_get_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._get_planner()
        plan = planner.get_plan(plan_id)
        if plan is None:
            return {"status": "error", "message": f"Plan not found: {plan_id}"}
        return {"plan": plan.to_dict()}

    async def _handle_planner_approve_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._get_planner()
        ok = planner.approve_plan(plan_id)
        return {"approved": ok}

    async def _handle_planner_reject_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        reason = params.get("reason", "")
        planner = self._get_planner()
        ok = planner.reject_plan(plan_id, reason=reason)
        return {"rejected": ok}

    async def _handle_planner_execute_step(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        step_id = params.get("step_id", "")
        planner = self._get_planner()
        try:
            step = planner.execute_step(plan_id, step_id)
            return {"step": step.to_dict()}
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    async def _handle_planner_execute_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._get_planner()
        try:
            plan = planner.execute_plan(plan_id)
            return {"plan": plan.to_dict()}
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    async def _handle_planner_list_plans(self, params: dict) -> dict:
        status = params.get("status", "")
        planner = self._get_planner()
        plans = planner.list_plans(status=status)
        return {"plans": [p.to_dict() for p in plans]}

    async def _handle_planner_cancel_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._get_planner()
        ok = planner.cancel_plan(plan_id)
        return {"cancelled": ok}

    # ── Verify handlers ──

    async def _handle_verify_verify(self, params: dict) -> dict:
        from .verifier import VerificationEngine
        task = params.get("task", "")
        output = params.get("output", "")
        criteria = params.get("criteria", "")
        context = params.get("context", "")
        max_attempts = params.get("max_attempts", 3)
        gateway = self._gateway
        engine = VerificationEngine(gateway=gateway, max_attempts=max_attempts)
        result = await engine.verify(task=task, output=output, criteria=criteria, context=context, max_attempts=max_attempts)
        return result.to_dict()

    # ── RAG handlers ──

    async def _handle_rag_query(self, params: dict) -> dict:
        query = params.get("query", "")
        if not query:
            return {"status": "error", "message": "query parameter required"}
        rag = self._get_rag()
        config_dict = params.get("config", {})
        config = RAGConfig.from_dict(config_dict) if config_dict else None
        result = await rag.query(
            query=query,
            config=config,
            model=params.get("model", ""),
            system_prompt=params.get("system_prompt", ""),
        )
        logger.info("rag.query: query=%r sources=%d", query[:50], len(result.get("sources", [])))
        return result

    async def _handle_rag_retrieve(self, params: dict) -> dict:
        query = params.get("query", "")
        if not query:
            return {"status": "error", "message": "query parameter required"}
        rag = self._get_rag()
        config_dict = params.get("config", {})
        config = RAGConfig.from_dict(config_dict) if config_dict else None
        rag_result = rag.retrieve(query, config=config)
        return {
            "query": rag_result.query,
            "context_text": rag_result.context_text,
            "documents": [
                {"id": d.id, "scope": d.scope, "content_preview": d.content[:200]}
                for d in rag_result.documents
            ],
            "metadata": rag_result.metadata,
        }

    def _get_vector_strategy(self, base_url: str = "http://localhost:8900"):
        from .rag_pipeline import VectorRetrievalStrategy
        if not hasattr(self, "_vector_strategy") or self._vector_strategy is None:
            self._vector_strategy = VectorRetrievalStrategy(base_url=base_url)
            logger.info("Created cached VectorRetrievalStrategy for %s", base_url)
        elif self._vector_strategy.base_url != base_url.rstrip("/"):
            logger.warning("VectorRetrievalStrategy base_url mismatch: cached=%s requested=%s, re-creating",
                           self._vector_strategy.base_url, base_url)
            self._vector_strategy = VectorRetrievalStrategy(base_url=base_url)
        return self._vector_strategy

    async def _handle_rag_vector_search(self, params: dict) -> dict:
        query = params.get("query", "")
        if not query:
            return {"status": "error", "message": "query parameter required"}
        base_url = params.get("base_url", "http://localhost:8900")
        strategy = self._get_vector_strategy(base_url)
        available = await strategy.is_available()
        if not available:
            return {"status": "error", "message": f"fusion-kb not reachable at {base_url}"}
        top_k = params.get("top_k", 5)
        scope = params.get("scope", "")
        entries = await strategy.search(query, top_k=top_k, scope=scope)
        return {
            "query": query,
            "results": [
                {"id": e.id, "content": e.content[:500], "scope": e.scope, "source": e.source}
                for e in entries
            ],
            "count": len(entries),
        }

    # ── Cron handlers ──

    def _get_cron_manager(self):
        from .triggers import CronManager, CronJob
        if not hasattr(self, "_cron_manager") or self._cron_manager is None:
            import os
            db_path = os.path.expanduser("~/.fusion-agent-studio/cron.db")
            self._cron_manager = CronManager(db_path=db_path)
        return self._cron_manager

    async def _handle_cron_register(self, params: dict) -> dict:
        from .triggers import CronJob
        cm = self._get_cron_manager()
        job_id = params.get("id", f"cron_{int(time.time())}")
        job = CronJob(
            id=job_id,
            name=params.get("name", ""),
            expression=params.get("expression", "* * * * *"),
            graph_id=params.get("graph_id", ""),
            enabled=params.get("enabled", True),
            input_data=params.get("input_data", ""),
            max_retries=params.get("max_retries", 0),
        )
        await cm.aregister(job)
        return {"status": "ok", "job": job.to_dict()}

    async def _handle_cron_unregister(self, params: dict) -> dict:
        job_id = params.get("id", "")
        if not job_id:
            return {"status": "error", "message": "id parameter required"}
        cm = self._get_cron_manager()
        await cm.aunregister(job_id)
        return {"status": "ok", "unregistered": job_id}

    async def _handle_cron_list(self, params: dict) -> dict:
        cm = self._get_cron_manager()
        return {"jobs": cm.list()}

    async def _handle_cron_list_executions(self, params: dict) -> dict:
        cm = self._get_cron_manager()
        job_id = params.get("job_id", "")
        limit = params.get("limit", 20)
        return {"executions": await cm.alist_executions(job_id=job_id, limit=limit)}

    # ── Dynamic tool handlers ──

    _SAFE_TOOL_NAME_RE = __import__("re").compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

    async def _handle_tool_dynamic_register(self, params: dict) -> dict:
        from tools import ToolRegistry
        if not hasattr(self, "_dynamic_registry"):
            self._dynamic_registry = ToolRegistry()
        name = params.get("name", "")
        if not name:
            return {"status": "error", "message": "name parameter required"}
        if not self._SAFE_TOOL_NAME_RE.match(name):
            return {"status": "error", "message": f"invalid tool name '{name}'"}
        tool_type = params.get("type", "terminal")
        description = params.get("description", "")
        tool_params = params.get("parameters", {})

        from tools.base import BaseTool
        from types import new_class

        param_dict = {}
        if isinstance(tool_params, dict):
            for pk, pv in tool_params.items():
                param_dict[pk] = pv if isinstance(pv, dict) else {"type": "string", "description": str(pv)}

        safe_name = f"Dynamic_{self._SAFE_TOOL_NAME_RE.match(name).group()}"
        dyn_cls = new_class(safe_name, (BaseTool,), {})
        dyn_cls.name = name
        dyn_cls.description = description or f"Dynamic tool: {name}"
        dyn_cls.parameters = param_dict
        async def _exec(self_inner, **kw):
            import asyncio
            import shlex
            cmd = kw.get("command", kw.get("url", kw.get("query", "")))
            if cmd:
                try:
                    split_args = shlex.split(str(cmd))
                except ValueError:
                    return f"Error: invalid command: {str(cmd)[:100]}"
                if not split_args:
                    return "Error: empty command"
                proc = await asyncio.create_subprocess_exec(
                    split_args[0], *split_args[1:],
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
                result = out.decode("utf-8", errors="replace").strip()
                if err:
                    result += f"\n[STDERR] {err.decode('utf-8', errors='replace')}"
                return result or "Done"
            return "No command"
        dyn_cls.execute = _exec
        new_tool = dyn_cls()

        self._dynamic_registry.register(new_tool)
        logger.info("Dynamic tool registered via daemon: %s", name)
        return {"status": "ok", "tool": name}

    async def _handle_tool_dynamic_unregister(self, params: dict) -> dict:
        name = params.get("name", "")
        if not name:
            return {"status": "error", "message": "name parameter required"}
        if hasattr(self, "_dynamic_registry") and self._dynamic_registry.has(name):
            self._dynamic_registry.unregister(name)
            return {"status": "ok", "unregistered": name}
        return {"status": "error", "message": f"Tool '{name}' not found in dynamic registry"}

    # ── Memory handlers ──

    async def _handle_memory_store(self, params: dict) -> dict:
        content = params.get("content", "")
        if not content:
            return {"status": "error", "message": "content parameter required"}
        mem = self._get_memory()
        entry_id = mem.store(
            content=content,
            scope=params.get("scope", "default"),
            tags=params.get("tags", ""),
            importance=params.get("importance", 5),
            metadata=params.get("metadata"),
            tier=params.get("tier", ""),
        )
        logger.info("memory.store: entry_id=%s scope=%s", entry_id, params.get("scope", "default"))
        return {"entry_id": entry_id}

    async def _handle_memory_recall(self, params: dict) -> dict:
        query = params.get("query", "")
        mem = self._get_memory()
        entries = mem.recall(
            query=query,
            scope=params.get("scope", ""),
            limit=params.get("limit", 10),
            min_importance=params.get("min_importance", 0),
            tier=params.get("tier", ""),
        )
        return {"entries": [e.to_dict() for e in entries]}

    async def _handle_memory_list_recent(self, params: dict) -> dict:
        mem = self._get_memory()
        entries = mem.list_recent(
            scope=params.get("scope", ""),
            limit=params.get("limit", 20),
            min_importance=params.get("min_importance", 0),
            tier=params.get("tier", ""),
        )
        return {"entries": [e.to_dict() for e in entries]}

    async def _handle_memory_get(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        mem = self._get_memory()
        entry = mem.get(entry_id)
        if entry is None:
            return {"status": "error", "message": f"Entry not found: {entry_id}"}
        return {"entry": entry.to_dict()}

    async def _handle_memory_delete(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        mem = self._get_memory()
        deleted = mem.delete(entry_id)
        return {"deleted": deleted}

    async def _handle_memory_delete_scope(self, params: dict) -> dict:
        scope = params.get("scope", "")
        if not scope:
            return {"status": "error", "message": "scope parameter required"}
        mem = self._get_memory()
        count = mem.delete_scope(scope)
        return {"deleted_count": count}

    async def _handle_memory_count(self, params: dict) -> dict:
        mem = self._get_memory()
        count = mem.count(scope=params.get("scope", ""), tier=params.get("tier", ""))
        return {"count": count}

    async def _handle_memory_recall_relevant(self, params: dict) -> dict:
        mem = self._get_memory()
        query = params.get("query", "")
        limit = params.get("limit", 5)
        scope = params.get("scope", "")
        result = mem.recall_relevant(query=query, limit=limit, scope=scope)
        return {"context": result}

    async def _handle_memory_auto_forget(self, params: dict) -> dict:
        mem = self._get_memory()
        max_entries = params.get("max_entries", 1000)
        min_importance = params.get("min_importance", 3)
        removed = mem.auto_forget(max_entries=max_entries, min_importance=min_importance)
        return {"removed": removed}

    # ── Safety handlers ──

    async def _handle_safety_check(self, params: dict) -> dict:
        content = params.get("content", "")
        if not content:
            return {"status": "error", "message": "content parameter required"}
        guard = self._get_safety()
        verdict = guard.check(content, context=params.get("context", ""))
        return {"verdict": verdict.to_dict()}

    async def _handle_safety_evaluate_action(self, params: dict) -> dict:
        category = params.get("category", "")
        if not category:
            return {"status": "error", "message": "category parameter required"}
        guard = self._get_safety()
        verdict = guard.evaluate_action(
            category=category,
            content=params.get("content", ""),
            context=params.get("context", ""),
        )
        return {"verdict": verdict.to_dict()}

    async def _handle_safety_approve_action(self, params: dict) -> dict:
        action_id = params.get("action_id", "")
        if not action_id:
            return {"status": "error", "message": "action_id parameter required"}
        guard = self._get_safety()
        ok = guard.approve_action(action_id)
        return {"approved": ok}

    async def _handle_safety_reject_action(self, params: dict) -> dict:
        action_id = params.get("action_id", "")
        if not action_id:
            return {"status": "error", "message": "action_id parameter required"}
        guard = self._get_safety()
        ok = guard.reject_action(action_id)
        return {"rejected": ok}

    async def _handle_safety_get_pending_actions(self, params: dict) -> dict:
        guard = self._get_safety()
        actions = guard.get_pending_actions()
        return {"actions": actions}

    async def _handle_safety_add_policy(self, params: dict) -> dict:
        from .safety import SafetyLevel, SafetyPolicy
        category = params.get("category", "")
        if not category:
            return {"status": "error", "message": "category parameter required"}
        level_str = params.get("default_level", "L2")
        try:
            level = SafetyLevel(level_str)
        except ValueError:
            level = SafetyLevel.L2
        policy = SafetyPolicy(
            category=category,
            description=params.get("description", ""),
            default_level=level,
            requires_diff=params.get("requires_diff", False),
        )
        guard = self._get_safety()
        guard.add_policy(policy)
        logger.info("safety.add_policy: category=%s level=%s", category, level_str)
        return {"added": True, "category": category}

    # ── Template handlers ──

    async def _handle_template_list(self, params: dict) -> dict:
        from .agent_templates import list_templates
        category = params.get("category", "")
        templates = list_templates(category=category)
        return {"templates": [t.to_dict() for t in templates]}

    async def _handle_template_get(self, params: dict) -> dict:
        from .agent_templates import get_template
        template_id = params.get("template_id", "")
        tmpl = get_template(template_id)
        if tmpl is None:
            return {"status": "error", "message": f"Template not found: {template_id}"}
        return {"template": tmpl.to_dict()}

    async def _handle_template_instantiate(self, params: dict) -> dict:
        from .agent_templates import instantiate_template
        template_id = params.get("template_id", "")
        if not template_id:
            return {"status": "error", "message": "template_id parameter required"}
        graph_data = instantiate_template(template_id, variables=params.get("variables"))
        if not graph_data:
            return {"status": "error", "message": f"Template not found: {template_id}"}
        return {"graph_data": graph_data}

    # ── Deploy handlers ──

    async def _handle_deploy_export(self, params: dict) -> dict:
        from .deployer import GraphDeployer
        graph_id = params.get("graph_id", "")
        fmt = params.get("format", "json")
        filepath = params.get("filepath", "")

        graph = self.store.load_graph(graph_id)
        if graph is None:
            return {"status": "error", "message": f"Graph not found: {graph_id}"}
        if not filepath:
            import tempfile
            ext = {"json": ".json", "python": ".py", "yaml": ".yaml", "fastapi": ".py"}.get(fmt, ".json")
            filepath = str(Path(tempfile.gettempdir()) / f"{graph.name}{ext}")

        try:
            if fmt == "json":
                path = GraphDeployer.export_as_json(graph, filepath)
            elif fmt == "python":
                path = GraphDeployer.export_as_python(graph, filepath, with_server=params.get("with_server", True))
            elif fmt == "yaml":
                path = GraphDeployer.export_as_yaml(graph, filepath)
            elif fmt == "fastapi":
                path = GraphDeployer.export_as_fastapi(graph, filepath, port=params.get("port", 8000))
            else:
                return {"status": "error", "message": f"Unknown format: {fmt}"}
            logger.info("deploy.export: graph=%s format=%s path=%s", graph_id, fmt, path)
            return {"status": "ok", "path": str(path), "format": fmt}
        except Exception as e:
            logger.exception("deploy.export failed")
            return {"status": "error", "message": str(e)}

    async def _handle_deploy_import(self, params: dict) -> dict:
        from .deployer import GraphDeployer
        filepath = params.get("filepath", "")
        if not filepath:
            return {"status": "error", "message": "filepath parameter required"}
        try:
            graph = GraphDeployer.import_from_json(filepath)
            self.store.save_graph(graph)
            logger.info("deploy.import: path=%s graph_id=%s", filepath, graph.id)
            return {
                "graph_id": graph.id,
                "name": graph.name,
                "nodes": len(graph.nodes),
                "edges": len(graph.edges),
            }
        except FileNotFoundError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            logger.exception("deploy.import failed")
            return {"status": "error", "message": str(e)}

    async def _handle_deploy_list_formats(self, params: dict) -> dict:
        from .deployer import GraphDeployer
        formats = GraphDeployer.list_formats()
        return {"formats": formats}

    # ── Agent & Marketplace lazy accessors ──

    def _get_marketplace(self):
        if self._marketplace is None:
            from .agent_marketplace import AgentMarketplace
            self._marketplace = AgentMarketplace()
            logger.info("AgentMarketplace created at %s", self._marketplace.store_dir)
        return self._marketplace

    def _agent_dir(self, agent_id: str) -> Path:
        return Path.home() / ".fusion-agent-studio" / "agents" / agent_id

    def _persist_agents_index(self):
        idx_path = Path.home() / ".fusion-agent-studio" / "agents" / "index.json"
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(self._agents, f, indent=4, ensure_ascii=False)
        logger.debug("Persisted agents index: %d agents", len(self._agents))

    def _load_agents_index(self):
        idx_path = Path.home() / ".fusion-agent-studio" / "agents" / "index.json"
        if idx_path.exists() and not self._agents:
            with open(idx_path, "r", encoding="utf-8") as f:
                self._agents = json.load(f)
            logger.info("Loaded agents index: %d agents", len(self._agents))

    # ── Agent handlers ──

    async def _handle_agent_create(self, params: dict) -> dict:
        import uuid
        name = params.get("name", "")
        if not name:
            return {"status": "error", "message": "name parameter required"}

        self._load_agents_index()
        agent_id = params.get("id", uuid.uuid4().hex[:12])

        from .agent_package import AgentPackage, AgentManifest
        agent_dir = self._agent_dir(agent_id)
        manifest = AgentManifest(
            name=name,
            model=params.get("model", ""),
            system_prompt=params.get("system_prompt", f"You are {name}."),
            temperature=params.get("temperature", 0.7),
            max_tokens=params.get("max_tokens", 4096),
            tools=params.get("tools", []),
            capabilities=params.get("capabilities", []),
            safety_level=params.get("safety_level", "L1"),
            tags=params.get("tags", []),
            author=params.get("author", ""),
            description=params.get("description", ""),
            status=params.get("status", "draft"),
            version_int=params.get("version_int", 1),
            published_at=params.get("published_at"),
            knowledge_base_ids=params.get("knowledge_base_ids", []),
            visibility=params.get("visibility", "private"),
            rag_strategy=params.get("rag_strategy", "hybrid"),
            web_search_enabled=params.get("web_search_enabled", False),
            deep_research_enabled=params.get("deep_research_enabled", False),
            connector_ids=params.get("connector_ids", []),
            style=params.get("style", ""),
            top_p=params.get("top_p", 1.0),
            context_window=params.get("context_window", 32768),
            rate_limit_qps=params.get("rate_limit_qps", 0),
        )
        pkg = AgentPackage(agent_dir)
        pkg.init(manifest=manifest, soul=params.get("soul", ""), memory=params.get("memory", ""), agents_md=params.get("agents_md", ""))

        self._agents[agent_id] = manifest.to_dict()
        self._agents[agent_id]["id"] = agent_id
        self._agents[agent_id]["created_at"] = time.time()
        self._persist_agents_index()

        logger.info("agent.create: id=%s name=%s", agent_id, name)
        return {"agent_id": agent_id, "manifest": manifest.to_dict()}

    async def _handle_agent_get(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        manifest = pkg.load_manifest()
        result = manifest.to_dict()
        result["id"] = agent_id
        result["skills"] = pkg.list_skills()
        result["has_soul"] = bool(pkg.load_soul().strip())
        return {"agent": result}

    async def _handle_agent_list(self, params: dict) -> dict:
        self._load_agents_index()
        tag_filter = params.get("tags", [])
        capability_filter = params.get("capabilities", [])

        results = []
        for aid, meta in self._agents.items():
            if tag_filter:
                agent_tags = meta.get("tags", [])
                if not any(t in agent_tags for t in tag_filter):
                    continue
            if capability_filter:
                agent_caps = meta.get("capabilities", [])
                if not any(c in agent_caps for c in capability_filter):
                    continue
            entry = dict(meta)
            entry["id"] = aid
            results.append(entry)

        return {"agents": results}

    async def _handle_agent_update(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from .agent_package import AgentPackage, AgentManifest
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        manifest = pkg.load_manifest()
        for key in ("name", "model", "system_prompt", "temperature", "max_tokens",
                     "safety_level", "description", "author", "version",
                     "status", "version_int", "published_at",
                     "knowledge_base_ids", "visibility", "rag_strategy",
                     "web_search_enabled", "deep_research_enabled",
                     "connector_ids", "style", "top_p", "context_window",
                     "rate_limit_qps"):
            if key in params:
                setattr(manifest, key, params[key])
        if "tools" in params:
            manifest.tools = params["tools"]
        if "capabilities" in params:
            manifest.capabilities = params["capabilities"]
        if "tags" in params:
            manifest.tags = params["tags"]

        pkg.save_manifest(manifest)

        self._load_agents_index()
        if agent_id in self._agents:
            self._agents[agent_id].update(manifest.to_dict())
            self._persist_agents_index()

        logger.info("agent.update: id=%s", agent_id)
        return {"updated": True, "manifest": manifest.to_dict()}

    async def _handle_agent_delete(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if pkg.exists:
            pkg.destroy()

        self._load_agents_index()
        removed = self._agents.pop(agent_id, None)
        if removed is not None:
            self._persist_agents_index()

        logger.info("agent.delete: id=%s existed=%s", agent_id, pkg.exists or removed is not None)
        return {"deleted": True}

    async def _handle_agent_configure(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from .agent_package import AgentPackage, AgentManifest
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        config = params.get("config", {})
        if not config:
            return {"status": "error", "message": "config parameter required"}

        manifest = pkg.load_manifest()
        if "model" in config:
            manifest.model = config["model"]
        if "temperature" in config:
            manifest.temperature = config["temperature"]
        if "max_tokens" in config:
            manifest.max_tokens = config["max_tokens"]
        if "system_prompt" in config:
            manifest.system_prompt = config["system_prompt"]
        if "tools" in config:
            manifest.tools = config["tools"]
        if "capabilities" in config:
            manifest.capabilities = config["capabilities"]
        if "safety_level" in config:
            manifest.safety_level = config["safety_level"]

        pkg.save_manifest(manifest)

        self._load_agents_index()
        if agent_id in self._agents:
            self._agents[agent_id].update(manifest.to_dict())
            self._persist_agents_index()

        logger.info("agent.configure: id=%s keys=%s", agent_id, list(config.keys()))
        return {"configured": True, "manifest": manifest.to_dict()}

    async def _handle_agent_execute(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        input_text = params.get("input", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        graph_config = pkg.to_graph_config()
        manifest = pkg.load_manifest()

        effective_prompt = graph_config.get("system_prompt", manifest.system_prompt)

        if manifest.knowledge_base_ids:
            kb_context = await self._inject_knowledge_context(manifest.knowledge_base_ids, input_text, manifest.rag_strategy)
            if kb_context:
                effective_prompt = f"{effective_prompt}\n\n{kb_context}"

        if manifest.style:
            style_mgr = self._get_style_manager()
            style_result = style_mgr.apply(effective_prompt, manifest.style)
            if "system_prompt" in style_result:
                effective_prompt = style_result["system_prompt"]

        graph = AgentGraph(name=manifest.name or agent_id)
        graph.description = manifest.description

        start_id = "start"
        llm_id = "llm-1"
        start_node = NodeConfig(type="start", label="Start")
        llm_node = NodeConfig(
            type="llm",
            label="Agent LLM",
            model=manifest.model,
            system_prompt=effective_prompt,
        )
        graph.add_node(start_id, start_node)
        graph.add_node(llm_id, llm_node)
        graph.add_edge(start_id, llm_id)

        for i, tool_name in enumerate(manifest.tools):
            tool_id = f"tool-{i+1}"
            tool_node = NodeConfig(type="tool", label=tool_name)
            graph.add_node(tool_id, tool_node)
            if i == 0:
                graph.add_edge(llm_id, tool_id)
            else:
                graph.add_edge(f"tool-{i}", tool_id)

        self.store.save_graph(graph)
        rt = self._get_runtime()

        events = []
        try:
            async for event in rt.execute_graph(graph, input_text):
                ev_dict = event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
                events.append(ev_dict)
        except Exception as e:
            logger.warning("agent.execute runtime error: %s", e)
            return {
                "agent_id": agent_id,
                "events": events,
                "status": "error",
                "message": str(e),
                "session_id": f"sess-{int(time.time())}",
            }

        logger.info("agent.execute: id=%s events=%d", agent_id, len(events))
        return {
            "agent_id": agent_id,
            "events": events,
            "status": "completed",
            "session_id": f"sess-{int(time.time())}",
        }

    async def _handle_agent_list_skills(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        skills = pkg.list_skills()
        return {"skills": skills}

    async def _handle_agent_add_skill(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        skill_name = params.get("skill_name", "")
        if not agent_id or not skill_name:
            return {"status": "error", "message": "agent_id and skill_name required"}

        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        skill_def = params.get("skill_def", {})
        pkg.save_skill(skill_name, skill_def)
        logger.info("agent.add_skill: agent=%s skill=%s", agent_id, skill_name)
        return {"added": True, "skill_name": skill_name}

    async def _handle_agent_delete_skill(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        skill_name = params.get("skill_name", "")
        if not agent_id or not skill_name:
            return {"status": "error", "message": "agent_id and skill_name required"}

        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        deleted = pkg.delete_skill(skill_name)
        return {"deleted": deleted}

    async def _handle_agent_get_soul(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        return {"soul": pkg.load_soul()}

    async def _handle_agent_update_soul(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        soul = params.get("soul", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        pkg.save_soul(soul)
        logger.info("agent.update_soul: agent=%s len=%d", agent_id, len(soul))
        return {"updated": True}

    # ── Agent task routing handlers ──

    async def _handle_agent_submit_code_task(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        code = params.get("code", "")
        language = params.get("language", "python")
        timeout = params.get("timeout", 60)
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        if not code:
            return {"status": "error", "message": "code parameter required"}

        import uuid
        task_id = params.get("task_id") or str(uuid.uuid4())
        task = {
            "task_id": task_id,
            "agent_id": agent_id,
            "code": code,
            "language": language,
            "timeout": timeout,
            "status": "pending",
            "result": None,
            "error": None,
            "created_at": __import__("time").time(),
        }
        self._code_tasks[task_id] = task
        logger.info("agent.submit_code_task: task=%s agent=%s", task_id, agent_id)

        try:
            task["status"] = "running"
            import asyncio

            async def _run():
                try:
                    result = await self._execute_code_task(task)
                    task["status"] = "completed"
                    task["result"] = result
                except asyncio.CancelledError:
                    task["status"] = "cancelled"
                except Exception as exc:
                    task["status"] = "failed"
                    task["error"] = str(exc)
                    logger.error("agent task %s failed: %s", task_id, exc)

            handle = asyncio.ensure_future(_run())
            task["_handle"] = handle
            await asyncio.sleep(0)
        except Exception as exc:
            task["status"] = "failed"
            task["error"] = str(exc)
            logger.error("agent.submit_code_task: task=%s error=%s", task_id, exc)

        return {
            "task_id": task_id,
            "status": task["status"],
        }

    async def _execute_code_task(self, task: dict):
        agent_id = task["agent_id"]
        code = task["code"]
        language = task["language"]
        timeout = task.get("timeout", 60)
        logger.info("_execute_code_task: task=%s lang=%s timeout=%s", task["task_id"], language, timeout)
        if language != "python":
            return {"output": f"Unsupported language: {language}", "exit_code": 1}

        try:
            sandbox = CodeSandbox(timeout=timeout, use_sandbox=True)
            result = await asyncio.to_thread(sandbox.execute, code, language)
            output = result.stdout
            if result.stderr:
                output = (output + "\n" + result.stderr) if output else result.stderr
            if result.timed_out:
                output = (output + "\nExecution timed out") if output else "Execution timed out"
            logger.info(
                "_execute_code_task done: task=%s exit=%s success=%s exec_id=%s",
                task["task_id"], result.exit_code, result.success, result.execution_id,
            )
            return {"output": output, "exit_code": result.exit_code}
        except Exception as exc:
            logger.error("_execute_code_task error: task=%s error=%s", task["task_id"], exc)
            return {"output": str(exc), "exit_code": 1}

    async def _handle_agent_task_status(self, params: dict) -> dict:
        task_id = params.get("task_id", "")
        if not task_id:
            return {"status": "error", "message": "task_id parameter required"}
        task = self._code_tasks.get(task_id)
        if not task:
            return {"status": "error", "message": f"Task not found: {task_id}"}
        return {
            "task_id": task_id,
            "status": task["status"],
            "result": task.get("result"),
            "error": task.get("error"),
        }

    async def _handle_agent_cancel_task(self, params: dict) -> dict:
        task_id = params.get("task_id", "")
        if not task_id:
            return {"status": "error", "message": "task_id parameter required"}
        task = self._code_tasks.get(task_id)
        if not task:
            return {"status": "error", "message": f"Task not found: {task_id}"}
        handle = task.get("_handle")
        if handle and not handle.done():
            handle.cancel()
            task["status"] = "cancelled"
            logger.info("agent.cancel_task: task=%s cancelled", task_id)
        return {"task_id": task_id, "status": task["status"]}

    async def _handle_agent_tasks(self, params: dict) -> dict:
        agent_id = params.get("agent_id")
        status_filter = params.get("status")
        tasks = list(self._code_tasks.values())
        if agent_id:
            tasks = [t for t in tasks if t["agent_id"] == agent_id]
        if status_filter:
            tasks = [t for t in tasks if t["status"] == status_filter]
        items = []
        for t in tasks:
            items.append({
                "task_id": t["task_id"],
                "agent_id": t["agent_id"],
                "status": t["status"],
                "language": t["language"],
                "created_at": t["created_at"],
                "error": t.get("error"),
            })
        return {"tasks": items}

    async def _inject_knowledge_context(self, knowledge_base_ids: list[str], query: str, strategy: str = "hybrid") -> str:
        if not knowledge_base_ids or not query:
            return ""
        try:
            from .knowledge_engine import KnowledgeEngine
            ke = KnowledgeEngine()
            all_contexts = []
            for kb_id in knowledge_base_ids:
                results = ke.search(query, mode=strategy, scope=kb_id)
                for r in results[:5]:
                    content = r.get("content", "") if isinstance(r, dict) else str(r)
                    if content:
                        all_contexts.append(content)
            if all_contexts:
                return f"[Knowledge Base Context]\n{'—'.join(all_contexts[:10])}\n[/Knowledge Base Context]"
        except Exception as exc:
            logger.warning("Knowledge injection failed: %s", exc)
        return ""

    async def _handle_agent_publish(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        manifest = pkg.load_manifest()
        if manifest.status == "published":
            return {"status": "error", "message": "Agent already published"}
        manifest.status = "published"
        manifest.version_int = manifest.version_int + 1
        manifest.published_at = time.time()
        pkg.save_manifest(manifest)
        self._load_agents_index()
        if agent_id in self._agents:
            self._agents[agent_id].update(manifest.to_dict())
            self._persist_agents_index()
        endpoint = f"http://localhost:{MLX_PORT}/v1/agents/{agent_id}/chat"
        logger.info("agent.publish: id=%s version=%d", agent_id, manifest.version_int)
        return {"agent_id": agent_id, "status": "published", "version": manifest.version_int, "published_at": manifest.published_at, "api_endpoint": endpoint}

    async def _handle_agent_archive(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        manifest = pkg.load_manifest()
        manifest.status = "archived"
        pkg.save_manifest(manifest)
        self._load_agents_index()
        if agent_id in self._agents:
            self._agents[agent_id].update(manifest.to_dict())
            self._persist_agents_index()
        logger.info("agent.archive: id=%s", agent_id)
        return {"agent_id": agent_id, "status": "archived"}

    async def _handle_agent_clone(self, params: dict) -> dict:
        import uuid
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from .agent_package import AgentPackage
        src_dir = self._agent_dir(agent_id)
        src_pkg = AgentPackage(src_dir)
        if not src_pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        manifest = src_pkg.load_manifest()
        cloned_id = uuid.uuid4().hex[:12]
        cloned_name = params.get("name", f"{manifest.name} (copy)")
        manifest.name = cloned_name
        manifest.status = "draft"
        manifest.version_int = 1
        manifest.published_at = None
        dest_dir = self._agent_dir(cloned_id)
        dest_pkg = AgentPackage(dest_dir)
        dest_pkg.init(manifest=manifest, soul=src_pkg.load_soul(), memory=src_pkg.load_memory(), agents_md=src_pkg.load_agents())
        self._load_agents_index()
        self._agents[cloned_id] = manifest.to_dict()
        self._agents[cloned_id]["id"] = cloned_id
        self._agents[cloned_id]["created_at"] = time.time()
        self._persist_agents_index()
        logger.info("agent.clone: src=%s cloned=%s", agent_id, cloned_id)
        return {"agent_id": cloned_id, "manifest": manifest.to_dict()}

    async def _handle_agent_get_api_endpoint(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        manifest = pkg.load_manifest()
        endpoint = f"http://localhost:{MLX_PORT}/v1/agents/{agent_id}/chat"
        logger.info("agent.get_api_endpoint: id=%s endpoint=%s", agent_id, endpoint)
        return {"agent_id": agent_id, "endpoint": endpoint, "status": manifest.status}

    async def _handle_agent_execute_stream(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        input_text = params.get("input", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        graph_config = pkg.to_graph_config()
        manifest = pkg.load_manifest()

        effective_prompt = graph_config.get("system_prompt", manifest.system_prompt)

        if manifest.knowledge_base_ids:
            kb_context = await self._inject_knowledge_context(manifest.knowledge_base_ids, input_text, manifest.rag_strategy)
            if kb_context:
                effective_prompt = f"{effective_prompt}\n\n{kb_context}"

        if manifest.style:
            style_mgr = self._get_style_manager()
            style_result = style_mgr.apply(effective_prompt, manifest.style)
            if "system_prompt" in style_result:
                effective_prompt = style_result["system_prompt"]

        graph = AgentGraph(name=manifest.name or agent_id)
        graph.description = manifest.description

        start_id = "start"
        llm_id = "llm-1"
        start_node = NodeConfig(type="start", label="Start")
        llm_node = NodeConfig(
            type="llm",
            label="Agent LLM",
            model=manifest.model,
            system_prompt=effective_prompt,
        )
        graph.add_node(start_id, start_node)
        graph.add_node(llm_id, llm_node)
        graph.add_edge(start_id, llm_id)

        for i, tool_name in enumerate(manifest.tools):
            tool_id = f"tool-{i+1}"
            tool_node = NodeConfig(type="tool", label=tool_name)
            graph.add_node(tool_id, tool_node)
            if i == 0:
                graph.add_edge(llm_id, tool_id)
            else:
                graph.add_edge(f"tool-{i}", tool_id)

        self.store.save_graph(graph)
        rt = self._get_runtime()

        events = []
        execution_id = f"exec-{int(time.time())}-{agent_id}"
        tool_calls_log = []
        knowledge_retrieved = []
        total_input_tokens = 0
        total_output_tokens = 0
        try:
            async for event in rt.execute_graph(graph, input_text):
                ev_dict = event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
                events.append(ev_dict)
                ev_type = ev_dict.get("type", "")
                if ev_type == "TOOL_CALL":
                    tool_calls_log.append(ev_dict)
                if ev_type == "TOOL_RESULT":
                    tool_calls_log.append(ev_dict)
                if "token" in str(ev_type).lower():
                    total_input_tokens += ev_dict.get("input_tokens", 0)
                    total_output_tokens += ev_dict.get("output_tokens", 0)
        except Exception as e:
            logger.warning("agent.execute_stream runtime error: %s", e)
            return {
                "execution_id": execution_id,
                "agent_id": agent_id,
                "events": events,
                "status": "error",
                "message": str(e),
                "tool_calls": tool_calls_log,
                "knowledge_retrieved": knowledge_retrieved,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }

        logger.info("agent.execute_stream: id=%s events=%d tools=%d", agent_id, len(events), len(tool_calls_log))
        return {
            "execution_id": execution_id,
            "agent_id": agent_id,
            "events": events,
            "status": "completed",
            "tool_calls": tool_calls_log,
            "knowledge_retrieved": knowledge_retrieved,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "duration_ms": int((events[-1].get("timestamp", 0) - events[0].get("timestamp", 0)) * 1000) if len(events) > 1 else 0,
        }

    # ── Connector handlers ──

    async def _handle_connector_list(self, params: dict) -> dict:
        mgr = self._get_connector_manager()
        return {"connectors": mgr.list_connectors()}

    async def _handle_connector_create(self, params: dict) -> dict:
        mgr = self._get_connector_manager()
        name = params.get("name", "")
        if not name:
            return {"status": "error", "message": "name parameter required"}
        return mgr.create(name, params.get("type", "api_key"), params.get("auth_config", {}))

    async def _handle_connector_get(self, params: dict) -> dict:
        mgr = self._get_connector_manager()
        connector_id = params.get("connector_id", "")
        result = mgr.get(connector_id)
        if result is None:
            return {"status": "error", "message": f"Connector not found: {connector_id}"}
        return {"connector": result}

    async def _handle_connector_update(self, params: dict) -> dict:
        mgr = self._get_connector_manager()
        connector_id = params.get("connector_id", "")
        return mgr.update(connector_id, params.get("updates", {}))

    async def _handle_connector_delete(self, params: dict) -> dict:
        mgr = self._get_connector_manager()
        connector_id = params.get("connector_id", "")
        return mgr.delete(connector_id)

    async def _handle_connector_connect(self, params: dict) -> dict:
        mgr = self._get_connector_manager()
        connector_id = params.get("connector_id", "")
        return mgr.connect(connector_id)

    async def _handle_connector_disconnect(self, params: dict) -> dict:
        mgr = self._get_connector_manager()
        connector_id = params.get("connector_id", "")
        return mgr.disconnect(connector_id)

    async def _handle_connector_test(self, params: dict) -> dict:
        mgr = self._get_connector_manager()
        connector_id = params.get("connector_id", "")
        return mgr.test(connector_id)

    # ── Dashboard handler ──

    async def _handle_dashboard_overview(self, params: dict) -> dict:
        self._load_agents_index()
        total_agents = len(self._agents)
        published_agents = sum(1 for m in self._agents.values() if m.get("status") == "published")
        active_agents = sum(1 for m in self._agents.values() if m.get("status") in ("draft", "published"))

        today_requests = 0
        total_tokens = 0
        error_count = 0
        try:
            sessions = self.store.list_sessions()
            now = time.time()
            day_ago = now - 86400
            for s in sessions:
                ts = s.get("timestamp", 0) if isinstance(s, dict) else 0
                if ts > day_ago:
                    today_requests += 1
                if isinstance(s, dict) and s.get("status") == "error":
                    error_count += 1
        except Exception as exc:
            logger.warning("dashboard.overview session query failed: %s", exc)

        try:
            from .metrics_engine import MetricsEngine
            me = MetricsEngine()
            summary = me.get_summary()
            total_tokens = summary.total_tokens_in + summary.total_tokens_out
        except Exception as exc:
            logger.warning("dashboard.overview metrics query failed: %s", exc)

        alerts = []
        try:
            budget_handler = self._handle_budget_status({})
            budget_data = budget_handler if isinstance(budget_handler, dict) else {}
            warn_pct = budget_data.get("warn_percent", 0)
            if warn_pct > 80:
                alerts.append({"level": "warning", "message": f"Token budget usage at {warn_pct}%", "type": "budget"})
        except Exception:
            pass

        recent_agents = []
        sorted_agents = sorted(self._agents.items(), key=lambda x: x[1].get("created_at", 0), reverse=True)[:5]
        for aid, meta in sorted_agents:
            recent_agents.append({"id": aid, "name": meta.get("name", ""), "status": meta.get("status", "draft")})

        logger.info("dashboard.overview: agents=%d requests=%d tokens=%d errors=%d", total_agents, today_requests, total_tokens, error_count)
        return {
            "total_agents": total_agents,
            "published_agents": published_agents,
            "active_agents": active_agents,
            "today_requests": today_requests,
            "total_tokens": total_tokens,
            "error_count": error_count,
            "alerts": alerts,
            "recent_agents": recent_agents,
        }

    # ── API Key handlers ──

    async def _handle_apikey_create(self, params: dict) -> dict:
        mgr = self._get_apikey_manager()
        name = params.get("name", "")
        if not name:
            return {"status": "error", "message": "name parameter required"}
        return mgr.create(
            name=name,
            permissions=params.get("permissions"),
            allowed_agent_ids=params.get("allowed_agent_ids"),
            ip_whitelist=params.get("ip_whitelist"),
            expires_at=params.get("expires_at"),
        )

    async def _handle_apikey_list(self, params: dict) -> dict:
        mgr = self._get_apikey_manager()
        return {"keys": mgr.list_keys()}

    async def _handle_apikey_revoke(self, params: dict) -> dict:
        mgr = self._get_apikey_manager()
        key_id = params.get("key_id", "")
        return mgr.revoke(key_id)

    async def _handle_apikey_rotate(self, params: dict) -> dict:
        mgr = self._get_apikey_manager()
        key_id = params.get("key_id", "")
        return mgr.rotate(key_id)

    async def _handle_apikey_update(self, params: dict) -> dict:
        mgr = self._get_apikey_manager()
        key_id = params.get("key_id", "")
        return mgr.update(key_id, params.get("updates", {}))

    # ── Analytics handler ──

    async def _handle_analytics_agent_usage(self, params: dict) -> dict:
        agent_id = params.get("agent_id")
        time_range = params.get("time_range", "day")
        now = time.time()
        range_seconds = {"day": 86400, "week": 604800, "month": 2592000}.get(time_range, 86400)
        cutoff = now - range_seconds

        agents_usage = []
        try:
            from .metrics_engine import MetricsEngine
            me = MetricsEngine()
            sessions = me.query_sessions()
            agent_buckets: dict[str, dict] = {}
            for s in sessions:
                ts = s.timestamp if hasattr(s, "timestamp") else s.get("timestamp", 0)
                if ts < cutoff:
                    continue
                gid = s.graph_id if hasattr(s, "graph_id") else s.get("graph_id", "unknown")
                aid = gid if agent_id is None else agent_id
                if agent_id and gid != agent_id:
                    continue
                bucket = agent_buckets.setdefault(aid, {"agent_id": aid, "requests": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0})
                bucket["requests"] += 1
                if hasattr(s, "error") and s.error:
                    bucket["errors"] += 1
                elif isinstance(s, dict) and s.get("error"):
                    bucket["errors"] += 1
            agents_usage = list(agent_buckets.values())
        except Exception as exc:
            logger.warning("analytics.agent_usage failed: %s", exc)

        logger.info("analytics.agent_usage: range=%s agents=%d", time_range, len(agents_usage))
        return {"agents": agents_usage, "time_range": time_range}

    # ── Style handlers ──

    async def _handle_style_list(self, params: dict) -> dict:
        mgr = self._get_style_manager()
        return {"styles": mgr.list_styles()}

    async def _handle_style_get(self, params: dict) -> dict:
        mgr = self._get_style_manager()
        style_id = params.get("style_id", "")
        result = mgr.get(style_id)
        if result is None:
            return {"status": "error", "message": f"Style not found: {style_id}"}
        return {"style": result}

    async def _handle_style_create(self, params: dict) -> dict:
        mgr = self._get_style_manager()
        name = params.get("name", "")
        if not name:
            return {"status": "error", "message": "name parameter required"}
        return mgr.create(name, params.get("suffix", ""), params.get("output_format", "markdown"))

    async def _handle_style_apply(self, params: dict) -> dict:
        mgr = self._get_style_manager()
        style_id = params.get("style_id", "")
        system_prompt = params.get("system_prompt", "")
        return mgr.apply(system_prompt, style_id)

    # ── Alert handlers ──

    async def _handle_alert_list(self, params: dict) -> dict:
        alerts = []
        try:
            budget_handler = self._handle_budget_status({})
            budget_data = budget_handler if isinstance(budget_handler, dict) else {}
            warn_pct = budget_data.get("warn_percent", 0)
            if warn_pct > 80:
                alerts.append({"id": "budget-warning", "level": "warning", "message": f"Token budget usage at {warn_pct}%", "type": "budget", "acknowledged": False})
            if warn_pct > 95:
                alerts.append({"id": "budget-critical", "level": "critical", "message": f"Token budget nearly exhausted ({warn_pct}%)", "type": "budget", "acknowledged": False})
        except Exception:
            pass
        try:
            sessions = self.store.list_sessions()
            now = time.time()
            for s in sessions[-20:]:
                if isinstance(s, dict) and s.get("status") == "error":
                    alerts.append({"id": f"session-error-{s.get('session_id', '')}", "level": "error", "message": f"Session error: {s.get('error', 'unknown')}", "type": "session", "acknowledged": False})
        except Exception:
            pass
        return {"alerts": alerts}

    async def _handle_alert_acknowledge(self, params: dict) -> dict:
        alert_id = params.get("alert_id", "")
        logger.info("alert.acknowledge: id=%s", alert_id)
        return {"acknowledged": True, "alert_id": alert_id}

    # ── Marketplace handlers ──

    async def _handle_marketplace_search(self, params: dict) -> dict:
        mp = self._get_marketplace()
        results = mp.search(
            query=params.get("query", ""),
            category=params.get("category", ""),
            tags=params.get("tags"),
            sort_by=params.get("sort_by", "name"),
            limit=params.get("limit", 50),
        )
        return {"entries": [e.to_dict() for e in results]}

    async def _handle_marketplace_get(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._get_marketplace()
        entry = mp.get(entry_id)
        if entry is None:
            return {"status": "error", "message": f"Entry not found: {entry_id}"}
        return {"entry": entry.to_dict()}

    async def _handle_marketplace_publish(self, params: dict) -> dict:
        from .agent_marketplace import MarketEntry
        mp = self._get_marketplace()
        entry = MarketEntry(
            name=params.get("name", ""),
            author=params.get("author", ""),
            description=params.get("description", ""),
            category=params.get("category", ""),
            tags=params.get("tags", []),
            version=params.get("version", "1.0.0"),
            graph_data=params.get("graph_data", {}),
        )
        entry_id = mp.publish(entry)
        logger.info("marketplace.publish: id=%s name=%s", entry_id, entry.name)
        return {"entry_id": entry_id}

    async def _handle_marketplace_unpublish(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._get_marketplace()
        ok = mp.unpublish(entry_id)
        return {"unpublished": ok}

    async def _handle_marketplace_list_categories(self, params: dict) -> dict:
        mp = self._get_marketplace()
        return {"categories": mp.list_categories()}

    async def _handle_marketplace_install(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._get_marketplace()
        result = mp.install(entry_id, target_dir=params.get("target_dir"))
        if result is None:
            return {"status": "error", "message": f"Install failed for: {entry_id}"}
        logger.info("marketplace.install: id=%s path=%s", entry_id, result)
        return {"installed": True, "path": str(result)}

    async def _handle_marketplace_uninstall(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._get_marketplace()
        entry = mp.get(entry_id)
        if not entry:
            return {"success": False, "message": f"Entry not found: {entry_id}"}
        ok = mp.unpublish(entry_id)
        logger.info("marketplace.uninstall: id=%s success=%s", entry_id, ok)
        return {"success": ok}

    # ── Chat Session Handlers ──

    async def _handle_chat_create(self, params: dict) -> dict:
        engine = self._get_chat_engine()
        session = engine.create_session(
            mode=params.get("mode", "simple"),
            title=params.get("title", ""),
            graph_id=params.get("graph_id", ""),
            metadata=params.get("metadata"),
        )
        logger.info("chat.create: id=%s mode=%s", session.id, session.mode)
        return session.to_dict()

    async def _handle_chat_get(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        engine = self._get_chat_engine()
        session = engine.get_session(session_id)
        if session is None:
            return {"status": "error", "message": f"Session {session_id} not found"}
        return session.to_dict()

    async def _handle_chat_list(self, params: dict) -> dict:
        engine = self._get_chat_engine()
        sessions = engine.list_sessions()
        return {"sessions": [s.to_dict() for s in sessions]}

    async def _handle_chat_delete(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        engine = self._get_chat_engine()
        deleted = engine.delete_session(session_id)
        return {"deleted": deleted}

    async def _handle_chat_send(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        message = params.get("message", "")
        content = params.get("content")
        mode = params.get("mode", "")
        engine = self._get_chat_engine()

        if content and isinstance(content, list):
            has_image = any(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in content
            )
            if has_image:
                vision_models = {"llava", "qwen-vl", "phi-vision", "cogvlm", "internvl"}
                model = getattr(engine, "_model", "") or ""
                is_vision = any(vm in model.lower() for vm in vision_models)
                if not is_vision:
                    return {
                        "status": "error",
                        "message": "Image input requires a vision model (e.g., llava, qwen-vl). "
                                   f"Current model: {model or 'unknown'}",
                        "code": 422,
                    }

        events = []
        full_content = ""
        async for ev in engine.send(session_id, message, mode=mode, content=content):
            ev_dict = ev.to_dict()
            events.append(ev_dict)
            if ev.type.value == "token":
                full_content += ev.content
            await self._broadcast_event("chat_event", {
                "session_id": session_id,
                "event": ev_dict,
            })

        logger.info("chat.send: session=%s events=%d content_len=%d multimodal=%s",
                     session_id, len(events), len(full_content), bool(content))
        return {"events": events, "content": full_content}

    async def _handle_chat_branch(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        message_id = params.get("message_id", "")
        engine = self._get_chat_engine()
        branched = engine.branch(session_id, message_id)
        if branched is None:
            return {"status": "error", "message": "Branch failed"}
        return branched.to_dict()

    async def _handle_chat_edit(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        message_id = params.get("message_id", "")
        new_content = params.get("content", "")
        engine = self._get_chat_engine()
        edited = engine.edit(session_id, message_id, new_content)
        if edited is None:
            return {"status": "error", "message": "Edit failed"}
        return edited.to_dict()

    async def _handle_chat_switch_branch(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        message_id = params.get("message_id", "")
        engine = self._get_chat_engine()
        ok = engine.switch_branch(session_id, message_id)
        return {"status": "ok" if ok else "error", "session_id": session_id, "active_branch": message_id}

    async def _handle_chat_branches(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        message_id = params.get("message_id", "")
        engine = self._get_chat_engine()
        branches = engine.get_branches(session_id, message_id)
        return {"branches": branches}

    async def _handle_chat_message_tree(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        engine = self._get_chat_engine()
        tree = engine.get_message_tree(session_id)
        return tree

    async def _handle_budget_set(self, params: dict) -> dict:
        from .token_budget import TokenBudget
        max_tokens = params.get("max_tokens", 0)
        budget = TokenBudget(max_tokens=max_tokens)
        self._token_budget = budget
        logger.info("Token budget set: max_tokens=%d", max_tokens)
        return budget.status()

    async def _handle_budget_status(self, params: dict) -> dict:
        if not hasattr(self, "_token_budget") or not self._token_budget:
            return {"max_tokens": 0, "spent_tokens": 0, "exceeded": False}
        return self._token_budget.status()

    async def _handle_safety_approve(self, params: dict) -> dict:
        action_id = params.get("action_id", "")
        if self._runtime and hasattr(self._runtime, "approve_action"):
            ok = self._runtime.approve_action(action_id)
            return {"status": "ok" if ok else "not_found", "action_id": action_id}
        return {"status": "error", "message": "No runtime available"}

    async def _handle_safety_reject(self, params: dict) -> dict:
        action_id = params.get("action_id", "")
        if self._runtime and hasattr(self._runtime, "reject_action"):
            ok = self._runtime.reject_action(action_id)
            return {"status": "ok" if ok else "not_found", "action_id": action_id}
        return {"status": "error", "message": "No runtime available"}

    # ── WebSocket Streaming ──

    async def _handle_ws_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._ws_clients.append(writer)
        peer = writer.get_extra_info("peername")
        logger.info("WS client connected: %s", peer)
        try:
            while self._running:
                data = await reader.readline()
                if not data:
                    break
                try:
                    msg = json.loads(data.decode().strip())
                    await self._handle_ws_message(writer, msg)
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
        finally:
            if writer in self._ws_clients:
                self._ws_clients.remove(writer)
            writer.close()
            logger.info("WS client disconnected: %s", peer)

    async def _handle_ws_message(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        action = msg.get("action", "")
        if action == "chat.stream":
            session_id = msg.get("session_id", "")
            message = msg.get("message", "")
            mode = msg.get("mode", "")
            engine = self._get_chat_engine()
            async for ev in engine.send(session_id, message, mode=mode):
                payload = json.dumps({
                    "type": "chat_event",
                    "session_id": session_id,
                    "event": ev.to_dict(),
                }) + "\n"
                writer.write(payload.encode())
                await writer.drain()
            done_payload = json.dumps({
                "type": "chat_done",
                "session_id": session_id,
            }) + "\n"
            writer.write(done_payload.encode())
            await writer.drain()
        elif action == "subscribe":
            writer.write((json.dumps({"type": "subscribed"}) + "\n").encode())
            await writer.drain()

    async def _broadcast_event(self, event_type: str, data: dict) -> None:
        if not self._ws_clients:
            return
        payload = json.dumps({"type": event_type, **data}) + "\n"
        encoded = payload.encode()

        async def _send(client):
            try:
                client.write(encoded)
                await client.drain()
                return None
            except Exception:
                return client

        results = await asyncio.gather(*[_send(c) for c in self._ws_clients])
        dead = [r for r in results if r is not None]
        for d in dead:
            if d in self._ws_clients:
                self._ws_clients.remove(d)

    # ── MLX helpers ──

    async def _check_mlx_health(self) -> bool:
        try:
            import httpx
            # 携带 fusion-mlx 配置的 api_key，否则开启鉴权时 /models 返回 401
            # 被误判为不健康 (bug6 一直显示检测中)
            key = self._read_mlx_api_key()
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{MLX_BASE_URL}/models", headers=headers)
                return resp.status_code == 200
        except Exception:
            return False

    async def _list_mlx_models(self) -> list[dict[str, Any]]:
        try:
            import httpx
            key = self._read_mlx_api_key()
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{MLX_BASE_URL}/models", headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", [])
        except Exception:
            return []

    async def _wait_mlx_healthy(self, timeout: float = 30.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if await self._check_mlx_health():
                return True
            await asyncio.sleep(0.5)
        return False

    def _attach_mlx_client(self) -> None:
        from server.fusion_mlx_client import FusionMLXClient
        api_key = self._read_mlx_api_key()
        client = FusionMLXClient(base_url=MLX_BASE_URL, api_key=api_key)
        self._gateway.set_default_client(client)
        loaded = self._discover_mlx_model_id(api_key)
        if loaded:
            self._gateway._default_model = loaded
        logger.info(
            "MLX client attached to gateway (api_key=%s, default_model=%s)",
            "set" if api_key else "none", self._gateway._default_model,
        )

    def _read_mlx_api_key(self) -> str:
        # 读取 fusion-mlx 配置的 api_key，避免硬编码 (bug1 联动)。
        # 优先级对齐 fusion-mlx server.py::_resolve_api_key：
        #   环境变量 FUSION_MLX_API_KEY > settings.json 的 auth.api_key > 顶层 api_key。
        env_key = os.environ.get("FUSION_MLX_API_KEY")
        if env_key:
            return env_key
        candidates = [
            os.path.expanduser("~/.fusion-mlx/settings.json"),
            os.path.expanduser("~/Library/Application Support/fusion-mlx/settings.json"),
        ]
        for path in candidates:
            try:
                with open(path) as f:
                    data = json.load(f)
                # 实际密钥存放在嵌套的 auth.api_key (顶层 api_key 通常为空)
                key = (data.get("auth") or {}).get("api_key") or data.get("api_key")
                if key:
                    return key
            except Exception as exc:
                logger.debug("read mlx api_key from %s failed: %s", path, exc)
                continue
        return ""

    def _discover_mlx_model_id(self, api_key: str) -> str:
        # 查询已运行 fusion-mlx 的模型目录，作为 gateway 默认模型 (bug1 联动)。
        # /v1/models 返回目录内全部模型，含图像/视频/编码器组件，
        # 需过滤出对话模型并优先 Qwen3 9B 级 (对齐 bug1 默认 Qwen3.6-9B-4bit)。
        try:
            import urllib.request
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            req = urllib.request.Request(f"{MLX_BASE_URL}/models", headers=headers)
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read())
            models = data.get("data", []) if isinstance(data, dict) else []
            ids = [m.get("id", "") for m in models if m.get("id")]
            excluded = (
                "flux", "vae", "transformer", "text_encoder", "siglip",
                "oldt5", "wan", "skyreels", "ltx", "tts",
            )
            chat_ids = [i for i in ids if not any(x in i.lower() for x in excluded)]
            if not chat_ids:
                return ids[0] if ids else ""
            preferred = (
                "Qwen3.6-9B-4bit", "Qwen3.5-9B-4bit", "Qwen3.6-27B-mxfp8",
                "Qwen3.6-27B-mixed_3_4", "Qwen3.6-27B-bf16",
            )
            for want in preferred:
                for cid in chat_ids:
                    if cid == want:
                        logger.info("select mlx default model (preferred): %s", cid)
                        return cid
            for cid in chat_ids:
                if "qwen" in cid.lower():
                    logger.info("select mlx default model (qwen fallback): %s", cid)
                    return cid
            logger.info("select mlx default model (first chat): %s", chat_ids[0])
            return chat_ids[0]
        except Exception as exc:
            logger.warning("discover mlx model id failed: %s", exc)
        return ""

    def _detach_mlx_client(self) -> None:
        self._gateway._default_client = None
        logger.info("MLX client detached from gateway")


def run_daemon(socket_path: str = SOCKET_PATH):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    daemon = DaemonServer(socket_path=socket_path)
    asyncio.run(daemon.run_forever())


    async def _handle_skill_execute(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        skill_name = params.get("skill_name", "")
        user_input = params.get("input", "")
        tool_names = params.get("tools", [])
        if not agent_id or not skill_name:
            return {"status": "error", "message": "agent_id and skill_name required"}

        from .agent_package import AgentPackage
        agent_dir = self._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        skills = pkg.list_skills()
        skill_def = None
        for s in skills:
            if s.get("name") == skill_name:
                skill_def = s
                break
        if skill_def is None:
            return {"status": "error", "message": f"Skill not found: {skill_name}"}

        system_prompt = skill_def.get("system_prompt", skill_def.get("systemPrompt", ""))
        steps = skill_def.get("steps", [])
        results = []
        chat_engine = self._get_chat_engine()
        session_id = f"skill-{agent_id}-{skill_name}-{id(params):012x}"

        if steps:
            step_results = []
            for i, step in enumerate(steps):
                step_prompt = step.get("prompt", "")
                action = step.get("action", "generate")
                step_input = step_prompt.replace("{input}", user_input) if step_prompt else user_input
                if step_results:
                    step_input += "\n\nPrevious step results:\n" + "\n".join(
                        f"[Step {j+1}]: {r}" for j, r in enumerate(step_results)
                    )

                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": step_input})

                try:
                    response_text = ""
                    async for ev in chat_engine.send(session_id, step_input, mode="skill"):
                        if ev.type.value == "token":
                            response_text += ev.content
                    step_results.append(response_text[:4000])
                    results.append({
                        "step": i + 1,
                        "name": step.get("name", f"Step {i+1}"),
                        "action": action,
                        "status": "completed",
                        "output_length": len(response_text),
                    })
                    logger.info("skill.execute: step %d/%d completed, %d chars", i+1, len(steps), len(response_text))
                except Exception as e:
                    step_results.append(f"Error: {e}")
                    results.append({
                        "step": i + 1,
                        "name": step.get("name", f"Step {i+1}"),
                        "action": action,
                        "status": "error",
                        "error": str(e),
                    })
                    break

            final_result = step_results[-1] if step_results else ""
        else:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_input})

            try:
                final_result = ""
                async for ev in chat_engine.send(session_id, user_input, mode="skill"):
                    if ev.type.value == "token":
                        final_result += ev.content
                results.append({"step": 1, "name": skill_name, "action": "generate", "status": "completed", "output_length": len(final_result)})
            except Exception as e:
                final_result = f"Error: {e}"
                results.append({"step": 1, "name": skill_name, "action": "generate", "status": "error", "error": str(e)})

        logger.info("skill.execute: agent=%s skill=%s steps=%d", agent_id, skill_name, len(results))
        return {"steps": results, "result": final_result, "skill_name": skill_name}

    async def _handle_research_adaptive(self, params: dict) -> dict:
        question = params.get("question", "")
        max_steps = min(params.get("max_steps", 10), 20)
        web_search = params.get("web_search", True)
        if not question:
            return {"status": "error", "message": "question parameter required"}

        chat_engine = self._get_chat_engine()
        session_id = f"research-adaptive-{id(params):012x}"
        findings = []
        citations = []
        steps_taken = 0

        decompose_prompt = (
            f"Break down the following question into 2-4 key sub-questions that need to be researched. "
            f"Output each sub-question on a separate line, prefixed with '## Sub-question N:'.\n\n"
            f"Question: {question}"
        )
        try:
            decomp_text = ""
            async for ev in chat_engine.send(session_id, decompose_prompt, mode="research"):
                if ev.type.value == "token":
                    decomp_text += ev.content
            steps_taken += 1
            import re
            sub_questions = re.findall(r"## Sub-question \d+:\s*(.+)", decomp_text)
            if not sub_questions:
                sub_questions = [line.strip() for line in decomp_text.split("\n") if line.strip() and not line.startswith("#")][:4]
            if not sub_questions:
                sub_questions = [question]
            findings.append({"step": "decompose", "sub_questions": sub_questions, "raw": decomp_text[:2000]})
            logger.info("research.adaptive: decomposed into %d sub-questions", len(sub_questions))
        except Exception as e:
            findings.append({"step": "decompose", "error": str(e)})
            sub_questions = [question]

        for sq in sub_questions:
            if steps_taken >= max_steps:
                break
            search_prompt = (
                f"Research this sub-question thoroughly. Provide specific facts, data, and cite sources.\n\n"
                f"Sub-question: {sq}\n\n"
                f"Original question: {question}"
            )
            try:
                search_text = ""
                async for ev in chat_engine.send(session_id, search_prompt, mode="research"):
                    if ev.type.value == "token":
                        search_text += ev.content
                steps_taken += 1
                findings.append({"step": "search", "sub_question": sq, "result": search_text[:4000]})
                url_pattern = re.findall(r'https?://[^\s)\]<>"]+', search_text)
                for url in url_pattern[:3]:
                    citations.append({"url": url, "context": sq})
            except Exception as e:
                findings.append({"step": "search", "sub_question": sq, "error": str(e)})

        sufficient = False
        sufficiency_prompt = (
            f"Given the following research findings, determine if they sufficiently answer the original question. "
            f"Respond with ONLY 'SUFFICIENT' or 'INSUFFICIENT' followed by a brief reason.\n\n"
            f"Original question: {question}\n\n"
            f"Findings so far:\n" + "\n".join(
                f"- {f.get('sub_question', f.get('step', ''))}: {f.get('result', f.get('raw', ''))[:500]}"
                for f in findings if 'error' not in f
            )
        )
        try:
            suff_text = ""
            async for ev in chat_engine.send(session_id, sufficiency_prompt, mode="research"):
                if ev.type.value == "token":
                    suff_text += ev.content
            sufficient = "SUFFICIENT" in suff_text.upper()
            steps_taken += 1
        except Exception:
            sufficient = True

        if not sufficient and steps_taken < max_steps:
            extra_prompt = (
                f"The previous research was deemed insufficient. Provide additional information "
                f"to fully answer the question.\n\n"
                f"Original question: {question}\n\n"
                f"What's missing or needs more depth?"
            )
            try:
                extra_text = ""
                async for ev in chat_engine.send(session_id, extra_prompt, mode="research"):
                    if ev.type.value == "token":
                        extra_text += ev.content
                steps_taken += 1
                findings.append({"step": "supplement", "result": extra_text[:4000]})
            except Exception as e:
                findings.append({"step": "supplement", "error": str(e)})

        synthesize_prompt = (
            f"Synthesize all the research findings into a comprehensive, well-structured response. "
            f"Include specific facts and cite sources where possible.\n\n"
            f"Original question: {question}\n\n"
            f"Research findings:\n" + "\n\n".join(
                f"[{f.get('step', 'step')}] {f.get('sub_question', '')}\n{f.get('result', f.get('raw', ''))[:2000]}"
                for f in findings if 'error' not in f
            )
        )
        try:
            final_answer = ""
            async for ev in chat_engine.send(session_id, synthesize_prompt, mode="research"):
                if ev.type.value == "token":
                    final_answer += ev.content
            steps_taken += 1
        except Exception as e:
            final_answer = f"Synthesis error: {e}"

        logger.info("research.adaptive: question=%s steps=%d sufficient=%s citations=%d",
                     question[:50], steps_taken, sufficient, len(citations))
        return {
            "answer": final_answer,
            "citations": citations,
            "steps_taken": steps_taken,
            "sufficient": sufficient,
            "findings": findings,
        }


if __name__ == "__main__":
    run_daemon()
