"""E2: eval RPC surface — eval.run / eval.listDatasets / eval.fromTrace."""

from __future__ import annotations

import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class EvalDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "eval.run": self._handle_eval_run,
            "eval.listDatasets": self._handle_eval_list,
            "eval.fromTrace": self._handle_eval_from_trace,
        }

    def _get_runner(self):
        from ..eval_runner import EvalRunner

        daemon = self._daemon
        return EvalRunner(
            runtime=getattr(daemon, "runtime", None),
            graph_loader=lambda gid: daemon._get_store().load_graph(gid),
            judge_client=getattr(daemon, "mlx_client", None),
        )

    async def _handle_eval_run(self, params: dict) -> dict:
        from ..eval_runner import EvalDataset

        dataset_data = params.get("dataset")
        dataset_path = str(params.get("dataset_path", "") or "")
        graph_id = str(params.get("graph_id", "") or "")
        limit = int(params.get("limit", 0) or 0)
        runner = self._get_runner()
        if dataset_path:
            dataset = EvalDataset.load_yaml(dataset_path)
        elif isinstance(dataset_data, dict):
            dataset = EvalDataset.from_dict(dataset_data)
        else:
            return {"error": "dataset (object) or dataset_path required"}
        report = await runner.run(dataset, graph_id=graph_id, limit=limit)
        return report.to_dict()

    def _handle_eval_list(self, params: dict) -> dict:
        return {"datasets": self._get_runner().list_datasets()}

    async def _handle_eval_from_trace(self, params: dict) -> dict:
        trace_id = str(params.get("trace_id", "") or "")
        if not trace_id:
            return {"error": "trace_id required"}
        case = self._get_runner().from_trace(trace_id)
        if case is None:
            return {"error": f"trajectory not found: {trace_id}"}
        return {"case": case.to_dict()}
