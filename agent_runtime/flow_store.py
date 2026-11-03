"""#555: flow store — first-class WorkflowGraph documents + version snapshots.

SQLite-backed store for web canvas flows (mirror of studio#426 §6.5).
Every save that changes the graph JSON records a version snapshot; restore
writes the snapshot back as a new current version. Flows are scoped per
tenant (fusion-identity lineage #271); local mode uses tenant_id="".

No schema-migration framework in v1 (per issue): a schema_version pragma
guards future migrations.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

FLOW_SCHEMA_VERSION = 1


class FlowStoreError(Exception):
    """Raised when a flow document is corrupt or an operation is invalid."""


class FlowStore:
    def __init__(self, db_path: str | Path = ""):
        if not db_path:
            db_path = Path.home() / ".fusion-agent-studio" / "flows.db"
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            logger.info("created flow store dir: %s", parent)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS flows (
                    flow_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    graph_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flow_versions (
                    flow_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    graph_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (flow_id, version)
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_flows_tenant ON flows(tenant_id)"
            )
            self._conn.commit()
        logger.info("flow store ready db=%s schema_v=%d", self.db_path, FLOW_SCHEMA_VERSION)

    def close(self):
        with self._lock:
            self._conn.close()

    def _validate_graph(self, graph: dict):
        if not isinstance(graph, dict):
            raise FlowStoreError("flow graph must be a JSON object")
        nodes = graph.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise FlowStoreError("flow graph must contain a non-empty 'nodes' array")
        for n in nodes:
            if not isinstance(n, dict) or not n.get("id") or not n.get("type"):
                raise FlowStoreError(
                    "flow graph node missing 'id' or 'type' — re-export the flow"
                )

    def save_flow(
        self,
        graph: dict,
        tenant_id: str = "",
        name: str = "",
        description: str = "",
        flow_id: str = "",
    ) -> dict:
        self._validate_graph(graph)
        now = time.time()
        graph_json = json.dumps(graph, ensure_ascii=False)
        with self._lock:
            if flow_id:
                row = self._conn.execute(
                    "SELECT flow_id, version, graph_json FROM flows WHERE flow_id=? AND tenant_id=?",
                    (flow_id, tenant_id),
                ).fetchone()
                if row is None:
                    raise FlowStoreError(f"flow '{flow_id}' not found for tenant '{tenant_id or 'default'}'")
                if row["graph_json"] == graph_json:
                    return {
                        "flow_id": flow_id,
                        "version": row["version"],
                        "created": False,
                        "updated_at": now,
                    }
                new_version = row["version"] + 1
                self._conn.execute(
                    "UPDATE flows SET graph_json=?, version=?, name=?, description=?, updated_at=? WHERE flow_id=? AND tenant_id=?",
                    (graph_json, new_version, name or row_name(self._conn, flow_id, tenant_id), description, now, flow_id, tenant_id),
                )
                self._conn.execute(
                    "INSERT INTO flow_versions (flow_id, version, graph_json, created_at) VALUES (?,?,?,?)",
                    (flow_id, new_version, graph_json, now),
                )
                self._conn.commit()
                logger.info("flow saved id=%s version=%d tenant=%s", flow_id, new_version, tenant_id)
                return {"flow_id": flow_id, "version": new_version, "created": False, "updated_at": now}
            fid = flow_id or ("flow-" + uuid.uuid4().hex[:12])
            self._conn.execute(
                "INSERT INTO flows (flow_id, tenant_id, name, description, graph_json, version, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)",
                (fid, tenant_id, name or "Untitled flow", description, graph_json, now, now),
            )
            self._conn.execute(
                "INSERT INTO flow_versions (flow_id, version, graph_json, created_at) VALUES (?,?,?,?)",
                (fid, 1, graph_json, now),
            )
            self._conn.commit()
            logger.info("flow created id=%s tenant=%s", fid, tenant_id)
            return {"flow_id": fid, "version": 1, "created": True, "updated_at": now}

    def list_flows(self, tenant_id: str = "") -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT flow_id, name, description, version, created_at, updated_at FROM flows WHERE tenant_id=? ORDER BY updated_at DESC",
                (tenant_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_flow(self, flow_id: str, tenant_id: str = "") -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT graph_json, version, name, description, created_at, updated_at FROM flows WHERE flow_id=? AND tenant_id=?",
                (flow_id, tenant_id),
            ).fetchone()
        if row is None:
            raise FlowStoreError(f"flow '{flow_id}' not found for tenant '{tenant_id or 'default'}'")
        try:
            graph = json.loads(row["graph_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error("flow corrupt id=%s: %s", flow_id, exc)
            raise FlowStoreError(
                f"flow '{flow_id}' is corrupt ({exc}) — restore from a version snapshot via flow.restore or delete it"
            ) from exc
        return {
            "flow_id": flow_id,
            "name": row["name"],
            "description": row["description"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "graph": graph,
        }

    def delete_flow(self, flow_id: str, tenant_id: str = "") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM flows WHERE flow_id=? AND tenant_id=?",
                (flow_id, tenant_id),
            )
            self._conn.execute(
                "DELETE FROM flow_versions WHERE flow_id=?",
                (flow_id,),
            )
            self._conn.commit()
        deleted = cur.rowcount > 0
        if deleted:
            logger.info("flow deleted id=%s tenant=%s", flow_id, tenant_id)
        return deleted

    def list_versions(self, flow_id: str, tenant_id: str = "") -> list[dict]:
        with self._lock:
            owner = self._conn.execute(
                "SELECT tenant_id FROM flows WHERE flow_id=?", (flow_id,)
            ).fetchone()
            if owner is None or owner["tenant_id"] != tenant_id:
                raise FlowStoreError(f"flow '{flow_id}' not found for tenant '{tenant_id or 'default'}'")
            rows = self._conn.execute(
                "SELECT version, created_at FROM flow_versions WHERE flow_id=? ORDER BY version DESC",
                (flow_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def restore_version(self, flow_id: str, version: int, tenant_id: str = "") -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, graph_json FROM flows WHERE flow_id=? AND tenant_id=?",
                (flow_id, tenant_id),
            ).fetchone()
            if row is None:
                raise FlowStoreError(f"flow '{flow_id}' not found for tenant '{tenant_id or 'default'}'")
            snap = self._conn.execute(
                "SELECT graph_json FROM flow_versions WHERE flow_id=? AND version=?",
                (flow_id, version),
            ).fetchone()
            if snap is None:
                raise FlowStoreError(f"version {version} of flow '{flow_id}' does not exist")
            current = json.loads(row["graph_json"])
            restored = json.loads(snap["graph_json"])
            if json.dumps(current, sort_keys=True) == json.dumps(restored, sort_keys=True):
                return {"flow_id": flow_id, "version": row["version"], "restored": False}
            new_version = row["version"] + 1
            now = time.time()
            self._conn.execute(
                "UPDATE flows SET graph_json=?, version=?, updated_at=? WHERE flow_id=? AND tenant_id=?",
                (snap["graph_json"], new_version, now, flow_id, tenant_id),
            )
            self._conn.execute(
                "INSERT INTO flow_versions (flow_id, version, graph_json, created_at) VALUES (?,?,?,?)",
                (flow_id, new_version, snap["graph_json"], now),
            )
            self._conn.commit()
        logger.info("flow restored id=%s from_v=%d to_v=%d tenant=%s", flow_id, version, new_version, tenant_id)
        return {"flow_id": flow_id, "version": new_version, "restored": True}


def row_name(conn: sqlite3.Connection, flow_id: str, tenant_id: str) -> str:
    row = conn.execute(
        "SELECT name FROM flows WHERE flow_id=? AND tenant_id=?",
        (flow_id, tenant_id),
    ).fetchone()
    return row["name"] if row else "Untitled flow"


def resolve_tenant_id() -> str:
    """Current tenant from fusion-core TenantContext; '' in local mode."""
    try:
        from fusion_core.tenant.context import current

        ctx = current()
        if ctx is not None:
            return str(getattr(ctx, "tenant_id", "") or "")
    except Exception as e:
        logger.debug("tenant context unavailable, using default: %s", e)
    return ""
