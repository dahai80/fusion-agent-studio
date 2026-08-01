import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AuditEntry:
    id: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    timestamp: float
    ip: str
    details: dict = field(default_factory=dict)
    result: str = "success"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "actor_id": self.actor_id,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "timestamp": self.timestamp,
            "ip": self.ip,
            "details": self.details,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuditEntry":
        return cls(
            id=data["id"],
            actor_id=data["actor_id"],
            action=data["action"],
            resource_type=data["resource_type"],
            resource_id=data["resource_id"],
            timestamp=data["timestamp"],
            ip=data["ip"],
            details=data.get("details", {}),
            result=data.get("result", "success"),
        )


class AuditLogger:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(Path.home() / ".fusion-agent-studio" / "audit.db")
        self.db_path = db_path
        logger.info("AuditLogger initialized with db_path=%s", self.db_path)
        self._init_db()

    def _init_db(self):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT,
                    action TEXT,
                    resource_type TEXT,
                    resource_id TEXT,
                    timestamp REAL,
                    ip TEXT,
                    details TEXT,
                    result TEXT
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_actor_id ON audit_log (actor_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log (action)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log (timestamp)"
            )
            conn.commit()
            conn.close()
            logger.info("Audit database initialized at %s", self.db_path)
        except sqlite3.Error as e:
            logger.error("Failed to initialize audit database: %s", e)
            raise

    def log_action(
        self,
        actor_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        ip: str = "",
        details: Optional[dict] = None,
        result: str = "success",
    ) -> AuditEntry:
        entry = AuditEntry(
            id=str(uuid.uuid4()),
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            timestamp=time.time(),
            ip=ip,
            details=details or {},
            result=result,
        )
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO audit_log
                   (id, actor_id, action, resource_type, resource_id, timestamp, ip, details, result)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.id,
                    entry.actor_id,
                    entry.action,
                    entry.resource_type,
                    entry.resource_id,
                    entry.timestamp,
                    entry.ip,
                    json.dumps(entry.details),
                    entry.result,
                ),
            )
            conn.commit()
            conn.close()
            logger.info(
                "Audit log entry: actor=%s action=%s resource=%s/%s result=%s",
                actor_id,
                action,
                resource_type,
                resource_id,
                result,
            )
        except sqlite3.Error as e:
            logger.error("Failed to write audit log entry: %s", e)
            raise
        return entry

    def query_logs(
        self,
        actor_id: Optional[str] = None,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict:
        conditions = []
        params = []

        if actor_id is not None:
            conditions.append("actor_id = ?")
            params.append(actor_id)
        if action is not None:
            conditions.append("action = ?")
            params.append(action)
        if resource_type is not None:
            conditions.append("resource_type = ?")
            params.append(resource_type)
        if resource_id is not None:
            conditions.append("resource_id = ?")
            params.append(resource_id)
        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            conditions.append("timestamp <= ?")
            params.append(until)

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            count_sql = f"SELECT COUNT(*) FROM audit_log {where_clause}"
            cursor.execute(count_sql, params)
            total = cursor.fetchone()[0]

            offset = (page - 1) * limit
            query_sql = (
                f"SELECT id, actor_id, action, resource_type, resource_id, "
                f"timestamp, ip, details, result "
                f"FROM audit_log {where_clause} "
                f"ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            )
            cursor.execute(query_sql, params + [limit, offset])

            rows = cursor.fetchall()
            conn.close()

            data = []
            for row in rows:
                entry_dict = {
                    "id": row[0],
                    "actor_id": row[1],
                    "action": row[2],
                    "resource_type": row[3],
                    "resource_id": row[4],
                    "timestamp": row[5],
                    "ip": row[6],
                    "details": json.loads(row[7]) if row[7] else {},
                    "result": row[8],
                }
                data.append(entry_dict)

            logger.info(
                "Audit query returned %d/%d results (page=%d, limit=%d)",
                len(data),
                total,
                page,
                limit,
            )

            return {
                "data": data,
                "total": total,
                "page": page,
                "limit": limit,
            }
        except sqlite3.Error as e:
            logger.error("Failed to query audit logs: %s", e)
            raise

    def export_logs(
        self,
        format: str = "json",
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> list[dict]:
        conditions = []
        params = []

        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            conditions.append("timestamp <= ?")
            params.append(until)

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            export_sql = (
                f"SELECT id, actor_id, action, resource_type, resource_id, "
                f"timestamp, ip, details, result "
                f"FROM audit_log {where_clause} "
                f"ORDER BY timestamp ASC"
            )
            cursor.execute(export_sql, params)
            rows = cursor.fetchall()
            conn.close()

            result = []
            for row in rows:
                entry_dict = {
                    "id": row[0],
                    "actor_id": row[1],
                    "action": row[2],
                    "resource_type": row[3],
                    "resource_id": row[4],
                    "timestamp": row[5],
                    "ip": row[6],
                    "details": json.loads(row[7]) if row[7] else {},
                    "result": row[8],
                }
                result.append(entry_dict)

            logger.info("Exported %d audit log entries (format=%s)", len(result), format)
            return result
        except sqlite3.Error as e:
            logger.error("Failed to export audit logs: %s", e)
            raise
