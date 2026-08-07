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
import base64
import hashlib
import json
import logging
import os
import platform
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .graph import AgentGraph, NodeConfig
from .llm_gateway import LLMGateway
from .persistence import AgentStore
from .rag_pipeline import RAGPipeline
from .runtime import AgentRuntime
from .chat_engine import ChatEngine
from .code_sandbox import CodeSandbox

logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/fusion-studio.sock"
WS_PORT = 11435
WS_MAGIC = "258EAFA5-E914-47DA-95CA-5AB5DC65B283"


async def _ws_read_frame(reader: asyncio.StreamReader) -> str | None:
    header = await reader.readexactly(2)
    opcode = header[0] & 0x0F
    masked = (header[1] & 0x80) != 0
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    mask_key = None
    if masked:
        mask_key = await reader.readexactly(4)
    payload = await reader.readexactly(length)
    if mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    if opcode == 0x8:
        return None
    return payload.decode("utf-8", errors="replace")


def _ws_write_frame(writer: asyncio.StreamWriter, data: str) -> None:
    payload = data.encode("utf-8")
    length = len(payload)
    frame = bytearray([0x81])
    if length <= 125:
        frame.append(length)
    elif length <= 65535:
        frame.append(126)
        frame.extend(struct.pack(">H", length))
    else:
        frame.append(127)
        frame.extend(struct.pack(">Q", length))
    frame.extend(payload)
    writer.write(bytes(frame))
# NetLayer 方案B: 默认经 fusion-gateway :11432 调用 fusion-mlx，不直连 :11434。
# 保留 FUSION_GATEWAY_URL / FUSION_MLX_PORT 显式覆盖 (可回退直连 11434)。
_MLX_PORT_DEFAULT = int(os.environ.get("FUSION_MLX_PORT", "11432"))
MLX_PORT = _MLX_PORT_DEFAULT
MLX_BASE_URL = os.environ.get(
    "FUSION_GATEWAY_URL",
    f"http://127.0.0.1:{MLX_PORT}/v1",
)


class DaemonServer:
    def __init__(
        self,
        socket_path: str = SOCKET_PATH,
        ws_port: int = WS_PORT,
        cluster_port: int = 11457,
        http_port: int = 11455,
    ):
        self.socket_path = socket_path
        self.ws_port = ws_port
        self.cluster_port = cluster_port
        self.http_port = http_port
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
        self._workflow_engine = None
        self._session_manager = None
        self._telemetry_engine = None
        self._status_tracker = None
        self._cowork_manager = None
        self._langgraph_engine = None
        self._artifact_manager = None
        self._kb_manager = None
        self._audit_logger = None
        self._version_store = None
        self._offline_mode = False

        self._sub_dispatchers = self._init_sub_dispatchers()

    def __getattr__(self, name: str):
        if name.startswith("_handle_"):
            for sd in self.__dict__.get("_sub_dispatchers", []):
                handlers = sd.get_handlers()
                for rpc, handler in handlers.items():
                    if handler.__name__ == name:
                        return handler
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _get_runtime(self) -> AgentRuntime:
        if self._runtime is None:
            from tools import create_default_registry

            registry = create_default_registry()
            self._runtime = AgentRuntime(
                llm_gateway=self._gateway, tool_registry=registry
            )
            logger.info("AgentRuntime created with %d tools", len(registry._tools))
        return self._runtime

    def _get_chat_engine(self) -> ChatEngine:
        if self._chat_engine is None:
            self._chat_engine = ChatEngine(
                runtime=self._get_runtime(), store=self.store
            )
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

    def _get_workflow_engine(self):
        if self._workflow_engine is None:
            from .workflow_engine import WorkflowEngine

            self._workflow_engine = WorkflowEngine(
                llm_gateway=self._gateway,
                tool_registry=self._get_runtime()._tool_registry
                if self._runtime
                else None,
                orchestrator=self._get_orchestrator(),
            )
            logger.info("WorkflowEngine created")
        return self._workflow_engine

    def _get_session_manager(self):
        if self._session_manager is None:
            from .session_manager import SessionManager

            self._session_manager = SessionManager(
                runtime=self._get_runtime(),
                gateway=self._gateway,
                store=self.store,
            )
            logger.info("SessionManager created")
        return self._session_manager

    def _get_telemetry_engine(self):
        if self._telemetry_engine is None:
            from .telemetry import TelemetryEngine

            self._telemetry_engine = TelemetryEngine()
            logger.info("TelemetryEngine created")
        return self._telemetry_engine

    def _get_status_tracker(self):
        if self._status_tracker is None:
            from .agent_api import AgentStatusTracker

            self._status_tracker = AgentStatusTracker()
            logger.info("AgentStatusTracker created")
        return self._status_tracker

    def _get_cowork_manager(self):
        if self._cowork_manager is None:
            from .cowork_manager import CoworkManager

            self._cowork_manager = CoworkManager()
            logger.info("CoworkManager created")
        return self._cowork_manager

    def _get_langgraph_engine(self):
        if self._langgraph_engine is None:
            from .langgraph_engine import LangGraphEngine

            self._langgraph_engine = LangGraphEngine()
            logger.info("LangGraphEngine created")
        return self._langgraph_engine

    def _get_artifact_manager(self):
        if self._artifact_manager is None:
            from .artifact_bridge import ArtifactBridge

            self._artifact_manager = ArtifactBridge()
            logger.info("ArtifactBridge created (local + remote RPC)")
        return self._artifact_manager

    def _get_kb_manager(self):
        if self._kb_manager is None:
            from .knowledge_base import KnowledgeBaseManager

            self._kb_manager = KnowledgeBaseManager()
            logger.info("KnowledgeBaseManager created")
        return self._kb_manager

    def _get_audit_logger(self):
        if self._audit_logger is None:
            from .audit_logger import AuditLogger

            self._audit_logger = AuditLogger()
            logger.info("AuditLogger created")
        return self._audit_logger

    def _get_version_store(self):
        if self._version_store is None:
            from .agent_version import AgentVersionStore

            self._version_store = AgentVersionStore()
            logger.info("AgentVersionStore created")
        return self._version_store

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

    async def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self.socket_path
        )
        os.chmod(self.socket_path, 0o666)

        self._ws_server = None
        if self.ws_port:
            self._ws_server = await asyncio.start_server(
                self._handle_ws_client, "127.0.0.1", self.ws_port
            )

        self._cluster_task: asyncio.Task | None = None
        if self.cluster_port:
            try:
                from .cluster_server import app as cluster_app
                import uvicorn

                config = uvicorn.Config(
                    cluster_app,
                    host="127.0.0.1",
                    port=self.cluster_port,
                    log_level="warning",
                )
                cluster_server = uvicorn.Server(config)
                self._cluster_task = asyncio.create_task(cluster_server.serve())
                logger.info("Cluster API server started on port %d", self.cluster_port)
            except Exception as e:
                logger.warning("Cluster API server failed to start: %s", e)

        self._http_task: asyncio.Task | None = None
        if self.http_port:
            try:
                from .api_server import app as fastapi_app, set_daemon

                set_daemon(self)
                import uvicorn as uvicorn2

                http_config = uvicorn2.Config(
                    fastapi_app,
                    host="127.0.0.1",
                    port=self.http_port,
                    log_level="warning",
                )
                http_server = uvicorn2.Server(http_config)
                self._http_task = asyncio.create_task(http_server.serve())
                logger.info("FastAPI HTTP server started on port %d", self.http_port)
            except Exception as e:
                logger.warning("FastAPI HTTP server failed to start: %s", e)

        self._running = True
        self._start_time = time.time()
        logger.info(
            "Daemon listening on %s + WS on %d + Cluster on %d + HTTP on %d",
            self.socket_path,
            self.ws_port,
            self.cluster_port,
            self.http_port,
        )

        if os.environ.get("FUSION_DISABLE_TELEMETRY", "").lower() not in (
            "1", "true", "yes",
        ):
            try:
                self._get_telemetry_engine()
                logger.info("Telemetry enabled by default (set FUSION_DISABLE_TELEMETRY=1 to disable)")
            except Exception as e:
                logger.warning("Telemetry auto-enable failed: %s", e)

        if await self._check_mlx_health():
            self._attach_mlx_client()
            logger.info("Auto-attached to running fusion-mlx on port %d", MLX_PORT)

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
            return self._error_response(
                msg_id, -32600, "Invalid Request: missing jsonrpc 2.0"
            )

        if not method:
            return self._error_response(
                msg_id, -32601, "Method not found: empty method"
            )

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
        core = {
            "budget.set": self._handle_budget_set,
            "budget.status": self._handle_budget_status,
            "context.compact": self._handle_context_compact,
            "context.usage": self._handle_context_usage,
            "env.health_check": self._handle_env_health_check,
            "env.repair": self._handle_env_repair,
            "env.repair_all": self._handle_env_repair_all,
            "graph.create": self._handle_graph_create,
            "graph.delete": self._handle_graph_delete,
            "graph.execute": self._handle_graph_execute,
            "graph.get": self._handle_graph_get,
            "graph.list": self._handle_graph_list,
            "graph.update": self._handle_graph_update,
            "mlx.health": self._handle_mlx_health,
            "mlx.infer": self._handle_mlx_infer,
            "mlx.restart": self._handle_mlx_restart,
            "mlx.set_model": self._handle_mlx_set_model,
            "mlx.start": self._handle_mlx_start,
            "mlx.status": self._handle_mlx_status,
            "mlx.stop": self._handle_mlx_stop,
            "mlx.switch_model_mid_turn": self._handle_mlx_switch_model_mid_turn,
            "ping": self._handle_ping,
            "daemon.ping": self._handle_daemon_ping,
            "daemon.status": self._handle_daemon_status,
            "daemon.shutdown": self._handle_daemon_shutdown,
            "rpc.discover": self._handle_rpc_discover,
            "tool.call": self._handle_tool_call,
            "session.attach": self._handle_session_attach,
            "session.background_kill": self._handle_session_background_kill,
            "session.background_list": self._handle_session_background_list,
            "session.detach": self._handle_session_detach,
            "session.fork": self._handle_session_fork,
            "session.get_accessibility": self._handle_session_get_accessibility,
            "session.list": self._handle_session_list,
            "session.set_accessibility": self._handle_session_set_accessibility,
            "tool.background_status": self._handle_tool_background_status,
            "tool.dynamic_register": self._handle_tool_dynamic_register,
            "tool.dynamic_unregister": self._handle_tool_dynamic_unregister,
            "tool.get": self._handle_tool_get,
            "tool.get_schema": self._handle_tool_get_schema,
            "tool.list": self._handle_tool_list,
            "tool.set_timeout": self._handle_tool_set_timeout,
        }
        handler = core.get(method)
        if handler:
            return handler
        for sd in self._sub_dispatchers:
            handlers = sd.get_handlers()
            if method in handlers:
                return handlers[method]
        return None

    def _init_sub_dispatchers(self):
        from .dispatchers import (
            MarketplaceDispatcher,
            DeployDispatcher,
            KnowledgeDispatcher,
            AgentDispatcher,
            ChatDispatcher,
            TeamDispatcher,
            InfraDispatcher,
            WorkflowDispatcher,
            SafetyDispatcher,
            PlannerDispatcher,
            MemoryDispatcher,
            PluginDispatcher,
            ArtifactDispatcher,
        )

        return [
            MarketplaceDispatcher(self),
            DeployDispatcher(self),
            KnowledgeDispatcher(self),
            AgentDispatcher(self),
            ChatDispatcher(self),
            TeamDispatcher(self),
            InfraDispatcher(self),
            WorkflowDispatcher(self),
            SafetyDispatcher(self),
            PlannerDispatcher(self),
            MemoryDispatcher(self),
            PluginDispatcher(self),
            ArtifactDispatcher(self),
        ]

    async def _handle_context_compact(self, params: dict) -> dict:
        compactor = self._get_compactor()
        messages = params.get("messages", [])
        level = params.get("level", "warning")
        compacted = compactor.compact(messages, level=level)
        before_tok = compactor.estimate_tokens(messages)
        after_tok = compactor.estimate_tokens(compacted)
        logger.info(
            "context.compact level=%s before_msgs=%d after_msgs=%d before_tok=%d after_tok=%d",
            level,
            len(messages),
            len(compacted),
            before_tok,
            after_tok,
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
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }

    # ── Handlers ──

    async def _handle_ping(self, params: dict) -> dict:
        return {"pong": True, "timestamp": time.time()}

    async def _handle_daemon_ping(self, params: dict) -> dict:
        return {"pong": True, "timestamp": time.time(), "daemon": True}

    async def _handle_daemon_status(self, params: dict) -> dict:
        return {
            "running": self._running,
            "socket_path": self.socket_path,
            "ws_port": self.ws_port,
            "cluster_port": self.cluster_port,
            "http_port": self.http_port,
            "mlx_attached": self._gateway._default_client is not None,
            "default_model": self._gateway._default_model or "",
            "active_sessions": len(self._active_executions),
            "uptime": time.time() - self._start_time if hasattr(self, "_start_time") and self._start_time else 0,
        }

    async def _handle_daemon_shutdown(self, params: dict) -> dict:
        logger.info("daemon.shutdown requested via RPC")
        self._running = False
        return {"status": "shutting_down"}

    async def _handle_rpc_discover(self, params: dict) -> dict:
        methods = {}
        core = {
            "budget.set": self._handle_budget_set,
            "budget.status": self._handle_budget_status,
            "context.compact": self._handle_context_compact,
            "context.usage": self._handle_context_usage,
            "env.health_check": self._handle_env_health_check,
            "env.repair": self._handle_env_repair,
            "env.repair_all": self._handle_env_repair_all,
            "graph.create": self._handle_graph_create,
            "graph.delete": self._handle_graph_delete,
            "graph.execute": self._handle_graph_execute,
            "graph.get": self._handle_graph_get,
            "graph.list": self._handle_graph_list,
            "graph.update": self._handle_graph_update,
            "mlx.health": self._handle_mlx_health,
            "mlx.infer": self._handle_mlx_infer,
            "mlx.restart": self._handle_mlx_restart,
            "mlx.set_model": self._handle_mlx_set_model,
            "mlx.start": self._handle_mlx_start,
            "mlx.status": self._handle_mlx_status,
            "mlx.stop": self._handle_mlx_stop,
            "mlx.switch_model_mid_turn": self._handle_mlx_switch_model_mid_turn,
            "ping": self._handle_ping,
            "daemon.ping": self._handle_daemon_ping,
            "daemon.status": self._handle_daemon_status,
            "daemon.shutdown": self._handle_daemon_shutdown,
            "rpc.discover": self._handle_rpc_discover,
            "session.attach": self._handle_session_attach,
            "session.background_kill": self._handle_session_background_kill,
            "session.background_list": self._handle_session_background_list,
            "session.detach": self._handle_session_detach,
            "session.fork": self._handle_session_fork,
            "session.get_accessibility": self._handle_session_get_accessibility,
            "session.list": self._handle_session_list,
            "session.set_accessibility": self._handle_session_set_accessibility,
            "tool.background_status": self._handle_tool_background_status,
            "tool.dynamic_register": self._handle_tool_dynamic_register,
            "tool.dynamic_unregister": self._handle_tool_dynamic_unregister,
            "tool.get": self._handle_tool_get,
            "tool.get_schema": self._handle_tool_get_schema,
            "tool.list": self._handle_tool_list,
            "tool.set_timeout": self._handle_tool_set_timeout,
            "tool.call": self._handle_tool_call,
        }
        methods.update(core)
        for sd in self._sub_dispatchers:
            methods.update(sd.get_handlers())
        return {"methods": sorted(methods.keys()), "count": len(methods)}

    async def _handle_tool_call(self, params: dict) -> dict:
        tool_name = params.get("tool_name", "")
        arguments = params.get("arguments", {})
        if not tool_name:
            return {"status": "error", "message": "tool_name is required"}
        registry = self._get_tool_registry()
        tool = registry._tools.get(tool_name)
        if tool is None:
            return {"status": "error", "message": f"tool '{tool_name}' not found"}
        try:
            result = await tool.execute(arguments)
            return {"status": "ok", "result": result}
        except Exception as e:
            logger.error("tool.call %s failed: %s", tool_name, e)
            return {"status": "error", "message": str(e)}

    async def _handle_mlx_start(self, params: dict) -> dict:
        model = params.get("model", "")
        if self._mlx_process and self._mlx_process.poll() is None:
            return {"status": "already_running", "port": MLX_PORT}

        # 复用已在运行的 fusion-mlx (如 fusion-studio start.sh 启动的)，
        # 避免在已占用端口上再起子进程导致冲突 (bug1 联动)
        if await self._check_mlx_health():
            self._attach_mlx_client()
            logger.info("Reusing already-running fusion-mlx on port %d", MLX_PORT)
            return {
                "status": "already_running",
                "port": MLX_PORT,
                "model": model,
                "external": True,
            }

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
            return {
                "status": "error",
                "message": "fusion-mlx failed to start within 30s",
            }

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
            return {
                "status": "error",
                "message": "fusion-mlx not running or unreachable",
            }

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
                    graph.add_edge(
                        source_id,
                        target_id,
                        label=e.get("label", e.get("condition", "")),
                    )

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
        if not isinstance(input_text, str):
            input_text = json.dumps(input_text, ensure_ascii=False)
        session_id = params.get("session_id", "")

        graph = self.store.load_graph(graph_id)
        if graph is None:
            raise ValueError(f"Graph not found: {graph_id}")

        rt = self._get_runtime()
        events = []

        async for event in rt.execute_graph(graph, input_text):
            ev_dict = (
                event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
            )
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
                    graph.add_edge(
                        source_id,
                        target_id,
                        label=e.get("label", e.get("condition", "")),
                    )

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

        if (
            not hasattr(self, "_cached_tool_registry")
            or self._cached_tool_registry is None
        ):
            self._cached_tool_registry = create_default_registry()
            logger.info(
                "Cached default tool registry with %d tools",
                len(self._cached_tool_registry.tools),
            )
        return self._cached_tool_registry

    async def _handle_tool_list(self, params: dict) -> dict:
        registry = self._get_tool_registry()
        tools = []
        for name, tool in registry._tools.items():
            schema = tool.get_schema()
            tools.append(
                {
                    "name": name,
                    "description": schema.get("description", ""),
                    "parameters": schema.get("parameters", {}),
                    "category": getattr(tool, "category", "built-in"),
                    "enabled": True,
                }
            )
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
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if result.returncode == 0:
                metrics["total_memory_gb"] = round(
                    int(result.stdout.strip()) / 1024 / 1024 / 1024, 1
                )
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["vm_stat"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "Pages free" in line:
                        free_pages = int(line.split(":")[1].strip().rstrip("."))
                        metrics["free_memory_gb"] = round(
                            free_pages * 16384 / 1024 / 1024 / 1024, 2
                        )
        except Exception:
            pass

        metrics["mlx_running"] = (
            self._mlx_process is not None and self._mlx_process.poll() is None
        )

        return metrics

    async def _handle_env_health_check(self, params: dict) -> dict:
        checks: dict[str, Any] = {}

        checks["python"] = {
            "ok": True,
            "version": platform.python_version(),
        }

        mlx_running = self._mlx_process is not None and self._mlx_process.poll() is None
        mlx_reachable = await self._check_mlx_health()
        checks["mlx_server"] = {
            "ok": mlx_running or mlx_reachable,
            "port": MLX_PORT,
            "process_managed": mlx_running,
            "api_reachable": mlx_reachable,
        }

        mlx_reachable = await self._check_mlx_health()
        checks["mlx_api"] = {"ok": mlx_reachable}

        try:
            import importlib.util

            checks["httpx"] = {"ok": importlib.util.find_spec("httpx") is not None}
        except Exception:
            checks["httpx"] = {"ok": False, "message": "httpx not installed"}

        socket_ok = os.path.exists(self.socket_path)
        checks["daemon_socket"] = {"ok": socket_ok, "path": self.socket_path}

        model_dir = Path.home() / ".fusion-mlx" / "models"
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
            logger.info(
                "PlannerEngine created (gateway=%s)",
                "enabled" if self._gateway._default_client else "stub",
            )
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
                logger.warning(
                    "KnowledgeEngine unavailable, RAG will run without retrieval"
                )
            self._rag = RAGPipeline(knowledge_engine=ke, gateway=self._gateway)
            logger.info(
                "RAGPipeline created (knowledge=%s, gateway=%s)",
                "enabled" if ke else "none",
                "enabled" if self._gateway._default_client else "stub",
            )
        return self._rag

    # ── Planner handlers ──

    def _get_vector_strategy(self, base_url: str = "http://localhost:8900"):
        from .rag_pipeline import VectorRetrievalStrategy

        if not hasattr(self, "_vector_strategy") or self._vector_strategy is None:
            self._vector_strategy = VectorRetrievalStrategy(base_url=base_url)
            logger.info("Created cached VectorRetrievalStrategy for %s", base_url)
        elif self._vector_strategy.base_url != base_url.rstrip("/"):
            logger.warning(
                "VectorRetrievalStrategy base_url mismatch: cached=%s requested=%s, re-creating",
                self._vector_strategy.base_url,
                base_url,
            )
            self._vector_strategy = VectorRetrievalStrategy(base_url=base_url)
        return self._vector_strategy

    def _get_cron_manager(self):
        from .triggers import CronManager

        if not hasattr(self, "_cron_manager") or self._cron_manager is None:
            import os

            db_path = os.path.expanduser("~/.fusion-agent-studio/cron.db")
            self._cron_manager = CronManager(db_path=db_path)
        return self._cron_manager

    async def _handle_tool_dynamic_register(self, params: dict) -> dict:
        from tools import ToolRegistry

        if not hasattr(self, "_dynamic_registry"):
            self._dynamic_registry = ToolRegistry()
        name = params.get("name", "")
        if not name:
            return {"status": "error", "message": "name parameter required"}
        if not self._SAFE_TOOL_NAME_RE.match(name):
            return {"status": "error", "message": f"invalid tool name '{name}'"}
        _tool_type = params.get("type", "terminal")
        description = params.get("description", "")
        tool_params = params.get("parameters", {})

        from tools.base import BaseTool
        from types import new_class

        param_dict = {}
        if isinstance(tool_params, dict):
            for pk, pv in tool_params.items():
                param_dict[pk] = (
                    pv
                    if isinstance(pv, dict)
                    else {"type": "string", "description": str(pv)}
                )

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
                    split_args[0],
                    *split_args[1:],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
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
        return {
            "status": "error",
            "message": f"Tool '{name}' not found in dynamic registry",
        }

    # ── Memory handlers ──

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

    async def _execute_code_task(self, task: dict):
        _agent_id = task["agent_id"]
        code = task["code"]
        language = task["language"]
        timeout = task.get("timeout", 60)
        logger.info(
            "_execute_code_task: task=%s lang=%s timeout=%s",
            task["task_id"],
            language,
            timeout,
        )
        if language != "python":
            return {"output": f"Unsupported language: {language}", "exit_code": 1}

        try:
            sandbox = CodeSandbox(timeout=timeout, use_sandbox=True)
            result = await asyncio.to_thread(sandbox.execute, code, language)
            output = result.stdout
            if result.stderr:
                output = (output + "\n" + result.stderr) if output else result.stderr
            if result.timed_out:
                output = (
                    (output + "\nExecution timed out")
                    if output
                    else "Execution timed out"
                )
            logger.info(
                "_execute_code_task done: task=%s exit=%s success=%s exec_id=%s",
                task["task_id"],
                result.exit_code,
                result.success,
                result.execution_id,
            )
            return {"output": output, "exit_code": result.exit_code}
        except Exception as exc:
            logger.error(
                "_execute_code_task error: task=%s error=%s", task["task_id"], exc
            )
            return {"output": str(exc), "exit_code": 1}

    async def _inject_knowledge_context(
        self, knowledge_base_ids: list[str], query: str, strategy: str = "hybrid"
    ) -> str:
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

    async def _handle_ws_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        try:
            header_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            header_text = header_line.decode("utf-8", errors="replace").strip()
            is_ws_upgrade = "Upgrade: websocket" in header_text or "upgrade: websocket" in header_text.lower()
            if is_ws_upgrade:
                remaining_headers = b""
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                    remaining_headers += line
                    if line == b"\r\n" or line == b"\n":
                        break
                ws_key = ""
                for h in remaining_headers.decode("utf-8", errors="replace").split("\r\n"):
                    if h.lower().startswith("sec-websocket-key:"):
                        ws_key = h.split(":", 1)[1].strip()
                if ws_key:
                    accept = base64.b64encode(
                        hashlib.sha1((ws_key + WS_MAGIC).encode()).digest()
                    ).decode()
                    response = (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    )
                    writer.write(response.encode())
                    await writer.drain()
                    logger.info("WS handshake completed for %s", peer)
                else:
                    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return
            else:
                writer.write(b"HTTP/1.1 400 Expected WebSocket Upgrade\r\n\r\n")
                await writer.drain()
                writer.close()
                return
        except asyncio.TimeoutError:
            logger.warning("WS handshake timeout from %s", peer)
            writer.close()
            return
        except Exception as e:
            logger.error("WS handshake error from %s: %s", peer, e)
            writer.close()
            return

        self._ws_clients.append(writer)
        logger.info("WS client connected: %s", peer)
        try:
            while self._running:
                text = await _ws_read_frame(reader)
                if text is None:
                    break
                try:
                    msg = json.loads(text)
                    await self._handle_ws_message(writer, msg)
                except json.JSONDecodeError:
                    logger.debug("WS non-JSON frame from %s", peer)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
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
                _ws_write_frame(writer, json.dumps({
                    "type": "chat_event",
                    "session_id": session_id,
                    "event": ev.to_dict(),
                }))
                await writer.drain()
            _ws_write_frame(writer, json.dumps({
                "type": "chat_done",
                "session_id": session_id,
            }))
            await writer.drain()
        elif action == "subscribe":
            _ws_write_frame(writer, json.dumps({"type": "subscribed"}))
            await writer.drain()

    async def _broadcast_event(self, event_type: str, data: dict) -> None:
        if not self._ws_clients:
            return
        payload = json.dumps({"type": event_type, **data})

        async def _send(client):
            try:
                _ws_write_frame(client, payload)
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
            self._gateway.register_default_local(name=loaded, base_url=MLX_BASE_URL)
        logger.info(
            "MLX client attached to gateway (api_key=%s, default_model=%s)",
            "set" if api_key else "none",
            self._gateway._default_model,
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
            os.path.expanduser(
                "~/Library/Application Support/fusion-mlx/settings.json"
            ),
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
                "flux",
                "vae",
                "transformer",
                "text_encoder",
                "siglip",
                "oldt5",
                "wan",
                "skyreels",
                "ltx",
                "tts",
            )
            chat_ids = [i for i in ids if not any(x in i.lower() for x in excluded)]
            if not chat_ids:
                return ids[0] if ids else ""
            preferred = (
                "Qwen3.6-9B-4bit",
                "Qwen3.5-9B-4bit",
                "Qwen3.6-27B-mxfp8",
                "Qwen3.6-27B-mixed_3_4",
                "Qwen3.6-27B-bf16",
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

    async def _handle_session_fork(self, params: dict) -> dict:
        mgr = self._get_session_manager()
        session_id = params.get("session_id", "")
        input_text = params.get("input", "")
        if not isinstance(input_text, str):
            input_text = json.dumps(input_text, ensure_ascii=False)
        bg_session = await mgr.fork(session_id, input_text)
        logger.info("session.fork: from=%s fork=%s", session_id, bg_session.id)
        return bg_session.to_dict()

    async def _handle_session_attach(self, params: dict) -> dict:
        mgr = self._get_session_manager()
        session_id = params.get("session_id", "")
        events = await mgr.attach(session_id)
        logger.info("session.attach: session=%s events=%d", session_id, len(events))
        return {"session_id": session_id, "events": events}

    async def _handle_session_detach(self, params: dict) -> dict:
        mgr = self._get_session_manager()
        session_id = params.get("session_id", "")
        ok = mgr.detach(session_id)
        logger.info("session.detach: session=%s ok=%s", session_id, ok)
        return {"session_id": session_id, "detached": ok}

    async def _handle_session_background_list(self, params: dict) -> dict:
        mgr = self._get_session_manager()
        sessions = mgr.background_list()
        return {"sessions": [s.to_dict() for s in sessions]}

    async def _handle_session_background_kill(self, params: dict) -> dict:
        mgr = self._get_session_manager()
        session_id = params.get("session_id", "")
        ok = await mgr.background_kill(session_id)
        logger.info("session.background_kill: session=%s ok=%s", session_id, ok)
        return {"session_id": session_id, "killed": ok}

    async def _handle_tool_set_timeout(self, params: dict) -> dict:
        tool_name = params.get("tool_name", "")
        timeout_ms = params.get("timeout_ms", 30000)
        if not hasattr(self, "_tool_timeouts"):
            self._tool_timeouts = {}
        self._tool_timeouts[tool_name] = timeout_ms
        logger.info("Tool timeout set: %s=%dms", tool_name, timeout_ms)
        return {"tool_name": tool_name, "timeout_ms": timeout_ms}

    async def _handle_tool_background_status(self, params: dict) -> dict:
        task_id = params.get("task_id", "")
        code_tasks = getattr(self, "_code_tasks", {})
        task_info = code_tasks.get(task_id)
        if task_info:
            return {
                "task_id": task_id,
                "status": task_info.get("status", "unknown"),
                "result": task_info.get("result"),
            }
        return {"task_id": task_id, "status": "not_found"}

    async def _handle_mlx_switch_model_mid_turn(self, params: dict) -> dict:
        model = params.get("model", "")
        if not model:
            return {"error": "model parameter required"}
        try:
            self._gateway.set_default_model(model)
            logger.info("Mid-turn model switch to: %s", model)
            return {"switched": True, "model": model}
        except Exception as e:
            return {"switched": False, "error": str(e)}

    async def _handle_session_set_accessibility(self, params: dict) -> dict:
        if not hasattr(self, "_accessibility"):
            self._accessibility = {
                "screen_reader": False,
                "high_contrast": False,
                "reduced_motion": False,
            }
        if "screen_reader" in params:
            self._accessibility["screen_reader"] = params["screen_reader"]
        if "high_contrast" in params:
            self._accessibility["high_contrast"] = params["high_contrast"]
        if "reduced_motion" in params:
            self._accessibility["reduced_motion"] = params["reduced_motion"]
        logger.info("Accessibility settings: %s", self._accessibility)
        return dict(self._accessibility)

    async def _handle_session_get_accessibility(self, params: dict) -> dict:
        if not hasattr(self, "_accessibility"):
            self._accessibility = {
                "screen_reader": False,
                "high_contrast": False,
                "reduced_motion": False,
            }
        return dict(self._accessibility)

    async def _handle_tool_get_schema(self, params: dict) -> dict:
        tool_name = params.get("tool_name", "")
        registry = self._get_runtime()._tool_registry
        if not registry:
            return {"error": "No tool registry available"}
        tool = registry.get_tool(tool_name)
        if not tool:
            return {"error": f"Tool not found: {tool_name}"}
        schema = (
            tool.get_schema() if hasattr(tool, "get_schema") else {"name": tool_name}
        )
        return {"tool_name": tool_name, "schema": schema}

    # ── Agent API handlers (#29, #31) ──

    def _get_agent_permissions(self, agent_id: str, manifest=None) -> dict:
        if manifest is None:
            from .agent_package import AgentPackage

            pkg = AgentPackage(self._agent_dir(agent_id))
            if not pkg.exists:
                return {}
            manifest = pkg.load_manifest()
        agent_dir = self._agent_dir(agent_id)
        defn_path = os.path.join(agent_dir, "definition.json")
        if os.path.exists(defn_path):
            try:
                import json

                with open(defn_path) as f:
                    defn_data = json.load(f)
                perms = defn_data.get("permissions", {})
                if perms:
                    return perms
            except Exception:
                pass
        return {
            "readKnowledge": bool(manifest.knowledge_base_ids),
            "writeKnowledge": False,
            "deleteKnowledge": False,
            "executeCode": "code_execution" in manifest.tools,
            "accessNetwork": manifest.web_search_enabled,
        }

    # ── LangGraph handlers (#35) ──


def run_daemon(socket_path: str = SOCKET_PATH):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    daemon = DaemonServer(socket_path=socket_path)
    asyncio.run(daemon.run_forever())


if __name__ == "__main__":
    run_daemon()
