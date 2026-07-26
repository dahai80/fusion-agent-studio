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

logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/fusion-studio.sock"
MLX_PORT = 11434
MLX_BASE_URL = f"http://127.0.0.1:{MLX_PORT}/v1"


class DaemonServer:
    def __init__(self, socket_path: str = SOCKET_PATH):
        self.socket_path = socket_path
        self.store = AgentStore()
        self._gateway = LLMGateway()
        self._runtime: AgentRuntime | None = None
        self._mlx_process: subprocess.Popen | None = None
        self._active_executions: dict[str, asyncio.Task] = {}
        self._server: asyncio.Server | None = None
        self._running = False
        self._planner = None
        self._memory = None
        self._safety = None
        self._rag: RAGPipeline | None = None
        self._agents: dict[str, dict] = {}
        self._marketplace = None

    def _get_runtime(self) -> AgentRuntime:
        if self._runtime is None:
            from tools import create_default_registry
            registry = create_default_registry()
            self._runtime = AgentRuntime(llm_gateway=self._gateway, tool_registry=registry)
            logger.info("AgentRuntime created with %d tools", len(registry._tools))
        return self._runtime

    async def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self.socket_path
        )
        os.chmod(self.socket_path, 0o666)
        self._running = True
        logger.info("Daemon listening on %s", self.socket_path)

    async def stop(self) -> None:
        self._running = False
        for task in self._active_executions.values():
            if not task.done():
                task.cancel()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
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
            "hardware.metrics": self._handle_hardware_metrics,
            "knowledge.search": self._handle_knowledge_search,
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
            "rag.query": self._handle_rag_query,
            "rag.retrieve": self._handle_rag_retrieve,
            "memory.store": self._handle_memory_store,
            "memory.recall": self._handle_memory_recall,
            "memory.list_recent": self._handle_memory_list_recent,
            "memory.get": self._handle_memory_get,
            "memory.delete": self._handle_memory_delete,
            "memory.delete_scope": self._handle_memory_delete_scope,
            "memory.count": self._handle_memory_count,
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
            "agent.get_soul": self._handle_agent_get_soul,
            "agent.update_soul": self._handle_agent_update_soul,
            "marketplace.search": self._handle_marketplace_search,
            "marketplace.get": self._handle_marketplace_get,
            "marketplace.publish": self._handle_marketplace_publish,
            "marketplace.unpublish": self._handle_marketplace_unpublish,
            "marketplace.list_categories": self._handle_marketplace_list_categories,
            "marketplace.install": self._handle_marketplace_install,
        }
        return handlers.get(method)

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
        return {"graphs": graphs}

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

    async def _handle_hardware_metrics(self, params: dict) -> dict:
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
        )
        pkg = AgentPackage(agent_dir)
        pkg.init(manifest=manifest, soul=params.get("soul", ""))

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
                     "safety_level", "description", "author", "version"):
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

        graph = AgentGraph(name=manifest.name or agent_id)
        graph.description = manifest.description

        start_id = "start"
        llm_id = "llm-1"
        start_node = NodeConfig(type="start", label="Start")
        llm_node = NodeConfig(
            type="llm",
            label="Agent LLM",
            model=manifest.model,
            system_prompt=graph_config.get("system_prompt", manifest.system_prompt),
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

    # ── MLX helpers ──

    async def _check_mlx_health(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{MLX_BASE_URL}/models")
                return resp.status_code == 200
        except Exception:
            return False

    async def _list_mlx_models(self) -> list[dict[str, Any]]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{MLX_BASE_URL}/models")
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
        client = FusionMLXClient(base_url=MLX_BASE_URL)
        self._gateway.set_default_client(client)
        logger.info("MLX client attached to gateway")

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


if __name__ == "__main__":
    run_daemon()
