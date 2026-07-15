"""UndoManager — canvas operation history for undo/redo support."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CanvasSnapshot:
    """A snapshot of the canvas state at a point in time."""
    nodes: dict[str, Any] = field(default_factory=dict)
    edges: list[dict[str, Any]] = field(default_factory=list)
    selected_node_id: str = ""


class UndoManager:
    """Manages canvas operation history for undo/redo.

    Records snapshots of the graph state before each mutation.
    Supports configurable max history depth.
    """

    def __init__(self, max_history: int = 50):
        self._undo_stack: list[CanvasSnapshot] = []
        self._redo_stack: list[CanvasSnapshot] = []
        self._max_history = max_history

    def record(self, nodes: dict, edges: list, selected: str = "") -> None:
        """Record a snapshot before a mutation."""
        snap = CanvasSnapshot(
            nodes={k: dict(v) if hasattr(v, 'to_dict') else dict(v) for k, v in nodes.items()},
            edges=[dict(e) if hasattr(e, 'to_dict') else dict(e) for e in edges],
            selected_node_id=selected,
        )
        self._undo_stack.append(snap)
        if len(self._undo_stack) > self._max_history:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def undo(self) -> CanvasSnapshot | None:
        """Undo the last operation. Returns the previous state or None."""
        if len(self._undo_stack) < 2:
            return None
        current = self._undo_stack.pop()
        self._redo_stack.append(current)
        return self._undo_stack[-1]

    def redo(self) -> CanvasSnapshot | None:
        """Redo the last undone operation. Returns the restored state or None."""
        if not self._redo_stack:
            return None
        state = self._redo_stack.pop()
        self._undo_stack.append(state)
        return state

    @property
    def can_undo(self) -> bool:
        return len(self._undo_stack) >= 2

    @property
    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    def clear(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()

    @property
    def undo_count(self) -> int:
        return len(self._undo_stack)

    @property
    def redo_count(self) -> int:
        return len(self._redo_stack)