"""#558: HistoryDispatcher — run history archive RPCs (run.history_*)."""

from __future__ import annotations

import logging

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class HistoryDispatcher(SubDispatcher):
    def get_handlers(self) -> dict:
        return {
            "run.history_list": self._handle_history_list,
            "run.history_get": self._handle_history_get,
            "run.diff": self._handle_run_diff,
            # E4: Inspector data-plane aggregations (managed-run buffer first,
            # then run history archive).
            "runs.tool_stats": self._handle_tool_stats,
            "runs.thread_lanes": self._handle_thread_lanes,
            # E5: offline permission reviewer over a run's event stream.
            "runs.offlineReview": self._handle_offline_review,
        }

    def _resolve_run_events(self, run_id: str) -> list[dict] | None:
        registry = getattr(self._daemon, "_managed_run_registry", None)
        if registry is not None:
            run = registry.get(run_id)
            if run is not None:
                return list(run.buffer)
        run = self._store().load_run(run_id)
        if run is None:
            return None
        return run.get("events") or []

    async def _handle_tool_stats(self, params: dict) -> dict:
        run_id = str(params.get("run_id", "") or "")
        events = self._resolve_run_events(run_id)
        if events is None:
            return {"error": f"run '{run_id}' not found"}
        from ..inspector import tool_stats

        return {"run_id": run_id, "tools": tool_stats(events)}

    async def _handle_thread_lanes(self, params: dict) -> dict:
        run_id = str(params.get("run_id", "") or "")
        events = self._resolve_run_events(run_id)
        if events is None:
            return {"error": f"run '{run_id}' not found"}
        from ..inspector import thread_lanes

        out = thread_lanes(events)
        out["run_id"] = run_id
        return out

    async def _handle_offline_review(self, params: dict) -> dict:
        from ..offline_reviewer import OfflineReviewer, default_rules

        run_id = str(params.get("run_id", "") or "")
        events = self._resolve_run_events(run_id)
        if events is None:
            return {"error": f"run '{run_id}' not found"}
        rules = params.get("rules") if isinstance(params.get("rules"), list) else default_rules()
        reviewer = OfflineReviewer(
            rules=rules,
            mlx_client=getattr(self._daemon, "mlx_client", None),
            audit_logger=self._daemon._get_audit_logger() if hasattr(self._daemon, "_get_audit_logger") else None,
        )
        report = await reviewer.review_run(run_id, events)
        return {"report": report}

    def _store(self):
        store = getattr(self._daemon, "_run_history_store", None)
        if store is None:
            from ..run_history import RunHistoryStore

            store = RunHistoryStore()
            self._daemon._run_history_store = store
            logger.info("run history store lazily created on daemon")
        return store

    async def _handle_history_list(self, params: dict) -> dict:
        graph_id = str(params.get("graph_id", "") or "")
        limit = int(params.get("limit", 20) or 20)
        runs = self._store().list_runs(graph_id=graph_id, limit=limit)
        return {"runs": runs}

    async def _handle_history_get(self, params: dict) -> dict:
        run_id = str(params.get("run_id", "") or "")
        run = self._store().load_run(run_id)
        if run is None:
            return {"error": f"run '{run_id}' not found in history"}
        return {"run": run}

    async def _handle_run_diff(self, params: dict) -> dict:
        run_a = str(params.get("run_a", "") or "")
        run_b = str(params.get("run_b", "") or "")
        diff = self._store().diff_runs(run_a, run_b)
        if diff is None:
            return {"error": f"diff failed: run '{run_a}' or '{run_b}' not found in history"}
        return {"diff": diff}
