import asyncio
import json
import os
import tempfile

from agent_runtime.dispatchers import TrainerDispatcher
from agent_runtime.dispatchers.trainer import TrainerDispatcher as TD2
from agent_runtime.training_service import TrainingService


def _write_pref_dataset() -> str:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "pref.jsonl")
    with open(p, "w") as f:
        f.write(json.dumps({"prompt": "hi", "chosen": "good", "rejected": "bad"}) + "\n")
    return p


class _FakeDaemon:
    pass


def test_trainer_dispatcher_exported_from_package():
    assert TrainerDispatcher is TD2


def test_trainer_dispatcher_handlers_registered():
    d = TrainerDispatcher(_FakeDaemon())
    keys = sorted(d.get_handlers().keys())
    assert keys == [
        "trainer.info",
        "trainer.rlsl",
        "trainer.runs.status",
        "trainer.sft",
        "trainer.trajectories.list",
    ]


def test_trainer_sft_requires_model():
    d = TrainerDispatcher(_FakeDaemon())
    result = asyncio.run(d._handle_sft({}))
    assert result == {"error": "model parameter required"}


def test_trainer_rlsl_requires_model_and_method():
    d = TrainerDispatcher(_FakeDaemon())
    assert asyncio.run(d._handle_rlsl({})) == {
        "error": "model parameter required"
    }
    assert asyncio.run(d._handle_rlsl({"model": "m"})) == {
        "error": "method parameter required (dpo|orpo|grpo)"
    }


def test_trainer_runs_status_unknown():
    d = TrainerDispatcher(_FakeDaemon())
    result = asyncio.run(d._handle_runs_status({"run_id": "nope"}))
    assert result == {"error": "unknown run_id"}


def test_trainer_runs_status_requires_run_id():
    d = TrainerDispatcher(_FakeDaemon())
    assert asyncio.run(d._handle_runs_status({})) == {
        "error": "run_id parameter required"
    }


def test_trainer_trajectories_list_limit_validation():
    d = TrainerDispatcher(_FakeDaemon())
    assert asyncio.run(d._handle_trajectories_list({"limit": 0})) == {
        "error": "limit must be between 1 and 500"
    }
    assert asyncio.run(d._handle_trajectories_list({"limit": 999})) == {
        "error": "limit must be between 1 and 500"
    }


def test_training_service_info_returns_version():
    svc = TrainingService()
    info = svc.info()
    assert "version" in info


def test_training_service_run_status_unknown():
    svc = TrainingService()
    assert svc.run_status("missing") == {"error": "unknown run_id"}


def test_training_service_run_rlsl_rejects_bad_method():
    svc = TrainingService()
    result = svc.run_rlsl(model="m", method="bogus")
    assert result == {"error": "method must be dpo|orpo|grpo"}


def test_training_service_run_rlsl_surfaces_upstream_error():
    svc = TrainingService()
    result = svc.run_rlsl(model="m", method="dpo", dataset=_write_pref_dataset())
    assert "run_id" in result
    asyncio.run(result["_task"]())
    status = svc.run_status(result["run_id"])
    assert status["method"] == "dpo"
    assert status["status"] == "error"
    assert "fusion-mlx" in status.get("error", "")


def test_trainer_rlsl_handler_schedules_task_and_surfaces_error():
    d = TrainerDispatcher(_FakeDaemon())
    result = asyncio.run(
        d._handle_rlsl({"model": "m", "method": "dpo", "dataset": _write_pref_dataset()})
    )
    assert result["run_id"]
    status = d._service().run_status(result["run_id"])
    assert status["method"] == "dpo"
