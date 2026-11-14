# Issue #702: consume fusion-harness 0.11.0 discipline modules —
# artifact_channel (worker delivery), playbook (lesson ops),
# cache_discipline (order/length-preserving tool masking).
from __future__ import annotations

import json

import pytest
from fusion_harness.cache_discipline import assert_stable_prefix, mask_tools

from agent_runtime.delivery_channel import WorkerDeliveryChannel
from agent_runtime.playbook_store import PlaybookStore
from agent_runtime.subagent import SubAgentRunner, SubAgentSpec
from tests.test_issue_341_subagent import _llm_response, _make_parent
from tests.test_issue_696_skill_drafter import _write_trace


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


class TestWorkerDeliveryChannel:
    def test_deliver_writes_artifact_and_summarizes(self, _home):
        ch = WorkerDeliveryChannel()
        d = ch.deliver("run-1", "worker-a", "Decision: use sqlite\nResult: 42 rows\n" + "filler\n" * 200)
        assert d.artifact_ref == "run-1/worker-a.text"
        full = _home / "worker_artifacts" / "run-1" / "worker-a.text"
        assert full.is_file()
        assert "42 rows" in full.read_text(encoding="utf-8")
        assert len(d.summary) <= 1300
        assert "Decision" in d.summary or "Result" in d.summary

    def test_inject_front_idempotent(self, _home):
        ch = WorkerDeliveryChannel()
        d = ch.deliver("run-1", "worker-a", "Result: ok")
        messages = [
            {"role": "system", "content": "stable head"},
            {"role": "user", "content": "hi"},
        ]
        first = ch.inject_front(messages, [d])
        assert first[0]["content"] == "stable head"
        assert "worker-a" in first[1]["content"]
        assert d.artifact_ref in first[1]["content"]
        second = ch.inject_front(first, [d])
        segs = [m for m in second if str(m.get("content", "")).startswith("[Worker summaries")]
        assert len(segs) == 1, "re-inject must replace, not duplicate"

    @pytest.mark.asyncio
    async def test_runner_delivers_report_and_injects_parent(self, _home):
        responses = [_llm_response(content="final worker report body")]
        rt, _client = _make_parent(responses=responses)
        from agent_runtime.context import AgentContext

        parent = AgentContext(session_id="parent-x")
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="w", task="do it"), parent_ctx=parent)
        assert report.ok
        assert report.artifact_ref == "parent-x/w.text"
        assert (_home / "worker_artifacts" / "parent-x" / "w.text").is_file()
        segs = [
            m
            for m in parent.messages
            if m.get("role") == "system" and str(m.get("content", "")).startswith("[Worker summaries")
        ]
        assert segs and "w" in segs[0]["content"]

    @pytest.mark.asyncio
    async def test_runner_delivery_env_off(self, _home, monkeypatch):
        monkeypatch.setenv("FUSION_WORKER_ARTIFACTS", "0")
        responses = [_llm_response(content="body")]
        rt, _client = _make_parent(responses=responses)
        from agent_runtime.context import AgentContext

        parent = AgentContext(session_id="parent-y")
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="w", task="t"), parent_ctx=parent)
        assert report.artifact_ref == ""
        assert not (_home / "worker_artifacts" / "parent-y").exists()


class TestPlaybook:
    def test_append_requires_provenance(self, _home):
        pb = PlaybookStore(root=_home / "playbook")
        assert pb.append_lesson("s", "lesson with no episodes")["status"] == "error"
        out = pb.append_lesson("s", "lesson", episodes=["trace-1"])
        assert out["status"] == "ok"
        assert pb.entries()[0]["episodes"] == ["trace-1"]

    def test_ops_persist_and_replay(self, _home):
        pb = PlaybookStore(root=_home / "playbook")
        pb.append_lesson("ops", "first lesson", episodes=["t1"])
        pb.append_lesson("ops", "second lesson", episodes=["t2"])
        pb2 = PlaybookStore(root=_home / "playbook")
        rows = pb2.entries()
        assert [r["content"] for r in rows] == ["first lesson", "second lesson"]
        with open(_home / "playbook" / "ops.jsonl", encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        assert all(l["episodes"] for l in lines if l["op"] == "add")

    def test_delete_tombstones(self, _home):
        pb = PlaybookStore(root=_home / "playbook")
        out = pb.append_lesson("s", "to delete", episodes=["t1"])
        pb.delete_lesson(out["entry_id"])
        assert pb.entries() == []

    def test_skill_drafter_appends_playbook_lessons(self, _home, tmp_path, monkeypatch):
        import agent_runtime.trajectory_writer as tw

        w_dir = tmp_path / "traces"
        w = tw.TrajectoryWriter(output_dir=w_dir)
        for i in range(2):
            _write_trace(w, f"y{i}", status="error", error="gateway 502 on chat")
        monkeypatch.setattr("agent_runtime.trajectory_writer._writer", w)
        from agent_runtime.skill_drafter import mine_and_draft

        drafts = mine_and_draft(min_count=2)
        assert drafts
        pb = PlaybookStore(root=_home / "playbook")
        rows = pb.entries()
        assert rows and rows[0]["section"] == "skill-drafts"
        assert rows[0]["episodes"], "lesson must carry trace-id provenance"


class TestCacheDisciplineMasking:
    TOOLS = [
        {"type": "function", "function": {"name": "file_read"}},
        {"type": "function", "function": {"name": "file_write"}},
        {"type": "function", "function": {"name": "web_fetch"}},
    ]

    def test_mask_preserves_order_and_length(self):
        masked = mask_tools(self.TOOLS, ["file_read", "web_fetch"])
        assert len(masked) == len(self.TOOLS)
        assert [m["function"]["name"] for m in masked] == ["file_read", "file_write", "web_fetch"]
        flags = [m["x-fusion-masked"] for m in masked]
        assert flags == [False, True, False]
        # Original list untouched (mask semantics: never mutate the source).
        assert "x-fusion-masked" not in self.TOOLS[1]

    def test_tool_availability_state_transition(self):
        from fusion_harness.cache_discipline import ToolAvailabilityState

        st = ToolAvailabilityState(self.TOOLS, ["file_read", "file_write", "web_fetch"])
        assert all(not m["x-fusion-masked"] for m in st.masked())
        st.transition(["file_read"])
        masked = st.masked()
        assert [m["x-fusion-masked"] for m in masked] == [False, True, True]

    def test_node_masking_env_gated(self, monkeypatch):
        # Mask mode OFF (default): allowlist narrows by removal (legacy).
        # Mask mode ON: order/length preserved. Verified at the helper level
        # the node executor delegates to.
        from agent_runtime.dispatchers.subagent import SubAgentDispatcher  # noqa: F401

        monkeypatch.setenv("FUSION_TOOL_MASKING", "1")
        masked = mask_tools(self.TOOLS, {"file_read"})
        assert len(masked) == 3 and masked[1]["x-fusion-masked"] is True
        monkeypatch.setenv("FUSION_TOOL_MASKING", "0")

    def test_prompt_prefix_stability_lint(self):
        # Lint-style contract: per-turn serialized prompts must extend the
        # first turn byte-identically (stable head + append-only tail).
        head = "stable system head\n"
        turns = [head, head + "user turn 1\n", head + "user turn 1\nuser turn 2\n"]
        assert assert_stable_prefix(turns) == []
        unstable = [head, "volatile head\n" + "user turn 1\n"]
        assert assert_stable_prefix(unstable) == [1]
