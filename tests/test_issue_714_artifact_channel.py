# Issue #714 part 2: artifact_channel shim + teams.run_member ref-only boundary.
from __future__ import annotations

import os

import pytest

import agent_runtime.artifact_channel as ac
from agent_runtime.artifact_channel import (
    ArtifactStore,
    ArtifactTools,
    StudioArtifactChannel,
    SummaryInjector,
    WorkerDelivery,
    default_summarize,
    studio_artifact_channel,
)
from tests.test_issue_707_teams_dispatcher import _brief, _definition, _run, _runtime


@pytest.fixture
def daemon(tmp_path):
    from agent_runtime.daemon_server import DaemonServer

    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ac, "_CHANNEL", None)
    return tmp_path / "home"


def test_shim_reexports_kernel_surface():
    import fusion_harness.artifact_channel as kh

    assert ArtifactStore is kh.ArtifactStore
    assert ArtifactTools is kh.ArtifactTools
    assert SummaryInjector is kh.SummaryInjector
    assert WorkerDelivery is kh.WorkerDelivery
    assert default_summarize is kh.default_summarize


def test_factory_rooted_in_studio_home(_iso):
    ch = studio_artifact_channel()
    assert isinstance(ch, StudioArtifactChannel)
    assert os.path.realpath(ch.root) == os.path.realpath(str(_iso / "artifact_channel"))
    assert studio_artifact_channel() is ch, "singleton"


def test_capture_read_roundtrip_through_shim(_iso):
    ch = studio_artifact_channel()
    ref = ch.tools.capture("worker-a", "full body text", kind="text", run_id="t1")
    assert ref == "t1/worker-a.text"
    assert ch.tools.read(ref) == "full body text"
    assert ch.tools.list("t1") == [ref]


def test_summary_injection_front_of_context():
    inj = SummaryInjector()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
    ]
    deliveries = [WorkerDelivery("worker-a", "t1/worker-a.text", "did the thing")]
    out = inj.inject_front(msgs, deliveries)
    assert out[0]["role"] == "system"
    assert "sys" in out[0]["content"]
    assert out[1]["content"].startswith("[Worker summaries")
    assert "did the thing" in out[1]["content"]
    # idempotent re-inject replaces the segment
    out2 = inj.inject_front(out, deliveries)
    assert sum(1 for m in out2 if "Worker summaries" in str(m.get("content"))) == 1


async def test_run_member_delivery_crosses_boundary_ref_only(daemon, _iso):
    rt, _client = _runtime(responses=[])
    daemon._get_runtime = lambda: rt
    res = await _run(daemon, "teams.register", {"definition": _definition()})
    assert res["status"] == "ok"
    board = await _run(daemon, "teams.add_task", {"team": "pair", "title": "t"})
    out = await _run(
        daemon,
        "teams.run_member",
        {"team": "pair", "member": "worker-a", "task_id": board["task_id"], "brief": _brief()},
    )
    assert out["status"] == "ok", out
    r = out["result"]
    assert r["artifact_ref"], "run_member must write the full output to the channel"
    assert r["artifact_ref"].endswith(".text")
    ch = studio_artifact_channel()
    full = ch.tools.read(r["artifact_ref"])
    assert full == r["summary"], "RPC summary and on-disk full text must agree"
    # boundary: the ref exists on disk; the RPC payload must not embed a
    # second full-text copy beyond the kernel-truncated summary field
    assert len(r["summary"]) <= len(full)


async def test_no_full_text_in_task_board_metadata(daemon, _iso):
    rt, _client = _runtime(responses=[])
    daemon._get_runtime = lambda: rt
    await _run(daemon, "teams.register", {"definition": _definition()})
    board = await _run(daemon, "teams.add_task", {"team": "pair", "title": "t"})
    await _run(
        daemon,
        "teams.run_member",
        {"team": "pair", "member": "worker-a", "task_id": board["task_id"], "brief": _brief()},
    )
    status = await _run(daemon, "teams.status", {"team": "pair"})
    task = status["tasks"][board["task_id"]]
    serialized = repr(task)
    assert "artifact_ref" not in serialized or r"\\\\/" not in serialized
    results = task["results"]
    for entry in results:
        assert len(entry.get("summary", "")) <= 500, "kernel truncation is the board boundary"
