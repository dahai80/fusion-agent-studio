import asyncio
import json
import os
import tempfile

import pytest

from agent_runtime.dispatchers import TrainerDispatcher
from agent_runtime.dispatchers.trainer import TrainerDispatcher as TD2
from agent_runtime.training_service import TrainingService

_HAS_FUSION_TRAINER = True
try:
    import fusion_trainer  # noqa: F401
except ImportError:
    _HAS_FUSION_TRAINER = False


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
        "trainer.adapters.delete",
        "trainer.adapters.list",
        "trainer.datasets.list",
        "trainer.datasets.preview",
        "trainer.info",
        "trainer.info_full",
        "trainer.presets.list",
        "trainer.rlsl",
        "trainer.runs.list",
        "trainer.runs.progress",
        "trainer.runs.status",
        "trainer.runs.status_full",
        "trainer.runs.stop",
        "trainer.sft",
        "trainer.start_rlsl",
        "trainer.start_sft",
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


@pytest.mark.skipif(not _HAS_FUSION_TRAINER, reason="fusion_trainer not installed")
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


@pytest.mark.skipif(not _HAS_FUSION_TRAINER, reason="fusion_trainer not installed")
def test_training_service_run_rlsl_surfaces_upstream_error():
    svc = TrainingService()
    result = svc.run_rlsl(model="m", method="dpo", dataset=_write_pref_dataset())
    assert "run_id" in result
    asyncio.run(result["_task"]())
    status = svc.run_status(result["run_id"])
    assert status["method"] == "dpo"
    assert status["status"] == "error"
    err = status.get("error", "")
    assert err, "upstream error must surface into run status"
    assert (
        "create_dpo_job" in err
        or "fusion-mlx" in err
        or "401" in err
        or "Connection refused" in err
        or "11432" in err
    ), f"unrecognized upstream error: {err}"


def test_trainer_rlsl_handler_schedules_task_and_surfaces_error():
    d = TrainerDispatcher(_FakeDaemon())
    result = asyncio.run(
        d._handle_rlsl({"model": "m", "method": "dpo", "dataset": _write_pref_dataset()})
    )
    assert result["run_id"]
    status = d._service().run_status(result["run_id"])
    assert status["method"] == "dpo"


# ---------------------------------------------------------------------------
# RunManager-backed handler param validation (no live MLX needed).
# ---------------------------------------------------------------------------


def test_trainer_start_sft_requires_config():
    d = TrainerDispatcher(_FakeDaemon())
    assert asyncio.run(d._handle_start_sft({})) == {
        "error": "config parameter (TrainerConfig dict) required"
    }


def test_trainer_start_rlsl_requires_config():
    d = TrainerDispatcher(_FakeDaemon())
    assert asyncio.run(d._handle_start_rlsl({})) == {
        "error": "config parameter (TrainerConfig dict) required"
    }


def test_trainer_runs_list_limit_validation():
    d = TrainerDispatcher(_FakeDaemon())
    assert asyncio.run(d._handle_runs_list({"limit": 0})) == {
        "error": "limit must be between 1 and 500"
    }


def test_trainer_runs_status_full_requires_run_id():
    d = TrainerDispatcher(_FakeDaemon())
    assert asyncio.run(d._handle_runs_status_full({})) == {
        "error": "run_id parameter required"
    }


def test_trainer_runs_progress_requires_run_id():
    d = TrainerDispatcher(_FakeDaemon())
    assert asyncio.run(d._handle_runs_progress({})) == {
        "error": "run_id parameter required"
    }


def test_trainer_runs_stop_requires_run_id():
    d = TrainerDispatcher(_FakeDaemon())
    assert asyncio.run(d._handle_runs_stop({})) == {
        "error": "run_id parameter required"
    }


def test_trainer_datasets_preview_requires_name():
    d = TrainerDispatcher(_FakeDaemon())
    assert asyncio.run(d._handle_datasets_preview({})) == {
        "error": "name parameter required"
    }


def test_trainer_adapters_delete_requires_name():
    d = TrainerDispatcher(_FakeDaemon())
    assert asyncio.run(d._handle_adapters_delete({})) == {
        "error": "name parameter required"
    }


# ---------------------------------------------------------------------------
# #277: _build_trainer_config forwards publish-to-hub fields.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FUSION_TRAINER, reason="fusion_trainer not installed")
def test_build_trainer_config_forwards_publish_fields():
    from fusion_trainer.config import (
        DatasetConfig,
        RLSLConfig,
        SFTConfig,
        TrainerConfig,
    )

    from agent_runtime.training_service import _build_trainer_config

    cfg = _build_trainer_config(
        {
            "dataset": {"path": "/tmp/d.jsonl"},
            "publish_adapter": True,
            "hub_url": "https://hub.example.com",
            "hub_api_key": "secret-key-123",
        },
        DatasetConfig,
        RLSLConfig,
        SFTConfig,
        TrainerConfig,
    )
    assert cfg.publish_adapter is True
    assert cfg.hub_url == "https://hub.example.com"
    assert cfg.hub_api_key == "secret-key-123"


@pytest.mark.skipif(not _HAS_FUSION_TRAINER, reason="fusion_trainer not installed")
def test_build_trainer_config_defaults_publish_off():
    from fusion_trainer.config import (
        DatasetConfig,
        RLSLConfig,
        SFTConfig,
        TrainerConfig,
    )

    from agent_runtime.training_service import _build_trainer_config

    cfg = _build_trainer_config(
        {"dataset": {"path": "/tmp/d.jsonl"}},
        DatasetConfig,
        RLSLConfig,
        SFTConfig,
        TrainerConfig,
    )
    assert cfg.publish_adapter is False
    assert cfg.hub_url == ""
    assert cfg.hub_api_key == ""


@pytest.mark.skipif(not _HAS_FUSION_TRAINER, reason="fusion_trainer not installed")
def test_build_trainer_config_truthy_non_bool_publish():
    from fusion_trainer.config import (
        DatasetConfig,
        RLSLConfig,
        SFTConfig,
        TrainerConfig,
    )

    from agent_runtime.training_service import _build_trainer_config

    cfg = _build_trainer_config(
        {"dataset": {"path": "/tmp/d.jsonl"}, "publish_adapter": "true"},
        DatasetConfig,
        RLSLConfig,
        SFTConfig,
        TrainerConfig,
    )
    # bool() coercion — GUI may send string "true".
    assert cfg.publish_adapter is True

