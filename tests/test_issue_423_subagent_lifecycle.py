"""Tests for #423 (subagent transcripts + resume + partial) and #424
(background spawn + subagent tree)."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_runtime.subagent import SubAgentRunner, SubAgentSpec
from tests.test_issue_341_subagent import T1Tool, _llm_response, _make_parent, _tc
from tools.registry import ToolRegistry


def _tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(T1Tool())
    return reg


class TestTranscripts:
    @pytest.mark.asyncio
    async def test_agent_id_and_transcript_persisted(self):
        rt, _ = _make_parent([_llm_response(content="report body")])
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="worker", task="do it"))
        assert report.agent_id.startswith("worker-")
        record = runner.transcripts().get(report.agent_id)
        assert record["name"] == "worker"
        assert record["status"] == "completed"
        assert record["partial"] is False
        assert record["report"] == "report body"
        assert record["messages"], "transcript should store the sub conversation"
        assert record["spec"]["name"] == "worker"

    @pytest.mark.asyncio
    async def test_transcript_list_by_parent(self):
        rt, _ = _make_parent([_llm_response(content="a"), _llm_response(content="b")])
        runner = SubAgentRunner(rt)
        from agent_runtime.context import AgentContext

        ctx = AgentContext(session_id="parent-1")
        await runner.run(SubAgentSpec(name="one", task="t"), parent_ctx=ctx)
        await runner.run(SubAgentSpec(name="two", task="t"), parent_ctx=ctx)
        rows = runner.transcripts().list(parent_session="parent-1")
        assert {r["name"] for r in rows} == {"one", "two"}

    @pytest.mark.asyncio
    async def test_partial_when_max_turns_hit(self):
        responses = [_llm_response(tool_calls=[_tc("t1", {"tag": "a"})])]
        rt, _ = _make_parent(responses, tools=_tools())
        runner = SubAgentRunner(rt)
        report = await runner.run(
            SubAgentSpec(name="cut", task="loop", tools=["t1"], max_turns=1)
        )
        assert report.partial is True
        record = runner.transcripts().get(report.agent_id)
        assert record["status"] == "partial"
        assert record["partial"] is True

    @pytest.mark.asyncio
    async def test_resume_continues_with_prior_context(self):
        responses = [
            _llm_response(tool_calls=[_tc("t1", {"tag": "a"})]),
            _llm_response(content="finished the job"),
        ]
        rt, client = _make_parent(responses, tools=_tools())
        runner = SubAgentRunner(rt)
        first = await runner.run(
            SubAgentSpec(name="resumable", task="step one", tools=["t1"], max_turns=1)
        )
        assert first.partial is True
        second = await runner.resume(first.agent_id, task="continue", max_turns=4)
        assert second is not None
        assert second.ok is True
        assert second.partial is False
        assert second.agent_id == first.agent_id
        assert "finished the job" in second.report
        sent = json.dumps(client.calls[-1], ensure_ascii=False)
        assert "t1" in sent or "step one" in sent, "resume must replay prior turns"

    @pytest.mark.asyncio
    async def test_resume_unknown_id_returns_none(self):
        rt, _ = _make_parent([_llm_response(content="x")])
        runner = SubAgentRunner(rt)
        assert await runner.resume("does-not-exist") is None


class TestBackgroundSpawn:
    @pytest.mark.asyncio
    async def test_spawn_returns_launched_and_completes(self):
        rt, _ = _make_parent([_llm_response(content="bg done")])
        runner = SubAgentRunner(rt)
        launched = runner.spawn(SubAgentSpec(name="bg", task="run", background=True))
        assert launched["status"] == "launched"
        report = await runner.task_manager().wait(launched["agent_id"])
        assert report is not None
        assert report["ok"] is True
        assert report["report"] == "bg done"
        record = runner.transcripts().get(launched["agent_id"])
        assert record["status"] == "completed"

    @pytest.mark.asyncio
    async def test_three_spawns_listed_in_tree(self):
        responses = [_llm_response(content=f"r{i}") for i in range(3)]
        rt, _ = _make_parent(responses)
        runner = SubAgentRunner(rt)
        ids = []
        for i in range(3):
            ids.append(
                runner.spawn(SubAgentSpec(name=f"w{i}", task="t", background=True))[
                    "agent_id"
                ]
            )
        tree = runner.task_manager().tree()
        assert tree["nodes"], "tree must list spawned subagents"
        assert {n["agent_id"] for n in tree["nodes"]} >= set(ids)
        reports = []
        for agent_id in ids:
            reports.append(await runner.task_manager().wait(agent_id))
        assert [r["ok"] for r in reports] == [True, True, True]
        tree_after = runner.task_manager().tree()
        completed = [n for n in tree_after["nodes"] if n["agent_id"] in ids]
        assert all(n["status"] == "completed" for n in completed)

    @pytest.mark.asyncio
    async def test_cancel_running_spawn(self):
        rt, _ = _make_parent([])
        runner = SubAgentRunner(rt)
        launched = runner.spawn(SubAgentSpec(name="slow", task="t", background=True))
        await asyncio.sleep(0)
        cancelled = runner.task_manager().cancel(launched["agent_id"])
        if cancelled:
            record = runner.transcripts().get(launched["agent_id"])
            assert record["status"] in ("failed", "partial", "completed")

    @pytest.mark.asyncio
    async def test_spawn_tool_background_mode(self):
        from tools.subagent_tool import SubAgentTool

        rt, _ = _make_parent([_llm_response(content="ok")])
        runner = SubAgentRunner(rt)
        rt.subagent_runner = runner
        tool = SubAgentTool(runner_provider=lambda: runner)
        raw = await tool.execute(name="bg-tool", task="t", background=True)
        payload = json.loads(raw)
        assert payload["status"] == "launched"
        assert payload["agent_id"]
