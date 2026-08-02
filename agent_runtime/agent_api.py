"""Agent REST API — published agents, status, history endpoints.

Implements #29 (Agent Publish API) and #31 (Agent Status & History API).
FastAPI router mounted into the main api_server.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RUN_HISTORY_DIR = Path.home() / ".fusion-agent-studio" / "run_history"


@dataclass
class RunHistoryEntry:
    run_id: str = ""
    agent_id: str = ""
    trigger: str = "manual"
    input_summary: str = ""
    output_summary: str = ""
    tokens_used: int = 0
    duration_ms: int = 0
    status: str = "completed"
    started_at: float = 0.0
    completed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "trigger": self.trigger,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "tokens_used": self.tokens_used,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunHistoryEntry:
        return cls(
            run_id=data.get("run_id", ""),
            agent_id=data.get("agent_id", ""),
            trigger=data.get("trigger", "manual"),
            input_summary=data.get("input_summary", ""),
            output_summary=data.get("output_summary", ""),
            tokens_used=data.get("tokens_used", 0),
            duration_ms=data.get("duration_ms", 0),
            status=data.get("status", "completed"),
            started_at=data.get("started_at", 0.0),
            completed_at=data.get("completed_at", 0.0),
        )


@dataclass
class AgentStatusInfo:
    agent_id: str = ""
    status: str = "idle"
    current_task: str = ""
    last_run_at: float = 0.0
    total_runs: int = 0
    error_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "current_task": self.current_task,
            "last_run_at": self.last_run_at,
            "total_runs": self.total_runs,
            "error_count": self.error_count,
        }


class AgentStatusTracker:
    """Track agent runtime status and execution history."""

    def __init__(self, data_dir: Path | str | None = None):
        self.data_dir = Path(data_dir) if data_dir else RUN_HISTORY_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._status: dict[str, AgentStatusInfo] = {}
        self._history: dict[str, list[RunHistoryEntry]] = {}
        self._load()

    def _status_path(self) -> Path:
        return self.data_dir / "agent_status.json"

    def _history_path(self, agent_id: str) -> Path:
        return self.data_dir / f"{agent_id}_history.json"

    def _load(self):
        import json

        sp = self._status_path()
        if sp.exists():
            with open(sp) as f:
                data = json.load(f)
            for aid, info in data.items():
                self._status[aid] = AgentStatusInfo(
                    agent_id=aid,
                    status=info.get("status", "idle"),
                    current_task=info.get("current_task", ""),
                    last_run_at=info.get("last_run_at", 0.0),
                    total_runs=info.get("total_runs", 0),
                    error_count=info.get("error_count", 0),
                )
            logger.info("Loaded agent status: %d agents", len(self._status))

    def _save_status(self):
        import json

        with open(self._status_path(), "w") as f:
            json.dump(
                {aid: s.to_dict() for aid, s in self._status.items()}, f, indent=2
            )

    def _save_history(self, agent_id: str):
        import json

        entries = self._history.get(agent_id, [])
        with open(self._history_path(agent_id), "w") as f:
            json.dump([e.to_dict() for e in entries], f, indent=2)

    def get_status(self, agent_id: str) -> AgentStatusInfo:
        if agent_id not in self._status:
            self._status[agent_id] = AgentStatusInfo(agent_id=agent_id)
        return self._status[agent_id]

    def set_running(self, agent_id: str, task: str = ""):
        info = self.get_status(agent_id)
        info.status = "running"
        info.current_task = task
        self._save_status()
        logger.info("Agent %s: running (%s)", agent_id, task)

    def set_idle(self, agent_id: str, error: bool = False):
        info = self.get_status(agent_id)
        info.status = "error" if error else "idle"
        info.current_task = ""
        info.last_run_at = time.time()
        info.total_runs += 1
        if error:
            info.error_count += 1
        self._save_status()

    def record_run(self, entry: RunHistoryEntry):
        aid = entry.agent_id
        if aid not in self._history:
            self._history[aid] = []
            hp = self._history_path(aid)
            if hp.exists():
                import json

                with open(hp) as f:
                    data = json.load(f)
                self._history[aid] = [RunHistoryEntry.from_dict(e) for e in data]
        self._history[aid].append(entry)
        if len(self._history[aid]) > 1000:
            self._history[aid] = self._history[aid][-1000:]
        self._save_history(aid)
        logger.debug("Recorded run %s for agent %s", entry.run_id, aid)

    def get_history(
        self, agent_id: str, limit: int = 50, offset: int = 0
    ) -> list[RunHistoryEntry]:
        if agent_id not in self._history:
            hp = self._history_path(agent_id)
            if hp.exists():
                import json

                with open(hp) as f:
                    data = json.load(f)
                self._history[agent_id] = [RunHistoryEntry.from_dict(e) for e in data]
            else:
                self._history[agent_id] = []
        entries = self._history[agent_id]
        return list(reversed(entries))[offset : offset + limit]

    def list_published(self, agents_index: dict[str, dict]) -> list[dict[str, Any]]:
        results = []
        for aid, meta in agents_index.items():
            if meta.get("status") == "published":
                results.append(
                    {
                        "agent_id": aid,
                        "name": meta.get("name", ""),
                        "description": meta.get("description", ""),
                        "version": meta.get("version", "0.1.0"),
                        "status": "published",
                        "published_at": meta.get("published_at"),
                        "published_by": meta.get("author", ""),
                    }
                )
        logger.info("Listed %d published agents", len(results))
        return results

    def get_definition(
        self, agent_id: str, agents_index: dict[str, dict]
    ) -> dict[str, Any] | None:
        from .agent_definition import AgentDefinition

        meta = agents_index.get(agent_id)
        if not meta or meta.get("status") != "published":
            return None
        definition = AgentDefinition.from_manifest(meta, agent_id=agent_id)
        return definition.to_dict()
