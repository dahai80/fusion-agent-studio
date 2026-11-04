"""#558: run history — archived managed-run executions with diff support.

Every managed run (api_server _execute_managed_run) archives its event
frames here on completion. RunHistoryStore derives a terminal status
(completed/failed/cancelled) plus per-node last-status from the frames,
so the canvas can list past runs, replay a run's node states, and diff
two runs node-by-node.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_EVENT_FRAMES = 500

_STATUS_RANK = {"error": 3, "failed": 3, "cancelled": 3, "done": 2, "result": 2, "end": 2}


def _summarize(frames: list[dict]) -> tuple[str, dict[str, str]]:
    status = "completed"
    node_status: dict[str, str] = {}
    for f in frames:
        ftype = str(f.get("type", ""))
        node = str(f.get("node_id", "") or "")
        if ftype in ("error", "failed"):
            status = "cancelled" if "cancel" in str(f.get("content", "")).lower() else "failed"
        elif ftype == "cancelled":
            status = "cancelled"
        if node:
            rank = _STATUS_RANK.get(ftype, 0)
            cur = node_status.get(node)
            if cur is None or rank >= _STATUS_RANK.get(cur, 0):
                node_status[node] = "error" if rank == 3 else "done"
    return status, node_status


class RunHistoryStore:
    def __init__(self, db_path: str | Path = ""):
        if not db_path:
            db_path = Path.home() / ".fusion-agent-studio" / "run_history.db"
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS run_history (
                run_id TEXT PRIMARY KEY,
                graph_id TEXT NOT NULL DEFAULT '',
                graph_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'completed',
                node_status TEXT NOT NULL DEFAULT '{}',
                events_json TEXT NOT NULL DEFAULT '[]',
                started_at REAL NOT NULL DEFAULT 0,
                finished_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_run_history_graph ON run_history(graph_id, finished_at);
            """
        )
        self._conn.commit()
        logger.info("run history store ready db=%s", self.db_path)

    def close(self):
        with self._lock:
            self._conn.close()

    def save_run(
        self,
        run_id: str,
        graph_id: str,
        graph_name: str,
        frames: list[dict],
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> dict:
        frames = list(frames or [])[-MAX_EVENT_FRAMES:]
        status, node_status = _summarize(frames)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO run_history (run_id, graph_id, graph_name, status, node_status, events_json, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    graph_id or "",
                    graph_name or "",
                    status,
                    json.dumps(node_status, ensure_ascii=False),
                    json.dumps(frames, ensure_ascii=False),
                    started_at if started_at is not None else now,
                    finished_at if finished_at is not None else now,
                ),
            )
            self._conn.commit()
        logger.info("run archived id=%s graph=%s status=%s frames=%d", run_id, graph_id, status, len(frames))
        return {"run_id": run_id, "status": status, "frames": len(frames)}

    def list_runs(self, graph_id: str = "", limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            if graph_id:
                rows = self._conn.execute(
                    "SELECT run_id, graph_id, graph_name, status, node_status, started_at, finished_at FROM run_history WHERE graph_id=? ORDER BY finished_at DESC LIMIT ?",
                    (graph_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT run_id, graph_id, graph_name, status, node_status, started_at, finished_at FROM run_history ORDER BY finished_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["node_status"] = json.loads(d.get("node_status") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["node_status"] = {}
            out.append(d)
        return out

    def load_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id, graph_id, graph_name, status, node_status, events_json, started_at, finished_at FROM run_history WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["events"] = json.loads(d.pop("events_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["events"] = []
        try:
            d["node_status"] = json.loads(d.get("node_status") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["node_status"] = {}
        return d

    def diff_runs(self, run_a: str, run_b: str) -> dict | None:
        a = self.load_run(run_a)
        b = self.load_run(run_b)
        if a is None or b is None:
            return None
        na, nb = a.get("node_status") or {}, b.get("node_status") or {}
        nodes = sorted(set(na) | set(nb))
        rows = [
            {"node_id": n, "a": na.get(n, ""), "b": nb.get(n, ""), "changed": na.get(n, "") != nb.get(n, "")}
            for n in nodes
        ]
        return {
            "run_a": run_a,
            "run_b": run_b,
            "status_a": a.get("status", ""),
            "status_b": b.get("status", ""),
            "nodes": rows,
            "changed_count": sum(1 for r in rows if r["changed"]),
        }
