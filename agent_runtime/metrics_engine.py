"""Metrics engine — inference performance metrics collection and history.

Collects fusion-mlx performance data (VRAM, throughput, latency),
session execution history, and provides aggregation/query APIs.
All metrics stored in SQLite for zero-dependency persistence.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".fusion-agent-studio" / "metrics.db"


@dataclass
class InferenceMetrics:
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    vram_mb: float = 0.0
    throughput_tps: float = 0.0
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms,
            "vram_mb": self.vram_mb,
            "throughput_tps": self.throughput_tps,
            "timestamp": self.timestamp,
        }


@dataclass
class SessionRecord:
    session_id: str
    graph_id: str = ""
    status: str = ""
    input_text: str = ""
    output_text: str = ""
    duration_ms: float = 0.0
    node_count: int = 0
    error: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "graph_id": self.graph_id,
            "status": self.status,
            "input_text": self.input_text,
            "output_text": self.output_text,
            "duration_ms": self.duration_ms,
            "node_count": self.node_count,
            "error": self.error,
            "timestamp": self.timestamp,
        }


@dataclass
class MetricsSummary:
    total_inferences: int = 0
    total_sessions: int = 0
    avg_latency_ms: float = 0.0
    avg_throughput_tps: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    peak_vram_mb: float = 0.0
    success_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_inferences": self.total_inferences,
            "total_sessions": self.total_sessions,
            "avg_latency_ms": self.avg_latency_ms,
            "avg_throughput_tps": self.avg_throughput_tps,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "peak_vram_mb": self.peak_vram_mb,
            "success_rate": self.success_rate,
        }


class MetricsEngine:
    """SQLite-backed metrics collection and query engine."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS inference_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT NOT NULL,
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                latency_ms REAL DEFAULT 0,
                vram_mb REAL DEFAULT 0,
                throughput_tps REAL DEFAULT 0,
                timestamp REAL NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS session_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                graph_id TEXT DEFAULT '',
                status TEXT DEFAULT '',
                input_text TEXT DEFAULT '',
                output_text TEXT DEFAULT '',
                duration_ms REAL DEFAULT 0,
                node_count INTEGER DEFAULT 0,
                error TEXT DEFAULT '',
                timestamp REAL NOT NULL
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_inf_ts ON inference_metrics(timestamp)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_sess_ts ON session_records(timestamp)")
        self._conn.commit()
        logger.info("Metrics DB initialized: %s", self.db_path)

    def record_inference(self, metrics: InferenceMetrics) -> int:
        if not metrics.timestamp:
            metrics.timestamp = time.time()
        cur = self._conn.execute("""
            INSERT INTO inference_metrics (model, tokens_in, tokens_out, latency_ms, vram_mb, throughput_tps, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (metrics.model, metrics.tokens_in, metrics.tokens_out, metrics.latency_ms,
              metrics.vram_mb, metrics.throughput_tps, metrics.timestamp))
        self._conn.commit()
        logger.debug("Recorded inference: model=%s latency=%.1fms", metrics.model, metrics.latency_ms)
        return cur.lastrowid

    def record_session(self, record: SessionRecord) -> int:
        if not record.timestamp:
            record.timestamp = time.time()
        cur = self._conn.execute("""
            INSERT INTO session_records (session_id, graph_id, status, input_text, output_text,
                                         duration_ms, node_count, error, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (record.session_id, record.graph_id, record.status, record.input_text,
              record.output_text, record.duration_ms, record.node_count, record.error,
              record.timestamp))
        self._conn.commit()
        logger.debug("Recorded session: %s status=%s", record.session_id, record.status)
        return cur.lastrowid

    def query_inferences(self, model: str = "", since: float = 0, limit: int = 100) -> list[InferenceMetrics]:
        query = "SELECT model, tokens_in, tokens_out, latency_ms, vram_mb, throughput_tps, timestamp FROM inference_metrics WHERE 1=1"
        params: list[Any] = []
        if model:
            query += " AND model = ?"
            params.append(model)
        if since:
            query += " AND timestamp >= ?"
            params.append(since)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [InferenceMetrics(**dict(zip(
            ["model", "tokens_in", "tokens_out", "latency_ms", "vram_mb", "throughput_tps", "timestamp"], r
        ))) for r in rows]

    def query_sessions(self, status: str = "", since: float = 0, limit: int = 100) -> list[SessionRecord]:
        query = "SELECT session_id, graph_id, status, input_text, output_text, duration_ms, node_count, error, timestamp FROM session_records WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if since:
            query += " AND timestamp >= ?"
            params.append(since)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [SessionRecord(**dict(zip(
            ["session_id", "graph_id", "status", "input_text", "output_text",
             "duration_ms", "node_count", "error", "timestamp"], r
        ))) for r in rows]

    def get_summary(self, since: float = 0) -> MetricsSummary:
        params: list[Any] = []
        where = "WHERE 1=1"
        if since:
            where += " AND timestamp >= ?"
            params.append(since)

        inf_row = self._conn.execute(
            f"SELECT COUNT(*), AVG(latency_ms), AVG(throughput_tps), SUM(tokens_in), SUM(tokens_out), MAX(vram_mb) FROM inference_metrics {where}",
            params
        ).fetchone()

        sess_where = "WHERE 1=1"
        sess_params: list[Any] = []
        if since:
            sess_where += " AND timestamp >= ?"
            sess_params.append(since)

        sess_row = self._conn.execute(
            f"SELECT COUNT(*), SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) FROM session_records {sess_where}",
            sess_params
        ).fetchone()

        total_inf = inf_row[0] or 0
        total_sess = sess_row[0] or 0
        completed = sess_row[1] or 0

        return MetricsSummary(
            total_inferences=total_inf,
            total_sessions=total_sess,
            avg_latency_ms=round(inf_row[1] or 0, 2),
            avg_throughput_tps=round(inf_row[2] or 0, 2),
            total_tokens_in=inf_row[3] or 0,
            total_tokens_out=inf_row[4] or 0,
            peak_vram_mb=round(inf_row[5] or 0, 2),
            success_rate=round(completed / total_sess, 4) if total_sess > 0 else 0.0,
        )

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("Metrics DB closed")
