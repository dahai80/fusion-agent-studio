"""#274: workspace↔agent binding store (Chat side).

Consumed by FSB's run-completion hook (POST /api/v1/chat/notify) and the
bind/unbind endpoints. Maps workspace_id -> {agent_id, session_id}. Kept in
the ChatEngine session metadata + an in-memory index. Env-gated via
FUSION_FSB_ENABLED=1 (binding is only meaningful when FSB is on).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class WorkspaceBinder:
    """In-memory workspace↔agent/session binding index (thread-safe).

    #274: FSB bind posts here to register the mapping; FSB run-completion
    notify uses it to resolve which chat session to push the result into.
    Binding is advisory metadata — chat works without it (FSB off).
    """

    def __init__(self):
        self._lock = threading.Lock()
        # workspace_id -> {"agent_id": str, "session_id": str | None}
        self._bindings: dict[str, dict[str, Any]] = {}

    def bind(self, workspace_id: str, agent_id: str, session_id: str | None = None) -> None:
        if not workspace_id or not agent_id:
            return
        with self._lock:
            self._bindings[workspace_id] = {"agent_id": agent_id, "session_id": session_id}
        logger.info("workspace binder bind ws=%s agent=%s session=%s", workspace_id, agent_id, session_id)

    def unbind(self, workspace_id: str) -> None:
        with self._lock:
            self._bindings.pop(workspace_id, None)
        logger.info("workspace binder unbind ws=%s", workspace_id)

    def get(self, workspace_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._bindings.get(workspace_id)

    def find_by_agent(self, agent_id: str) -> str | None:
        """Return workspace_id bound to agent_id, or None."""
        with self._lock:
            for ws_id, b in self._bindings.items():
                if b.get("agent_id") == agent_id:
                    return ws_id
        return None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"workspace_id": k, **v} for k, v in self._bindings.items()]


_singleton: WorkspaceBinder | None = None


def get_workspace_binder() -> WorkspaceBinder:
    global _singleton
    if _singleton is None:
        _singleton = WorkspaceBinder()
    return _singleton
