# Issue #696: skill drafter — mine failure trajectories into draft SKILL.md
# candidates, review-gated install into the skills store.
from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.skill_drafter import (
    SkillDraftStore,
    TrajectoryMiner,
    draft_skill_md,
)
from agent_runtime.skills_store import SkillsStore
from agent_runtime.trajectory_writer import TrajectoryWriter


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    return await handler(params or {})


def _write_trace(writer: TrajectoryWriter, session: str, status: str, error: str = "", tools: list[str] | None = None) -> str:
    trace_id = writer.start(session, graph_name="g")
    writer.record_event(session, {"type": "tool_call", "name": (tools or ["t0"])[0]})
    if error:
        writer.record_event(session, {"type": "error", "content": error})
    writer.flush(session, status=status)
    return trace_id


@pytest.fixture
def writer(tmp_path):
    return TrajectoryWriter(output_dir=tmp_path / "traces")


@pytest.fixture
def drafts(tmp_path):
    return SkillDraftStore(
        drafts_dir=tmp_path / "drafts",
        store=SkillsStore(skills_dir=str(tmp_path / "skills")),
    )


class TestTrajectoryMiner:
    def test_clusters_failure_traces_by_error(self, writer):
        for i in range(3):
            _write_trace(writer, f"s{i}", status="error", error="Connection refused by gateway")
        _write_trace(writer, "ok1", status="completed")
        cands = TrajectoryMiner(writer, min_count=2).mine()
        assert len(cands) == 1
        assert cands[0]["count"] == 3
        assert len(cands[0]["trace_ids"]) == 3
        assert cands[0]["draft_name"].startswith("draft-")

    def test_min_count_filters_singletons(self, writer):
        _write_trace(writer, "a", status="error", error="boom one")
        _write_trace(writer, "b", status="error", error="boom two")
        assert TrajectoryMiner(writer, min_count=2).mine() == []

    def test_tool_signature_fallback(self, writer):
        # No error text but status failed — cluster by tool-call names.
        for i in range(2):
            _write_trace(writer, f"t{i}", status="max_iterations", tools=["web_fetch"])
        cands = TrajectoryMiner(writer, min_count=2).mine()
        assert len(cands) == 1
        assert cands[0]["pattern"].startswith("tools:")

    def test_completed_traces_ignored(self, writer):
        _write_trace(writer, "a", status="completed")
        _write_trace(writer, "b", status="completed")
        assert TrajectoryMiner(writer, min_count=2).mine() == []


class TestDraftRendering:
    def test_frontmatter_and_provenance(self):
        cand = {
            "draft_name": "draft-abcd1234",
            "count": 3,
            "pattern": "error:Connection refused",
            "trace_ids": ["t1", "t2", "t3"],
            "samples": ["Connection refused by gateway"],
        }
        md = draft_skill_md(cand)
        assert md.startswith("---\n")
        assert "name: draft-abcd1234" in md
        assert "UNTRUSTED" in md
        assert "## Provenance" in md
        assert "t1, t2, t3" in md

    def test_bad_name_hashed(self):
        cand = {"draft_name": "../evil", "count": 2, "pattern": "x", "trace_ids": []}
        md = draft_skill_md(cand)
        assert "draft-" in md.split("name: ")[1].splitlines()[0]


class TestSkillDraftStore:
    def test_save_list_get_roundtrip(self, drafts):
        cand = {
            "draft_name": "draft-11111111",
            "count": 2,
            "pattern": "error:boom",
            "trace_ids": ["t1", "t2"],
        }
        rec = drafts.save(cand, draft_skill_md(cand))
        assert rec["status"] == "draft"
        assert rec["untrusted"] is True
        listed = drafts.list_drafts()
        assert len(listed) == 1
        assert listed[0]["name"] == "draft-11111111"
        got = drafts.get("draft-11111111")
        assert got is not None
        assert got["provenance"]["trace_ids"] == ["t1", "t2"]

    def test_list_status_filter(self, drafts):
        cand = {"draft_name": "draft-22222222", "count": 2, "pattern": "p", "trace_ids": []}
        drafts.save(cand, draft_skill_md(cand))
        assert drafts.list_drafts(status="draft")
        assert drafts.list_drafts(status="published") == []

    def test_review_reject(self, drafts):
        cand = {"draft_name": "draft-33333333", "count": 2, "pattern": "p", "trace_ids": []}
        drafts.save(cand, draft_skill_md(cand))
        out = drafts.review("draft-33333333", "reject", reviewer="alice")
        assert out["status"] == "rejected"
        assert drafts.get("draft-33333333")["status"] == "rejected"

    def test_review_approve_installs_skill(self, drafts, tmp_path):
        cand = {"draft_name": "draft-44444444", "count": 2, "pattern": "p", "trace_ids": ["t1"]}
        drafts.save(cand, draft_skill_md(cand))
        out = drafts.review("draft-44444444", "approve", reviewer="bob")
        assert out["status"] == "published"
        rec = drafts.get("draft-44444444")
        assert rec["untrusted"] is False
        assert rec["reviewed_by"] == "bob"
        installed = Path(out["path"])
        assert installed.is_file()
        assert "## Provenance" in installed.read_text(encoding="utf-8")

    def test_review_approve_rejects_missing_frontmatter(self, drafts):
        # Structure-validation failure must refuse to publish: no frontmatter
        # means the draft never passes the agentskills.io gate.
        cand = {"draft_name": "draft-66666666", "count": 2, "pattern": "p", "trace_ids": []}
        drafts.save(cand, "just some prose, no frontmatter")
        out = drafts.review("draft-66666666", "approve", reviewer="carol")
        assert out["status"] == "error"
        assert "missing YAML frontmatter" in out["message"]
        assert drafts.get("draft-66666666")["status"] == "invalid"
        assert drafts.list_drafts(status="published") == []

    def test_review_approve_rejects_empty_body(self, drafts):
        cand = {"draft_name": "draft-77777777", "count": 2, "pattern": "p", "trace_ids": []}
        drafts.save(cand, "---\nname: draft-77777777\ndescription: hollow shell\n---\n\n")
        out = drafts.review("draft-77777777", "approve", reviewer="carol")
        assert out["status"] == "error"
        assert "empty body" in out["message"]
        assert drafts.get("draft-77777777")["status"] == "invalid"

    def test_review_unknown_and_bad_action(self, drafts):
        assert drafts.review("draft-nope", "approve")["status"] == "not_found"
        cand = {"draft_name": "draft-55555555", "count": 2, "pattern": "p", "trace_ids": []}
        drafts.save(cand, draft_skill_md(cand))
        assert drafts.review("draft-55555555", "maybe")["status"] == "error"

    def test_get_rejects_bad_name(self, drafts):
        assert drafts.get("../escape") is None


class TestDraftRpcs:
    @pytest.fixture
    def daemon(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
        return DaemonServer(
            socket_path=str(tmp_path / "test.sock"),
            store_path=str(tmp_path / "store.db"),
        )

    async def test_mine_list_review_flow(self, daemon, tmp_path, monkeypatch):
        # Seed two failing traces, then point the miner's default writer at
        # them via the module singleton (same isolation pattern as #697).
        w = TrajectoryWriter(output_dir=tmp_path / "traces")
        for i in range(2):
            _write_trace(w, f"x{i}", status="error", error="gateway 502 on chat")
        monkeypatch.setattr("agent_runtime.trajectory_writer._writer", w)

        res = await _run(daemon, "skills.mine_drafts", {"min_count": 2})
        assert res["status"] == "ok"
        assert res["count"] >= 1
        name = res["drafts"][0]["name"]

        listed = await _run(daemon, "skills.list_drafts", {"status": "draft"})
        assert listed["count"] >= 1
        assert any(d["name"] == name for d in listed["drafts"])

        reviewed = await _run(
            daemon, "skills.review_draft", {"name": name, "action": "approve", "reviewer": "ci"}
        )
        assert reviewed["status"] == "published"
        assert Path(reviewed["path"]).is_file()
        after = await _run(daemon, "skills.list_drafts", {"status": "draft"})
        assert after["count"] == 0

    async def test_review_errors(self, daemon):
        res = await _run(daemon, "skills.review_draft", {"name": "draft-none", "action": "approve"})
        assert res["status"] == "error"
        res = await _run(daemon, "skills.review_draft", {"name": ""})
        assert res["status"] == "error"

    async def test_mine_validates_params(self, daemon):
        res = await _run(daemon, "skills.mine_drafts", {"min_count": "abc"})
        assert res["status"] == "error"
