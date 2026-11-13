"""E4: Inspector data-plane — tool_stats / thread_lanes aggregations + RPCs."""

from __future__ import annotations

import pytest

from agent_runtime.inspector import thread_lanes, tool_stats


def _ev(type_, name="", node_id="", ts=0.0, event_id="", parent_id="", content="", **meta):
    return {
        "type": type_,
        "name": name,
        "node_id": node_id,
        "timestamp": ts,
        "event_id": event_id,
        "parent_id": parent_id,
        "content": content,
        "metadata": meta.pop("metadata", None) or meta,
    }


class TestToolStats:
    def test_counts_and_median(self):
        events = [
            _ev("tool_call", "read_file", "n1", ts=1.0),
            _ev("tool_result", "read_file", "n1", ts=1.4, content="ok"),
            _ev("tool_call", "read_file", "n1", ts=2.0),
            _ev("tool_result", "read_file", "n1", ts=2.6, content="ok"),
            _ev("tool_call", "terminal", "n2", ts=3.0),
            _ev("tool_result", "terminal", "n2", ts=3.1, content="Error: boom"),
        ]
        stats = tool_stats(events)
        by = {s["tool"]: s for s in stats}
        assert by["read_file"]["calls"] == 2
        assert by["read_file"]["failures"] == 0
        assert by["read_file"]["median_ms"] == 500.0
        assert by["terminal"]["calls"] == 1
        assert by["terminal"]["failures"] == 1
        assert by["terminal"]["failure_rate"] == 1.0

    def test_metadata_error_flag(self):
        events = [
            _ev("tool_call", "git_commit", "n1", ts=1.0),
            _ev("tool_result", "git_commit", "n1", ts=1.2, content="...", error=True),
        ]
        stats = tool_stats(events)
        assert stats[0]["failures"] == 1

    def test_unpaired_call_not_failure(self):
        events = [_ev("tool_call", "browser_navigate", "n1", ts=1.0)]
        stats = tool_stats(events)
        assert stats[0]["calls"] == 1
        assert stats[0]["failures"] == 0
        assert stats[0]["median_ms"] == 0.0

    def test_blocked_prefix_counts_failure(self):
        events = [
            _ev("tool_call", "file_write", "n1", ts=1.0),
            _ev(
                "tool_result",
                "file_write",
                "n1",
                ts=1.1,
                content="Blocked: plan_mode is active",
            ),
        ]
        assert tool_stats(events)[0]["failures"] == 1

    def test_empty_events(self):
        assert tool_stats([]) == []


class TestThreadLanes:
    def test_single_lane_flat(self):
        events = [
            _ev("start", ts=1.0, event_id="e1"),
            _ev("llm", ts=1.1, event_id="e2", parent_id="e1"),
            _ev("result", ts=2.0, event_id="e3", parent_id="e1"),
        ]
        out = thread_lanes(events)
        assert out["lane_count"] == 1
        lane = out["lanes"][0]
        assert lane["lane_id"] == "e1"
        assert lane["event_count"] == 3
        assert lane["max_depth"] == 1
        assert lane["t_start"] == 1.0
        assert lane["t_end"] == 2.0

    def test_subagent_nesting(self):
        # root -> llm -> subagent span -> its own tool_call chain
        events = [
            _ev("start", ts=1.0, event_id="r"),
            _ev("tool_call", "subagent_spawn", "n1", ts=1.2, event_id="s", parent_id="r"),
            _ev("tool_call", "read_file", "sub", ts=1.3, event_id="s1", parent_id="s"),
            _ev("tool_result", "read_file", "sub", ts=1.5, event_id="s2", parent_id="s"),
            _ev("result", ts=2.0, event_id="f", parent_id="r"),
        ]
        out = thread_lanes(events)
        assert out["lane_count"] == 1
        lane = out["lanes"][0]
        depths = {e["event_id"]: e["depth"] for e in lane["events"]}
        assert depths["s1"] == 2
        assert depths["s2"] == 2
        assert lane["max_depth"] == 2

    def test_parallel_roots_two_lanes_sorted_by_start(self):
        events = [
            _ev("start", "b-run", ts=2.0, event_id="b"),
            _ev("start", "a-run", ts=1.0, event_id="a"),
            _ev("llm", ts=1.2, event_id="a1", parent_id="a"),
        ]
        out = thread_lanes(events)
        assert out["lane_count"] == 2
        assert [l["lane_id"] for l in out["lanes"]] == ["a", "b"]

    def test_events_without_ids_each_own_lane(self):
        events = [_ev("token", ts=1.0), _ev("token", ts=1.1)]
        out = thread_lanes(events)
        assert out["lane_count"] == 2


class TestRunsRpcs:
    @pytest.fixture
    async def daemon(self, tmp_path):
        import os

        from agent_runtime.daemon_server import DaemonServer

        sock = f"/tmp/test-e4-{os.getpid()}-{id(tmp_path)}.sock"
        d = DaemonServer(
            socket_path=sock,
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=str(tmp_path / "s.db"),
        )
        await d.start()
        yield d
        await d.stop()
        if os.path.exists(sock):
            os.unlink(sock)

    async def test_tool_stats_via_history_store(self, daemon, tmp_path):
        from agent_runtime.run_history import RunHistoryStore

        store = RunHistoryStore(db_path=str(tmp_path / "h.db"))
        daemon._run_history_store = store
        events = [
            _ev("tool_call", "read_file", "n1", ts=1.0),
            _ev("tool_result", "read_file", "n1", ts=1.5, content="ok"),
        ]
        store.save_run("run-abc", "g", "g", events, started_at=1.0, finished_at=2.0)
        h = daemon._get_handler("runs.tool_stats")
        r = await h({"run_id": "run-abc"})
        assert r["tools"][0]["tool"] == "read_file"
        assert r["tools"][0]["calls"] == 1

    async def test_tool_stats_via_managed_registry(self, daemon):
        from agent_runtime.run_registry import ManagedRun, RunRegistry

        reg = RunRegistry()
        daemon._managed_run_registry = reg
        run = ManagedRun("g")
        run.buffer = [
            _ev("tool_call", "terminal", "n1", ts=1.0),
            _ev("tool_result", "terminal", "n1", ts=1.2, content="Error: x"),
        ]
        reg._runs[run.run_id] = run
        h = daemon._get_handler("runs.thread_lanes")
        r = await h({"run_id": run.run_id})
        assert r["run_id"] == run.run_id
        assert r["lane_count"] >= 1

    async def test_unknown_run_error(self, daemon):
        h = daemon._get_handler("runs.tool_stats")
        r = await h({"run_id": "nope"})
        assert "error" in r
