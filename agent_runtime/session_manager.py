from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .context import AgentContext, AgentEventType

logger = logging.getLogger(__name__)


class SessionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


@dataclass
class BackgroundSession:
    id: str = ""
    forked_from: str = ""
    status: SessionStatus = SessionStatus.RUNNING
    created_at: float = 0.0
    finished_at: float = 0.0
    input_text: str = ""
    event_buffer: list[dict] = field(default_factory=list)
    output: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)

    def __post_init__(self):
        if not self.id:
            self.id = f"bg_{uuid.uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "forked_from": self.forked_from,
            "status": self.status.value,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "input_text": self.input_text[:200],
            "event_count": len(self.event_buffer),
            "output": self.output[:500],
            "error": self.error,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BackgroundSession:
        return cls(
            id=data.get("id", ""),
            forked_from=data.get("forked_from", ""),
            status=SessionStatus(data.get("status", "running")),
            created_at=data.get("created_at", 0.0),
            finished_at=data.get("finished_at", 0.0),
            input_text=data.get("input_text", ""),
            event_buffer=data.get("event_buffer", []),
            output=data.get("output", ""),
            error=data.get("error", ""),
            metadata=data.get("metadata", {}),
        )


class SessionManager:
    def __init__(self, runtime=None, gateway=None, store=None):
        self.runtime = runtime
        self.gateway = gateway
        self.store = store
        self._sessions: dict[str, BackgroundSession] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        logger.info(
            "SessionManager init, runtime=%s, gateway=%s",
            "provided" if runtime else "none",
            "provided" if gateway else "none",
        )

    async def fork(self, session_id: str, input_text: str = "") -> BackgroundSession:
        ctx = None
        if self.store:
            try:
                sessions = self.store.list_sessions()
                for s in sessions:
                    if s.get("id") == session_id:
                        ctx = AgentContext()
                        break
            except Exception as e:
                logger.warning("Failed to load session %s for fork: %s", session_id, e)

        bg = BackgroundSession(
            forked_from=session_id,
            input_text=input_text,
        )
        self._sessions[bg.id] = bg

        task = asyncio.create_task(self._run_background(bg, input_text, ctx))
        bg._task = task
        self._tasks[bg.id] = task
        logger.info("Forked background session %s from %s", bg.id, session_id)
        return bg

    async def _run_background(
        self,
        bg: BackgroundSession,
        input_text: str,
        ctx: AgentContext | None,
    ) -> None:
        try:
            if not self.runtime:
                bg.status = SessionStatus.FAILED
                bg.error = "No runtime available"
                bg.finished_at = time.time()
                logger.error("Background session %s: no runtime", bg.id)
                return

            graph = None
            if self.store:
                try:
                    graphs = self.store.list_graphs()
                    if graphs:
                        from .graph import AgentGraph

                        graph = AgentGraph.from_dict(graphs[0])
                except Exception:
                    pass

            if graph is None:
                from .graph import AgentGraph, NodeConfig, NodeType

                graph = AgentGraph(name=f"bg_{bg.id}")
                start_id = "start"
                llm_id = "llm"
                end_id = "end"
                # C16: 统一 soul.md 加载 — 后台会话 metadata.agent_id 可用时用 soul.
                bg_agent_id = ""
                if bg.metadata:
                    bg_agent_id = bg.metadata.get("agent_id", "")
                bg_soul = "You are a helpful assistant."
                if bg_agent_id:
                    try:
                        from .agent_package import resolve_soul_prompt

                        bg_soul = resolve_soul_prompt(
                            bg_agent_id, fallback=bg_soul
                        )
                    except Exception as e:
                        logger.warning(
                            "bg session soul resolve failed for agent=%s: %s",
                            bg_agent_id,
                            e,
                        )
                graph.add_node(start_id, NodeConfig(type=NodeType.START, label="Start"))
                graph.add_node(
                    llm_id,
                    NodeConfig(
                        type=NodeType.LLM,
                        label="LLM",
                        system_prompt=bg_soul,
                    ),
                )
                graph.add_node(end_id, NodeConfig(type=NodeType.END, label="End"))
                graph.add_edge(start_id, llm_id)
                graph.add_edge(llm_id, end_id)

            if ctx is None:
                ctx = AgentContext()

            events = []
            async for event in self.runtime.execute_graph(graph, input_text, ctx):
                ev_dict = event.to_dict()
                bg.event_buffer.append(ev_dict)
                events.append(event)
                for q in bg._subscribers:
                    try:
                        q.put_nowait(ev_dict)
                    except asyncio.QueueFull:
                        pass

            output = ""
            for ev in reversed(events):
                if ev.type == AgentEventType.THINK and ev.content:
                    output = ev.content
                    break
            if not output:
                for msg in reversed(ctx.messages):
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        output = msg.get("content", "")
                        break

            bg.output = output
            bg.status = SessionStatus.COMPLETED
            bg.finished_at = time.time()
            logger.info(
                "Background session %s completed, output len=%d", bg.id, len(output)
            )

        except asyncio.CancelledError:
            bg.status = SessionStatus.KILLED
            bg.finished_at = time.time()
            logger.info("Background session %s killed", bg.id)
        except Exception as e:
            bg.status = SessionStatus.FAILED
            bg.error = str(e)
            bg.finished_at = time.time()
            logger.exception("Background session %s failed", bg.id)

    async def attach(self, session_id: str) -> list[dict]:
        bg = self._sessions.get(session_id)
        if not bg:
            raise ValueError(f"Background session not found: {session_id}")
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        bg._subscribers.append(q)
        buffered = list(bg.event_buffer)
        logger.info(
            "Attached to background session %s, buffered=%d", session_id, len(buffered)
        )
        return buffered

    def detach(self, session_id: str) -> bool:
        bg = self._sessions.get(session_id)
        if not bg:
            return False
        bg._subscribers.clear()
        logger.info("Detached from background session %s", session_id)
        return True

    def background_list(self) -> list[BackgroundSession]:
        return list(self._sessions.values())

    async def background_kill(self, session_id: str) -> bool:
        bg = self._sessions.get(session_id)
        if not bg:
            return False
        task = self._tasks.get(session_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        bg.status = SessionStatus.KILLED
        bg.finished_at = time.time()
        logger.info("Killed background session %s", session_id)
        return True
