"""Database tools — SQLite and PostgreSQL query execution."""
from __future__ import annotations

import json
from .base import BaseTool


class SqliteQueryTool(BaseTool):
    name = "sqlite_query"
    description = "Execute SQL queries against a SQLite database"
    parameters = {
        "database": {"type": "string", "description": "Path to SQLite database file"},
        "query": {"type": "string", "description": "SQL query to execute"},
        "max_rows": {"type": "integer", "description": "Maximum rows to return", "default": 50},
    }

    async def execute(self, **kwargs) -> str:
        import sqlite3
        db_path = kwargs.get("database", "")
        query = kwargs.get("query", "")
        max_rows = int(kwargs.get("max_rows", 50))
        if not db_path:
            return "Error: database path is required"
        if not query:
            return "Error: query is required"
        query_upper = query.strip().upper()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query)
            if query_upper.startswith("SELECT") or query_upper.startswith("PRAGMA"):
                rows = cur.fetchmany(max_rows)
                if not rows:
                    return "Query returned no rows"
                columns = [d[0] for d in cur.description]
                result = [f"Columns: {', '.join(columns)}", f"Rows: {len(rows)}"]
                for i, row in enumerate(rows):
                    result.append(f"Row {i+1}: {dict(row)}")
                if len(cur.fetchmany(1)) > 0:
                    result.append(f"... (more rows available, showing first {max_rows})")
                return "\n".join(result)
            else:
                conn.commit()
                return f"Query executed. Rows affected: {cur.rowcount}"
        except Exception as e:
            return f"Error: {e}"
        finally:
            try:
                conn.close()
            except Exception:
                pass


class AnnotationNode(BaseTool):
    name = "annotation"
    description = "Add a text annotation or documentation note to the agent graph"
    parameters = {
        "text": {"type": "string", "description": "Annotation text content"},
        "style": {"type": "string", "enum": ["info", "warning", "note", "todo"], "description": "Annotation style", "default": "info"},
    }

    async def execute(self, **kwargs) -> str:
        text = kwargs.get("text", "")
        style = kwargs.get("style", "info")
        if not text:
            return "Error: text is required"
        return f"[{style.upper()}] {text}"


class PerformanceMonitor:
    """Tracks and reports agent execution performance metrics.

    Reads data from fusion-mlx's /stats endpoint and combines
    with agent-level metrics for a comprehensive view.
    """

    def __init__(self, mlx_client=None):
        from server.fusion_mlx_client import FusionMLXClient
        self.mlx = mlx_client or FusionMLXClient()
        self._metrics: list[dict] = []

    async def collect(self, agent_name: str = "") -> dict:
        """Collect current performance metrics."""
        import time
        stats = await self.mlx.get_server_stats()
        metric = {
            "timestamp": time.time(),
            "agent": agent_name,
            "models_loaded": stats.get("models_loaded", 0),
            "models_discovered": stats.get("models_discovered", 0),
            "total_requests": stats.get("total_requests", 0),
            "total_prompt_tokens": stats.get("total_prompt_tokens", 0),
            "total_completion_tokens": stats.get("total_tokens_generated", 0),
            "model_memory_used": stats.get("model_memory_used_formatted", "0B"),
            "model_memory_max": stats.get("model_memory_max_formatted", "unlimited"),
        }
        self._metrics.append(metric)
        if len(self._metrics) > 1000:
            self._metrics = self._metrics[-500:]
        return metric

    def history(self, limit: int = 50) -> list[dict]:
        return self._metrics[-limit:]

    def summary(self) -> dict:
        if not self._metrics:
            return {"error": "No metrics collected"}
        latest = self._metrics[-1]
        return {
            "models_loaded": latest["models_loaded"],
            "total_requests": latest["total_requests"],
            "total_tokens": latest["total_prompt_tokens"] + latest["total_completion_tokens"],
            "memory": latest["model_memory_used"],
        }

    def to_html(self) -> str:
        """Generate a simple HTML dashboard snippet."""
        s = self.summary()
        if "error" in s:
            return "<div class='perf-error'>No metrics available</div>"
        return f"""
        <div class="perf-dashboard">
            <div class="perf-card"><span class="perf-label">Models</span><span class="perf-value">{s['models_loaded']}</span></div>
            <div class="perf-card"><span class="perf-label">Requests</span><span class="perf-value">{s['total_requests']}</span></div>
            <div class="perf-card"><span class="perf-label">Tokens</span><span class="perf-value">{s['total_tokens']:,}</span></div>
            <div class="perf-card"><span class="perf-label">Memory</span><span class="perf-value">{s['memory']}</span></div>
        </div>"""