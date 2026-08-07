from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class TrainerDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "trainer.trajectories.list": self._handle_trajectories_list,
            "trainer.sft": self._handle_sft,
            "trainer.rlsl": self._handle_rlsl,
            "trainer.info": self._handle_info,
            "trainer.runs.status": self._handle_runs_status,
        }

    def _service(self):
        from ..training_service import TrainingService

        svc = getattr(self._daemon, "_training_service", None)
        if svc is None:
            svc = TrainingService()
            self._daemon._training_service = svc
        return svc

    async def _handle_trajectories_list(self, params: dict) -> dict:
        limit = int(params.get("limit", 50))
        if limit < 1 or limit > 500:
            return self._err("limit must be between 1 and 500")
        items = self._service().list_trajectories(limit=limit)
        logger.info("trainer.trajectories.list -> %d items", len(items))
        return self._ok({"trajectories": items, "count": len(items)})

    async def _handle_sft(self, params: dict) -> dict:
        model = params.get("model", "")
        if not model:
            return self._err("model parameter required")
        result = self._service().run_sft(
            model=model,
            trace_ids=params.get("trace_ids"),
            preset=params.get("preset"),
            dataset=params.get("dataset"),
        )
        if "error" in result:
            return self._err(result["error"])
        asyncio.create_task(result["_task"]())
        logger.info("trainer.sft -> %s", result["run_id"])
        return self._ok(
            {"run_id": result["run_id"], "status": result["status"], "dataset": result["dataset"]}
        )

    async def _handle_rlsl(self, params: dict) -> dict:
        model = params.get("model", "")
        if not model:
            return self._err("model parameter required")
        method = params.get("method", "")
        if not method:
            return self._err("method parameter required (dpo|orpo|grpo)")
        result = self._service().run_rlsl(
            model=model,
            method=method,
            trace_ids=params.get("trace_ids"),
            preset=params.get("preset"),
            dataset=params.get("dataset"),
        )
        if "error" in result:
            return self._err(result["error"])
        asyncio.create_task(result["_task"]())
        logger.info("trainer.rlsl method=%s -> %s", method, result["run_id"])
        return self._ok(
            {"run_id": result["run_id"], "status": result["status"], "dataset": result["dataset"]}
        )

    async def _handle_info(self, params: dict) -> dict:
        return self._ok(self._service().info())

    async def _handle_runs_status(self, params: dict) -> dict:
        run_id = params.get("run_id", "")
        if not run_id:
            return self._err("run_id parameter required")
        return self._ok(self._service().run_status(run_id))
