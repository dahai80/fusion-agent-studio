from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class TrainerDispatcher(SubDispatcher):
    def __init__(self, daemon):
        super().__init__(daemon)
        # 审计 P1-22/NEW-2: 持 training task 引用防 GC 静默取消 + done_callback
        # 兜底 mark failed (CancelledError / _task 体外异常). key=run_id.
        self._training_tasks: dict[str, asyncio.Task] = {}

    def _spawn_training_task(self, run_id: str, coro) -> None:
        # 审计 P1-22/NEW-2: 原 asyncio.create_task() fire-and-forget — 无引用
        # 可被 GC 静默取消, 异常吞没, run 状态恒 "running". 现持引用 + done_callback.
        task = asyncio.create_task(coro)
        self._training_tasks[run_id] = task

        def _on_done(t: asyncio.Task) -> None:
            self._training_tasks.pop(run_id, None)
            if t.cancelled():
                logger.warning("training run %s cancelled", run_id)
                try:
                    svc = self._daemon._training_service
                    if svc is not None:
                        svc.mark_run_cancelled(run_id)
                except Exception:
                    pass
                return
            exc = t.exception()
            if exc is not None:
                # _task 体已 try/except 记 _RUNS, 此为体外漏网 (如 coro 启动即崩).
                logger.error("training run %s task-level exception: %s", run_id, exc)
                try:
                    svc = self._daemon._training_service
                    if svc is not None:
                        svc.mark_run_failed(run_id, str(exc))
                except Exception:
                    pass

        task.add_done_callback(_on_done)

    def get_handlers(self) -> dict[str, Callable]:
        return {
            "trainer.trajectories.list": self._handle_trajectories_list,
            "trainer.sft": self._handle_sft,
            "trainer.rlsl": self._handle_rlsl,
            "trainer.info": self._handle_info,
            "trainer.runs.status": self._handle_runs_status,
            # RunManager-backed surface (live progress + full config + registry).
            "trainer.start_sft": self._handle_start_sft,
            "trainer.start_rlsl": self._handle_start_rlsl,
            "trainer.runs.list": self._handle_runs_list,
            "trainer.runs.status_full": self._handle_runs_status_full,
            "trainer.runs.progress": self._handle_runs_progress,
            "trainer.runs.stop": self._handle_runs_stop,
            "trainer.presets.list": self._handle_presets_list,
            "trainer.datasets.list": self._handle_datasets_list,
            "trainer.datasets.preview": self._handle_datasets_preview,
            "trainer.adapters.list": self._handle_adapters_list,
            "trainer.adapters.delete": self._handle_adapters_delete,
            "trainer.info_full": self._handle_info_full,
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
        # 审计 P1-22/NEW-2: 持引用 + done_callback, 防 GC 静默取消/异常吞没.
        self._spawn_training_task(result["run_id"], result["_task"]())
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
        # 审计 P1-22/NEW-2: 持引用 + done_callback, 同 sft.
        self._spawn_training_task(result["run_id"], result["_task"]())
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

    # ------------------------------------------------------------------
    # RunManager-backed handlers (live progress + full config + registry).
    # ------------------------------------------------------------------

    async def _handle_start_sft(self, params: dict) -> dict:
        config_dict = params.get("config")
        if not isinstance(config_dict, dict):
            return self._err("config parameter (TrainerConfig dict) required")
        result = self._service().start_sft_cfg(config_dict)
        if "error" in result:
            return self._err(result["error"])
        logger.info("trainer.start_sft -> %s", result.get("run_id"))
        return self._ok(result)

    async def _handle_start_rlsl(self, params: dict) -> dict:
        config_dict = params.get("config")
        if not isinstance(config_dict, dict):
            return self._err("config parameter (TrainerConfig dict) required")
        result = self._service().start_rlsl_cfg(config_dict)
        if "error" in result:
            return self._err(result["error"])
        logger.info("trainer.start_rlsl -> %s", result.get("run_id"))
        return self._ok(result)

    async def _handle_runs_list(self, params: dict) -> dict:
        limit = int(params.get("limit", 50))
        if limit < 1 or limit > 500:
            return self._err("limit must be between 1 and 500")
        return self._ok({"runs": self._service().list_runs(limit=limit)})

    async def _handle_runs_status_full(self, params: dict) -> dict:
        run_id = params.get("run_id", "")
        if not run_id:
            return self._err("run_id parameter required")
        return self._ok(self._service().run_status_rm(run_id))

    async def _handle_runs_progress(self, params: dict) -> dict:
        run_id = params.get("run_id", "")
        if not run_id:
            return self._err("run_id parameter required")
        since_step = int(params.get("since_step", -1))
        return self._ok(self._service().run_progress(run_id, since_step=since_step))

    async def _handle_runs_stop(self, params: dict) -> dict:
        run_id = params.get("run_id", "")
        if not run_id:
            return self._err("run_id parameter required")
        return self._ok(self._service().stop_run(run_id))

    async def _handle_presets_list(self, params: dict) -> dict:
        kind = params.get("kind", "")
        return self._ok({"presets": self._service().list_presets(kind=kind)})

    async def _handle_datasets_list(self, params: dict) -> dict:
        return self._ok({"datasets": self._service().list_datasets()})

    async def _handle_datasets_preview(self, params: dict) -> dict:
        name = params.get("name", "")
        if not name:
            return self._err("name parameter required")
        limit = int(params.get("limit", 5))
        return self._ok(self._service().preview_dataset(name, limit=limit))

    async def _handle_adapters_list(self, params: dict) -> dict:
        return self._ok({"adapters": self._service().list_adapters(model=params.get("model", ""))})

    async def _handle_adapters_delete(self, params: dict) -> dict:
        name = params.get("name", "")
        if not name:
            return self._err("name parameter required")
        return self._ok(self._service().delete_adapter(name))

    async def _handle_info_full(self, params: dict) -> dict:
        return self._ok(self._service().info_full())
