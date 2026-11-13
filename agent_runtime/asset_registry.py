"""E6: prompt/skill/agent-template asset version registry.

Append-only version chain in SQLite (studio_home()/assets.db): put() never
mutates an existing version, checkout() is idempotent and returns the latest
(or an exact pinned) version. Marketplace package format stays aligned —
content is addressed by sha256 so packages can pin exact versions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .paths import studio_home

logger = logging.getLogger(__name__)

_KINDS = ("prompt", "skill", "agent_template")


class AssetRegistry:
    def __init__(self, db_path: str | Path = ""):
        if not db_path:
            db_path = str(studio_home() / "assets.db")
        self.db_path = str(db_path)
        self._init_db()
        logger.info("asset registry ready: %s", self.db_path)

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS asset_versions ("
            "kind TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL, "
            "content_hash TEXT NOT NULL, content TEXT NOT NULL, "
            "created_by TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, "
            "PRIMARY KEY (kind, name, version))"
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def put(self, kind: str, name: str, content: str, created_by: str = "") -> dict:
        if kind not in _KINDS:
            raise ValueError(f"invalid asset kind {kind!r} (expected one of {_KINDS})")
        if not name:
            raise ValueError("asset name required")
        content = str(content)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT MAX(version) FROM asset_versions WHERE kind=? AND name=?",
                (kind, name),
            ).fetchone()
            version = int(row[0] or 0) + 1
            chash = self._hash(content)
            # Idempotent re-put of identical content: no new version.
            same = conn.execute(
                "SELECT version FROM asset_versions WHERE kind=? AND name=? AND content_hash=?",
                (kind, name, chash),
            ).fetchone()
            if same:
                logger.info("asset put idempotent %s/%s -> v%s", kind, name, same[0])
                return {"kind": kind, "name": name, "version": same[0], "content_hash": chash, "unchanged": True}
            conn.execute(
                "INSERT INTO asset_versions (kind, name, version, content_hash, content, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                (kind, name, version, chash, content, created_by, time.time()),
            )
            conn.commit()
            logger.info("asset put %s/%s v%d by=%s", kind, name, version, created_by or "?")
            return {"kind": kind, "name": name, "version": version, "content_hash": chash, "unchanged": False}
        finally:
            conn.close()

    def list(self, kind: str = "", name: str = "") -> list[dict]:
        q = "SELECT kind, name, version, content_hash, created_by, created_at FROM asset_versions"
        conds, args = [], []
        if kind:
            conds.append("kind=?")
            args.append(kind)
        if name:
            conds.append("name=?")
            args.append(name)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY kind, name, version"
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(q, args).fetchall()
        finally:
            conn.close()
        return [
            {
                "kind": r[0],
                "name": r[1],
                "version": r[2],
                "content_hash": r[3],
                "created_by": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

    def checkout(self, kind: str, name: str, version: Optional[int] = None) -> Optional[dict]:
        conn = sqlite3.connect(self.db_path)
        try:
            if version is None:
                row = conn.execute(
                    "SELECT version, content_hash, content, created_at FROM asset_versions "
                    "WHERE kind=? AND name=? ORDER BY version DESC LIMIT 1",
                    (kind, name),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT version, content_hash, content, created_at FROM asset_versions "
                    "WHERE kind=? AND name=? AND version=?",
                    (kind, name, int(version)),
                ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "kind": kind,
            "name": name,
            "version": row[0],
            "content_hash": row[1],
            "content": row[2],
            "created_at": row[3],
        }

    def verify(self, kind: str, name: str, version: int) -> bool:
        rec = self.checkout(kind, name, version)
        return rec is not None and rec["content_hash"] == self._hash(rec["content"])

    def to_json(self, records: list[dict]) -> str:
        return json.dumps(records, ensure_ascii=False)
