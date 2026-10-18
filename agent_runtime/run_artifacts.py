# N9 (#348): run artifact versioned triple — graph definition + environment
# snapshot + conversation archive. Deterministic, local-only (no LLM calls).
from __future__ import annotations

import json
import logging
import os
import platform
import sys
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = "run_artifact.v1"
DEFAULT_ROOT = Path.home() / ".fusion-agent-studio" / "run_artifacts"

_ENV_KEYS = (
    "FUSION_MLX_PORT",
    "FUSION_GATEWAY_URL",
    "FUSION_MLX_API_KEY",
    "FUSION_RAG_BACKEND",
    "FUSION_IDENTITY_ENABLED",
    "FUSION_FSB_ENABLED",
    "FUSION_ENABLE_WS",
)


def snapshot_environment(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        from agent_runtime.llm_gateway import DEFAULT_LOCAL_BASE_URL
    except Exception:
        DEFAULT_LOCAL_BASE_URL = ""
    env = {k: os.environ.get(k, "") for k in _ENV_KEYS}
    # api key value is a secret — record presence only
    env["FUSION_MLX_API_KEY"] = "set" if os.environ.get("FUSION_MLX_API_KEY") else ""
    snap = {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "sys_version": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "gateway_base": DEFAULT_LOCAL_BASE_URL,
        "env": env,
        "captured_at": time.time(),
    }
    if extra:
        snap.update(extra)
    return snap


def _message_record(msg: dict[str, Any]) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "role": msg.get("role", ""),
        "content": msg.get("content", ""),
    }
    if msg.get("tool_calls"):
        rec["tool_calls"] = msg["tool_calls"]
    if msg.get("tool_call_id"):
        rec["tool_call_id"] = msg["tool_call_id"]
    return rec


class RunArtifactStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, artifact_id: str) -> Path:
        return self.root / f"{artifact_id}.json"

    def _next_version(self, name: str) -> int:
        version = 0
        for rec in self.list(limit=0):
            if rec.get("name") != name:
                continue
            try:
                version = max(version, int(rec.get("version", 0)))
            except (TypeError, ValueError):
                continue
        return version + 1

    def create(
        self,
        graph: Any,
        messages: list[dict[str, Any]] | None = None,
        run_id: str = "",
        name: str = "",
        metadata: dict[str, Any] | None = None,
        environment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        graph_snapshot = graph.to_dict() if hasattr(graph, "to_dict") else dict(graph or {})
        name = name or str(graph_snapshot.get("name") or graph_snapshot.get("id") or "run")
        artifact_name = name
        version = self._next_version(artifact_name)
        artifact_id = f"run-{uuid.uuid4().hex[:16]}"
        msgs = [_message_record(m) for m in (messages or []) if isinstance(m, dict)]
        record = {
            "schema": SCHEMA,
            "artifact_id": artifact_id,
            "name": artifact_name,
            "run_id": run_id or artifact_id,
            "version": version,
            "created_at": time.time(),
            "graph": graph_snapshot,
            "environment": environment if environment is not None else snapshot_environment(),
            "conversation": {"count": len(msgs), "messages": msgs},
            "metadata": dict(metadata or {}),
        }
        tmp = self._path(artifact_id).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path(artifact_id))
        logger.info(
            "run artifact created id=%s name=%s version=%d run=%s messages=%d",
            artifact_id,
            artifact_name,
            version,
            record["run_id"],
            len(msgs),
        )
        return record

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        path = self._path(artifact_id)
        if not path.exists():
            logger.warning("run artifact not found: %s", artifact_id)
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("run artifact read failed id=%s: %s", artifact_id, exc)
            return None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            paths = sorted(
                self.root.glob("run-*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError as exc:
            logger.warning("run artifact list failed: %s", exc)
            return records
        if limit and limit > 0:
            paths = paths[:limit]
        for path in paths:
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("run artifact skip %s: %s", path.name, exc)
        return records

    def versions(self, name: str) -> list[dict[str, Any]]:
        recs = [r for r in self.list(limit=0) if r.get("name") == name]
        recs.sort(key=lambda r: int(r.get("version", 0)))
        return recs


_STORE: RunArtifactStore | None = None


def get_run_artifact_store() -> RunArtifactStore:
    global _STORE
    if _STORE is None:
        _STORE = RunArtifactStore()
    return _STORE


def record_run_artifact(
    graph: Any,
    messages: list[dict[str, Any]] | None = None,
    run_id: str = "",
    metadata: dict[str, Any] | None = None,
    store: RunArtifactStore | None = None,
) -> dict[str, Any] | None:
    try:
        return (store or get_run_artifact_store()).create(
            graph=graph, messages=messages, run_id=run_id, metadata=metadata
        )
    except Exception as exc:
        logger.warning("record_run_artifact failed: %s", exc)
        return None
