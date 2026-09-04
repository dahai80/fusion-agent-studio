from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from .trajectory_writer import TRAJECTORY_DIR, get_trajectory_writer

logger = logging.getLogger(__name__)

_DATASET_DIR = Path.home() / ".fusion" / "trainer" / "datasets"
_RUNS: dict[str, dict[str, Any]] = {}


def _export_sft_dataset(trace_ids: list[str] | None = None) -> tuple[str, int]:
    _DATASET_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(TRAJECTORY_DIR.glob("*.json"), reverse=True)
    records: list[dict[str, Any]] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip unreadable trajectory %s: %s", f.name, e)
            continue
        tid = data.get("trace_id", "")
        if trace_ids and tid not in trace_ids:
            continue
        messages = data.get("messages", [])
        if len(messages) < 2:
            logger.info("skip trajectory %s: only %d messages", tid, len(messages))
            continue
        norm = [
            {"role": m.get("role", ""), "content": m.get("content", "")}
            for m in messages
            if m.get("role") in ("system", "user", "assistant")
            and m.get("content")
        ]
        if len(norm) < 2:
            continue
        records.append({"messages": norm})
    stamp = int(time.time() * 1000)
    out = _DATASET_DIR / f"sft_{stamp}_{uuid.uuid4().hex[:6]}.jsonl"
    with open(out, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    logger.info("exported %d SFT samples -> %s", len(records), out)
    return str(out), len(records)


async def _run_sft(dataset: str, model: str, preset: str | None) -> dict[str, Any]:
    from fusion_trainer import TrainerConfig
    from fusion_trainer.sft import SFT_PRESETS, SFTTrainer

    cfg = TrainerConfig()
    cfg.dataset.path = dataset
    cfg.sft.model = model
    if preset and preset in SFT_PRESETS:
        p = SFT_PRESETS[preset]
        cfg.sft.lora_rank = p.get("lora_rank", cfg.sft.lora_rank)
        cfg.sft.num_epochs = p.get("num_epochs", cfg.sft.num_epochs)
        cfg.sft.batch_size = p.get("batch_size", cfg.sft.batch_size)
        logger.info("applied preset %s to sft config", preset)
    trainer = SFTTrainer(cfg)
    await asyncio.to_thread(trainer.train)
    return {"method": "sft", "output_dir": str(cfg.sft.output_dir)}


async def _run_rlsl(
    dataset: str, model: str, method: str, preset: str | None
) -> dict[str, Any]:
    from fusion_trainer import TrainerConfig
    from fusion_trainer.rlsl import RLSL_PRESETS, RLSLEngine

    cfg = TrainerConfig()
    cfg.dataset.path = dataset
    cfg.sft.model = model
    cfg.rlsl.method = method
    if preset and preset in RLSL_PRESETS:
        p = RLSL_PRESETS[preset]
        cfg.rlsl.group_size = p.get("group_size", cfg.rlsl.group_size)
        cfg.rlsl.learning_rate = p.get("learning_rate", cfg.rlsl.learning_rate)
        logger.info("applied preset %s to rlsl config", preset)
    engine = RLSLEngine(cfg)
    await asyncio.to_thread(engine.run)
    return {"method": method, "output_dir": str(cfg.rlsl.output_dir)}


class TrainingService:
    def list_trajectories(self, limit: int = 50) -> list[dict[str, Any]]:
        return get_trajectory_writer().list_trajectories(limit=limit)

    def export_sft_dataset(
        self, trace_ids: list[str] | None = None
    ) -> dict[str, Any]:
        path, count = _export_sft_dataset(trace_ids)
        return {"dataset": path, "count": count}

    def run_sft(
        self,
        model: str,
        trace_ids: list[str] | None = None,
        preset: str | None = None,
        dataset: str | None = None,
    ) -> dict[str, Any]:
        ds = dataset
        if not ds:
            ds, _ = _export_sft_dataset(trace_ids)
        run_id = uuid.uuid4().hex[:12]
        _RUNS[run_id] = {
            "run_id": run_id,
            "method": "sft",
            "status": "running",
            "started": time.time(),
            "model": model,
            "dataset": ds,
        }
        logger.info("run_sft registered run_id=%s model=%s", run_id, model)

        async def _task() -> None:
            try:
                res = await _run_sft(ds, model, preset)
                _RUNS[run_id].update({"status": "completed", **res})
                logger.info("run_sft completed run_id=%s", run_id)
            except Exception as e:
                _RUNS[run_id].update({"status": "error", "error": str(e)})
                logger.exception("run_sft failed run_id=%s: %s", run_id, e)

        return {"run_id": run_id, "status": "running", "dataset": ds, "_task": _task}

    def run_rlsl(
        self,
        model: str,
        method: str,
        trace_ids: list[str] | None = None,
        preset: str | None = None,
        dataset: str | None = None,
    ) -> dict[str, Any]:
        if method not in ("dpo", "orpo", "grpo"):
            return {"error": "method must be dpo|orpo|grpo"}
        ds = dataset
        if not ds:
            ds, _ = _export_sft_dataset(trace_ids)
        run_id = uuid.uuid4().hex[:12]
        _RUNS[run_id] = {
            "run_id": run_id,
            "method": method,
            "status": "running",
            "started": time.time(),
            "model": model,
            "dataset": ds,
        }
        logger.info("run_rlsl registered run_id=%s model=%s method=%s", run_id, model, method)

        async def _task() -> None:
            try:
                res = await _run_rlsl(ds, model, method, preset)
                _RUNS[run_id].update({"status": "completed", **res})
                logger.info("run_rlsl completed run_id=%s", run_id)
            except Exception as e:
                _RUNS[run_id].update({"status": "error", "error": str(e)})
                logger.exception("run_rlsl failed run_id=%s: %s", run_id, e)

        return {"run_id": run_id, "status": "running", "dataset": ds, "_task": _task}

    def run_status(self, run_id: str) -> dict[str, Any]:
        run = _RUNS.get(run_id)
        if run is None:
            return {"error": "unknown run_id"}
        return dict(run)

    # 审计 P1-22/NEW-2: task-level 兜底 mark (done_callback 用). _task 体内已
    # try/except 记 error, 此处理体外漏网 (coro 启动即崩 / CancelledError).
    def mark_run_failed(self, run_id: str, err: str) -> None:
        run = _RUNS.get(run_id)
        if run is not None and run.get("status") == "running":
            run.update({"status": "error", "error": err})
            logger.error("training run %s marked failed (task-level): %s", run_id, err)

    def mark_run_cancelled(self, run_id: str) -> None:
        run = _RUNS.get(run_id)
        if run is not None and run.get("status") == "running":
            run.update({"status": "cancelled"})
            logger.warning("training run %s marked cancelled", run_id)

    def info(self) -> dict[str, Any]:
        try:
            import fusion_trainer

            return {"version": fusion_trainer.__version__}
        except ImportError as e:
            logger.warning("fusion_trainer not importable: %s", e)
            return {"error": "fusion_trainer not installed"}

    # ------------------------------------------------------------------
    # RunManager-backed surface (live progress + full config + run history).
    # Kept separate from the legacy _RUNS dict above so existing callers
    # keep working; the GUI uses these for real loss curves + run registry.
    # ------------------------------------------------------------------

    def run_manager(self):
        rm = getattr(self, "_run_manager", None)
        if rm is None:
            from fusion_trainer.run_manager import get_run_manager

            rm = get_run_manager()
            self._run_manager = rm
        return rm

    def start_sft_cfg(self, config_dict: dict[str, Any]) -> dict[str, Any]:
        from fusion_trainer.config import (
            DatasetConfig,
            RLSLConfig,
            SFTConfig,
            TrainerConfig,
        )

        cfg = _build_trainer_config(
            config_dict, DatasetConfig, RLSLConfig, SFTConfig, TrainerConfig
        )
        return self.run_manager().start_sft(cfg)

    def start_rlsl_cfg(self, config_dict: dict[str, Any]) -> dict[str, Any]:
        from fusion_trainer.config import (
            DatasetConfig,
            RLSLConfig,
            SFTConfig,
            TrainerConfig,
        )

        cfg = _build_trainer_config(
            config_dict, DatasetConfig, RLSLConfig, SFTConfig, TrainerConfig
        )
        return self.run_manager().start_rlsl(cfg)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.run_manager().list_runs(limit=limit)

    def run_status_rm(self, run_id: str) -> dict[str, Any]:
        return self.run_manager().run_status(run_id)

    def run_progress(self, run_id: str, since_step: int = -1) -> dict[str, Any]:
        return self.run_manager().run_progress(run_id, since_step=since_step)

    def stop_run(self, run_id: str) -> dict[str, Any]:
        return self.run_manager().stop_run(run_id)

    def list_presets(self, kind: str = "") -> list[dict[str, Any]]:
        return self.run_manager().list_presets(kind=kind)

    def list_datasets(self) -> list[dict[str, Any]]:
        return self.run_manager().list_datasets()

    def preview_dataset(self, name: str, limit: int = 5) -> dict[str, Any]:
        return self.run_manager().preview_dataset(name, limit=limit)

    def list_adapters(self, model: str = "") -> list[dict[str, Any]]:
        return self.run_manager().list_adapters(model=model)

    def delete_adapter(self, name: str) -> dict[str, Any]:
        return self.run_manager().delete_adapter(name)

    def info_full(self) -> dict[str, Any]:
        return self.run_manager().info()


def _build_trainer_config(
    d: dict[str, Any], DatasetConfig, RLSLConfig, SFTConfig, TrainerConfig
) -> Any:
    """Deserialize a TrainerConfig JSON payload into a TrainerConfig instance.

    Sub-dicts (dataset/sft/rlsl) are rebuilt from their dataclasses; missing
    keys fall back to dataclass defaults. Non-dataclass scalars (seed,
    model_hub_path, cache_path, mlx_port) are passed through.
    """
    dataset = DatasetConfig(**(d.get("dataset") or {}))
    sft = SFTConfig(**(d.get("sft") or {}))
    rlsl = RLSLConfig(**(d.get("rlsl") or {}))
    return TrainerConfig(
        dataset=dataset,
        sft=sft,
        rlsl=rlsl,
        seed=d.get("seed", 42),
        model_hub_path=d.get("model_hub_path", ""),
        cache_path=d.get("cache_path", ""),
        mlx_port=d.get("mlx_port"),
        # #277: publish-to-hub fields — forward from RPC payload so GUI-triggered
        # runs can publish LoRA adapters. hub_api_key is a secret: never log its value.
        publish_adapter=bool(d.get("publish_adapter", False)),
        hub_url=d.get("hub_url", ""),
        hub_api_key=d.get("hub_api_key", ""),
    )
