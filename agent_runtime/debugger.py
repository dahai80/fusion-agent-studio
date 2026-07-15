"""Step debugger — single-step execution and breakpoint support for agent graphs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DebuggerState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    STEP_OVER = "step_over"
    STEP_INTO = "step_into"
    STOPPED = "stopped"


@dataclass
class Breakpoint:
    """A breakpoint on a specific node in the graph."""
    node_id: str
    enabled: bool = True
    condition: str = ""  # Optional condition expression
    hit_count: int = 0


@dataclass
class DebugEvent:
    """Event emitted during debugging."""
    type: str  # "pause", "resume", "step", "breakpoint_hit", "variable_change", "error"
    node_id: str = ""
    message: str = ""
    variables: dict = field(default_factory=dict)
    timestamp: float = 0.0


class StepDebugger:
    """Provides single-step debugging for agent graph execution.

    Supports:
    - Pause/resume execution
    - Step over (execute current node, pause at next)
    - Breakpoints on specific nodes
    - Variable inspection at each step
    """

    def __init__(self):
        self.state = DebuggerState.RUNNING
        self._breakpoints: dict[str, Breakpoint] = {}
        self._event_queue: asyncio.Queue[DebugEvent] = asyncio.Queue()
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # Start running
        self._current_node_id: str = ""

    async def pause(self) -> None:
        """Pause execution at the next opportunity."""
        self.state = DebuggerState.PAUSED
        self._pause_event.clear()
        await self._emit(DebugEvent(type="pause", message="Execution paused"))

    async def resume(self) -> None:
        """Resume execution."""
        self.state = DebuggerState.RUNNING
        self._pause_event.set()
        await self._emit(DebugEvent(type="resume", message="Execution resumed"))

    async def step_over(self) -> None:
        """Execute the next node and pause."""
        self.state = DebuggerState.STEP_OVER
        self._pause_event.set()

    async def step_into(self) -> None:
        """Step into a sub-graph or tool call."""
        self.state = DebuggerState.STEP_INTO
        self._pause_event.set()

    def add_breakpoint(self, node_id: str, condition: str = "") -> None:
        """Add a breakpoint on a node."""
        self._breakpoints[node_id] = Breakpoint(node_id=node_id, condition=condition)

    def remove_breakpoint(self, node_id: str) -> None:
        """Remove a breakpoint from a node."""
        self._breakpoints.pop(node_id, None)

    def has_breakpoint(self, node_id: str) -> bool:
        """Check if a node has a breakpoint."""
        bp = self._breakpoints.get(node_id)
        return bp is not None and bp.enabled

    async def check_pause(self, node_id: str, variables: dict | None = None) -> None:
        """Check if execution should pause before executing a node."""
        self._current_node_id = node_id

        # Check breakpoints
        if self.has_breakpoint(node_id):
            bp = self._breakpoints[node_id]
            bp.hit_count += 1
            self.state = DebuggerState.PAUSED
            self._pause_event.clear()
            await self._emit(DebugEvent(
                type="breakpoint_hit",
                node_id=node_id,
                message=f"Breakpoint hit: {node_id} (hit {bp.hit_count})",
                variables=variables or {},
            ))
            return

        # Wait if paused or step mode
        await self._pause_event.wait()

        # If step_over, pause after this node
        if self.state == DebuggerState.STEP_OVER:
            self.state = DebuggerState.PAUSED
            self._pause_event.clear()
            await self._emit(DebugEvent(
                type="step",
                node_id=node_id,
                message=f"Stepped to: {node_id}",
                variables=variables or {},
            ))

    async def _emit(self, event: DebugEvent) -> None:
        """Emit a debug event to the queue."""
        import time
        event.timestamp = time.time()
        await self._event_queue.put(event)

    async def next_event(self) -> DebugEvent:
        """Get the next debug event (blocks until available)."""
        return await self._event_queue.get()

    def stop(self) -> None:
        """Stop debugging."""
        self.state = DebuggerState.STOPPED
        self._pause_event.set()