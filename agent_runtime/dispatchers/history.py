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
        }

    def _store(self):
        store = getattr(self._daemon, "_run_history_store", None)
        if store is None:
            from ..run_history import RunHistoryStore

            store = RunHistoryStore()
            self._daemon._run_history_store = store
            logger.info("run history store lazily created on daemon")
        return store

    def _handle_history_list(self, params: dict) -> dict:
        graph_id = str(params.get("graph_id", "") or "")
        limit = int(params.get("limit", 20) or 20)
        runs = self._store().list_runs(graph_id=graph_id, limit=limit)
        return {"runs": runs}

    def _handle_history_get(self, params: dict) -> dict:
        run_id = str(params.get("run_id", "") or "")
        run = self._store().load_run(run_id)
        if run is None:
            return {"error": f"run '{run_id}' not found in history"}
        return {"run": run}

    def _handle_run_diff(self, params: dict) -> dict:
        run_a = str(params.get("run_a", "") or "")
        run_b = str(params.get("run_b", "") or "")
        diff = self._store().diff_runs(run_a, run_b)
        if diff is None:
            return {"error": f"diff failed: run '{run_a}' or '{run_b}' not found in history"}
        return {"diff": diff}
