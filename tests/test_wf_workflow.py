"""Tests for WF-1 (anti-forgetting), WF-2 (truncation detection), WF-3 (progressive generation)."""

from unittest.mock import MagicMock

import pytest

from agent_runtime.artifact_bridge import ArtifactBridge
from agent_runtime.artifact_tools import (
    VALID_GENERATION_PHASES,
    ArtifactManager,
    ArtifactRecord,
)
from agent_runtime.context import AgentContext
from agent_runtime.runtime import AgentRuntime

# ── WF-1: Anti-Forgetting Turn Counter ─────────────────────────


class TestWFAntiForgetting:
    def test_context_has_artifact_turn_count(self):
        ctx = AgentContext()
        assert ctx.artifact_turn_count == 0

    def test_context_serialization_turn_count(self):
        ctx = AgentContext(artifact_turn_count=7)
        d = ctx.to_dict()
        assert d["artifact_turn_count"] == 7

    def test_context_deserialization_turn_count(self):
        d = {"artifact_turn_count": 12}
        ctx = AgentContext.from_dict(d)
        assert ctx.artifact_turn_count == 12

    def test_turn_counter_increments_on_llm_node(self):
        mgr = ArtifactManager()
        mgr.create_artifact("agent1", "doc1", content="hello world")
        ctx = AgentContext(agent_id="agent1")
        assert ctx.artifact_turn_count == 0
        ctx.artifact_turn_count += 1
        assert ctx.artifact_turn_count == 1

    def test_summary_injected_every_5_turns(self):
        mgr = ArtifactManager()
        mgr.create_artifact("agent1", "doc1", content="hello world")
        ctx = AgentContext(agent_id="agent1")
        for i in range(1, 11):
            ctx.artifact_turn_count = i
            if i % 5 == 0:
                summary = mgr.get_active_artifacts_context("agent1", limit=10)
                assert summary != ""
                assert "[Active Artifacts]" in summary
            else:
                assert ctx.artifact_turn_count % 5 != 0

    def test_no_summary_when_no_artifacts(self):
        mgr = ArtifactManager()
        summary = mgr.get_active_artifacts_context("agent_no_art", limit=10)
        assert summary == ""


# ── WF-2: Truncation Detection ─────────────────────────────────


class TestWFTruncationDetection:
    def test_detect_unclosed_artifact_tag(self):
        content = '<artifact id="abc123" title="test">Some content here without closing tag'
        result = AgentRuntime._detect_unclosed_artifacts(content)
        assert "abc123" in result

    def test_detect_no_truncation_when_closed(self):
        content = '<artifact id="abc123" title="test">Some content</artifact>'
        result = AgentRuntime._detect_unclosed_artifacts(content)
        assert result == []

    def test_detect_multiple_unclosed(self):
        content = (
            '<artifact id="a1">text1</artifact>'
            '<artifact id="a2">text2'
            '<artifact id="a3">text3'
        )
        result = AgentRuntime._detect_unclosed_artifacts(content)
        assert len(result) == 2
        assert "a2" in result
        assert "a3" in result

    def test_detect_unclosed_artifact_ref(self):
        content = '<artifact-ref id="ref1" title="ref">some ref text'
        result = AgentRuntime._detect_unclosed_artifacts(content)
        assert "ref1" in result

    def test_extract_breakpoint_paragraphs(self):
        content = "First paragraph\n\nSecond paragraph\n\nThird paragraph with more text"
        bp = AgentRuntime._extract_breakpoint(content)
        assert "Third paragraph" in bp

    def test_extract_breakpoint_single_line(self):
        content = "single line content"
        bp = AgentRuntime._extract_breakpoint(content)
        assert bp == "single line content"

    def test_extract_breakpoint_truncation(self):
        content = "x" * 500
        bp = AgentRuntime._extract_breakpoint(content)
        assert len(bp) <= 300

    def test_no_truncation_when_content_empty(self):
        result = AgentRuntime._detect_unclosed_artifacts("")
        assert result == []

    def test_no_truncation_when_no_artifact_tags(self):
        result = AgentRuntime._detect_unclosed_artifacts("plain text without tags")
        assert result == []


# ── WF-3: Progressive Generation Pipeline ──────────────────────


class TestWFProgressiveGeneration:
    def test_artifact_record_has_generation_phase(self):
        rec = ArtifactRecord()
        assert rec.generation_phase == "skeleton"

    def test_artifact_record_serialization_phase(self):
        rec = ArtifactRecord(generation_phase="filling")
        d = rec.to_dict()
        assert d["generation_phase"] == "filling"

    def test_artifact_record_deserialization_phase(self):
        d = {"generation_phase": "completed"}
        rec = ArtifactRecord.from_dict(d)
        assert rec.generation_phase == "completed"

    def test_valid_generation_phases(self):
        assert VALID_GENERATION_PHASES == {"skeleton", "filling", "completed"}

    def test_create_artifact_starts_as_skeleton(self):
        mgr = ArtifactManager()
        result = mgr.create_artifact("agent1", "doc1", content="outline")
        assert result["status"] == "ok"
        rec = result["record"]
        assert rec["generation_phase"] == "skeleton"

    def test_patch_advances_to_filling(self):
        mgr = ArtifactManager()
        result = mgr.create_artifact("agent1", "doc1", content="## Outline\n<!-- section:intro -->\nIntro\n<!-- end:intro -->")
        aid = result["artifact_id"]
        patch_result = mgr.patch_artifact(aid, "section_replace", content="Full intro", section="intro", agent_id="agent1")
        assert patch_result["status"] == "ok"
        assert patch_result["record"]["generation_phase"] == "filling"

    def test_append_advances_to_filling(self):
        mgr = ArtifactManager()
        result = mgr.create_artifact("agent1", "doc1", content="outline")
        aid = result["artifact_id"]
        patch_result = mgr.patch_artifact(aid, "append", content="\nmore content", agent_id="agent1")
        assert patch_result["status"] == "ok"
        assert patch_result["record"]["generation_phase"] == "filling"

    def test_replace_large_advances_to_completed(self):
        mgr = ArtifactManager()
        mgr.create_artifact("agent1", "doc1", content="outline")
        aid = list(mgr._artifacts.keys())[0]
        mgr._artifacts[aid].generation_phase = "filling"
        big_content = "x" * 2500
        patch_result = mgr.patch_artifact(aid, "replace", content=big_content, agent_id="agent1")
        assert patch_result["status"] == "ok"
        assert patch_result["record"]["generation_phase"] == "completed"

    def test_advance_generation_phase_explicit(self):
        mgr = ArtifactManager()
        result = mgr.create_artifact("agent1", "doc1", content="outline")
        aid = result["artifact_id"]
        assert mgr._artifacts[aid].generation_phase == "skeleton"
        adv = mgr.advance_generation_phase(aid, "filling")
        assert adv["status"] == "ok"
        assert adv["generation_phase"] == "filling"
        adv2 = mgr.advance_generation_phase(aid, "completed")
        assert adv2["status"] == "ok"
        assert adv2["generation_phase"] == "completed"

    def test_advance_phase_rejects_regression(self):
        mgr = ArtifactManager()
        result = mgr.create_artifact("agent1", "doc1", content="outline")
        aid = result["artifact_id"]
        mgr.advance_generation_phase(aid, "filling")
        adv = mgr.advance_generation_phase(aid, "skeleton")
        assert adv["status"] == "error"
        assert "regress" in adv["message"]

    def test_advance_phase_rejects_invalid_phase(self):
        mgr = ArtifactManager()
        result = mgr.create_artifact("agent1", "doc1", content="outline")
        aid = result["artifact_id"]
        adv = mgr.advance_generation_phase(aid, "invalid")
        assert adv["status"] == "error"

    def test_advance_phase_not_found(self):
        mgr = ArtifactManager()
        adv = mgr.advance_generation_phase("nonexistent", "filling")
        assert adv["status"] == "error"

    def test_bridge_advance_generation_phase(self):
        mgr = ArtifactManager()
        bridge = ArtifactBridge(local_manager=mgr)
        bridge._remote_available = False
        result = mgr.create_artifact("agent1", "doc1", content="outline")
        aid = result["artifact_id"]
        adv = bridge.advance_generation_phase(aid, "filling")
        assert adv["status"] == "ok"

    @pytest.mark.asyncio
    async def test_dispatcher_advance_phase(self):
        daemon_mock = MagicMock()
        bridge = MagicMock()
        bridge.advance_generation_phase.return_value = {"status": "ok", "generation_phase": "filling"}
        daemon_mock._get_artifact_manager.return_value = bridge
        from agent_runtime.dispatchers.artifact import ArtifactDispatcher
        disp = ArtifactDispatcher(daemon_mock)
        result = await disp._handle_advance_phase({"artifact_id": "a1", "target_phase": "filling"})
        assert result["status"] == "ok"
