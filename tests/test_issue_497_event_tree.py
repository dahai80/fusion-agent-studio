"""Tests for #497 event tree + conversation fork.

Runner: pytest tests/test_issue_497_event_tree.py
API: EventTreeStore (run_event_log/run_meta/run_branches SQLite), AgentEvent
event_id/parent_id stamping, run.fork/run.branches/run.branch.activate RPC,
fork_from continuation seeding on graph.execute_async.

User instruction: "修复fusion-agent-studio所有issue和pr，提交代码到代码仓，合并所有分支到主干，发布补丁版本"
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from agent_runtime.context import AgentEvent, AgentEventType
from agent_runtime.event_tree import EventTreeStore
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.persistence import AgentStore
from agent_runtime.runtime import AgentRuntime
from tools import ToolRegistry


def _store(tmp_path):
    return AgentStore(db_path=str(tmp_path / "tree.db"))


def _ev(event_id, parent_id="", ev_type="start", content=""):
    return AgentEvent(
        type=AgentEventType(ev_type), content=content, event_id=event_id, parent_id=parent_id
    )


class TestEventTreeStoreUnit:
    def test_append_skip_token_events(self, tmp_path):
        ts = EventTreeStore(_store(tmp_path))
        ts.register_run("r1")
        ts.append_event("r1", {"type": "token", "content": "a"}, messages=[])
        ts.append_event("r1", {"type": "start", "event_id": "e1"}, messages=[])
        events = ts.list_events("r1")
        assert len(events) == 1
        assert events[0]["event_id"] == "e1"

    def test_append_defaults_ids_and_orders_by_seq(self, tmp_path):
        ts = EventTreeStore(_store(tmp_path))
        ts.register_run("r1")
        # ids come from the runtime stamping; the store only persists what it
        # is given (parent chain built by the caller).
        ts.append_event("r1", {"type": "start", "event_id": "e1"}, messages=[])
        ts.append_event("r1", {"type": "end", "event_id": "e2", "parent_id": "e1"}, messages=[])
        events = ts.list_events("r1")
        assert [e["type"] for e in events] == ["start", "end"]
        assert events[0]["parent_id"] == ""
        assert events[1]["parent_id"] == "e1"

    def test_path_to_root_chain_and_unknown(self, tmp_path):
        ts = EventTreeStore(_store(tmp_path))
        ts.register_run("r1")
        ts.append_event("r1", {"type": "start", "event_id": "e1"}, messages=[])
        ts.append_event("r1", {"type": "tool_call", "event_id": "e2", "parent_id": "e1"}, messages=[])
        ts.append_event("r1", {"type": "end", "event_id": "e3", "parent_id": "e2"}, messages=[])
        path = ts.path_to_root("r1", "e3")
        assert [e["event_id"] for e in path] == ["e1", "e2", "e3"]
        assert ts.path_to_root("r1", "missing") is None
        assert ts.path_to_root("unknown-run", "e1") is None

    def test_messages_at_event_and_last(self, tmp_path):
        ts = EventTreeStore(_store(tmp_path))
        ts.register_run("r1")
        ts.append_event("r1", {"type": "start", "event_id": "e1"}, messages=[])
        ts.append_event(
            "r1", {"type": "end", "event_id": "e2"}, messages=[{"role": "user", "content": "hi"}]
        )
        assert ts.messages_at("r1", "e1") == []
        assert ts.messages_at("r1", "e2") == [{"role": "user", "content": "hi"}]
        # empty event_id = last event snapshot
        assert ts.messages_at("r1") == [{"role": "user", "content": "hi"}]
        assert ts.messages_at("unknown-run") is None

    def test_fork_copies_prefix_and_registers_branch(self, tmp_path):
        ts = EventTreeStore(_store(tmp_path))
        ts.register_run("r1", graph_id="g1", task_id="t1")
        ts.append_event("r1", {"type": "start", "event_id": "e1"}, messages=[])
        ts.append_event(
            "r1",
            {"type": "tool_call", "event_id": "e2", "parent_id": "e1"},
            messages=[{"role": "user", "content": "q"}],
        )
        ts.append_event(
            "r1",
            {"type": "end", "event_id": "e3", "parent_id": "e2"},
            messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        )
        # fork at e2: only prefix e1+e2 copied
        res = ts.fork_run("r1", at_event_id="e2", title="mid fork")
        assert res is not None
        new_run = res["run_id"]
        assert res["parent_run_id"] == "r1"
        assert res["root_run_id"] == "r1"
        assert res["fork_event_id"] == "e2"
        assert res["events_copied"] == 2
        assert res["messages"] == [{"role": "user", "content": "q"}]
        copied = ts.list_events(new_run)
        assert [e["event_id"] for e in copied] == ["e1", "e2"]
        # branch row registered under root family
        listing = ts.list_branches("r1")
        assert listing["root_run_id"] == "r1"
        assert [b["branch_id"] for b in listing["branches"]] == [new_run]
        assert listing["active_branch_id"] == ""

    def test_fork_default_last_event(self, tmp_path):
        ts = EventTreeStore(_store(tmp_path))
        ts.register_run("r1")
        ts.append_event("r1", {"type": "start", "event_id": "e1"}, messages=[])
        ts.append_event("r1", {"type": "end", "event_id": "e2"}, messages=[{"role": "user", "content": "x"}])
        res = ts.fork_run("r1")
        assert res is not None
        assert res["fork_event_id"] == "e2"
        assert res["events_copied"] == 2

    def test_fork_errors(self, tmp_path):
        ts = EventTreeStore(_store(tmp_path))
        assert ts.fork_run("no-such-run") is None
        ts.register_run("r1")
        assert ts.fork_run("r1") is None  # empty log
        ts.append_event("r1", {"type": "start", "event_id": "e1"}, messages=[])
        assert ts.fork_run("r1", at_event_id="missing") is None

    def test_fork_resolves_task_id(self, tmp_path):
        # /v1/runs callers key by task_id — fork must accept it too.
        ts = EventTreeStore(_store(tmp_path))
        ts.register_run("exec_1", task_id="task-9")
        ts.append_event("exec_1", {"type": "start", "event_id": "e1"}, messages=[])
        res = ts.fork_run("task-9")
        assert res is not None
        assert res["parent_run_id"] == "exec_1"

    def test_branch_activate_and_validation(self, tmp_path):
        ts = EventTreeStore(_store(tmp_path))
        ts.register_run("r1")
        ts.append_event("r1", {"type": "start", "event_id": "e1"}, messages=[])
        b1 = ts.fork_run("r1")
        b2 = ts.fork_run("r1")
        assert b1 is not None and b2 is not None
        assert ts.activate_branch("r1", b1["branch_id"]) == {
            "root_run_id": "r1",
            "activated_branch_id": b1["branch_id"],
        }
        listing = ts.list_branches("r1")
        assert listing["active_branch_id"] == b1["branch_id"]
        # activating b2 flips the family pointer
        ts.activate_branch("r1", b2["branch_id"])
        assert ts.list_branches("r1")["active_branch_id"] == b2["branch_id"]
        # unknown branch / unknown run
        assert ts.activate_branch("r1", "nope") is None
        assert ts.activate_branch("no-such-run", b1["branch_id"]) is None
        assert ts.list_branches("no-such-run") is None

    def test_restart_durability(self, tmp_path):
        # Everything in the store db — a fresh process (new AgentStore +
        # EventTreeStore on the same path) still sees events, lineage, branches.
        db_path = str(tmp_path / "tree.db")
        store1 = AgentStore(db_path=db_path)
        ts1 = EventTreeStore(store1)
        ts1.register_run("r1", task_id="t1")
        ts1.append_event("r1", {"type": "start", "event_id": "e1"}, messages=[])
        forked = ts1.fork_run("r1")
        assert forked is not None
        ts1.activate_branch("r1", forked["branch_id"])
        store1.close()

        store2 = AgentStore(db_path=db_path)
        ts2 = EventTreeStore(store2)
        assert [e["event_id"] for e in ts2.list_events("r1")] == ["e1"]
        listing = ts2.list_branches("t1")
        assert listing["root_run_id"] == "r1"
        assert listing["active_branch_id"] == forked["branch_id"]
        meta = ts2.get_run_meta(forked["run_id"])
        assert meta["parent_run_id"] == "r1"
        assert meta["root_run_id"] == "r1"
        store2.close()


class TestAgentEventLinkage:
    def test_roundtrip(self):
        ev = AgentEvent(type=AgentEventType.START, event_id="e1", parent_id="")
        d = ev.to_dict()
        assert d["event_id"] == "e1" and d["parent_id"] == ""
        back = AgentEvent.from_dict(d)
        assert back.event_id == "e1" and back.parent_id == ""

    def test_legacy_payload_without_linkage_keys(self):
        # Old persisted events (no event_id/parent_id) must still load.
        ev = AgentEvent.from_dict({"type": "end", "content": "done"})
        assert ev.event_id == "" and ev.parent_id == ""


def _flat_graph(name="flat"):
    g = AgentGraph(name=name)
    g.id = name
    g.add_node("start", NodeConfig(type="start", label="Start", x=0, y=0))
    g.add_node("end", NodeConfig(type="end", label="End", x=200, y=0))
    g.add_edge("start", "end")
    return g


class TestRuntimeStamping:
    async def test_events_get_chained_ids(self):
        rt = AgentRuntime(tool_registry=ToolRegistry())
        events = [e async for e in rt.execute_graph(_flat_graph(), "hello")]
        assert [e.type.value for e in events] == ["start", "end"]
        assert all(e.event_id for e in events)
        assert events[0].parent_id == ""
        assert events[1].parent_id == events[0].event_id


class TestDaemonForkFlow:
    @pytest.fixture
    def socket_path(self):
        path = tempfile.mktemp(suffix=".sock", dir="/tmp")
        yield path
        if os.path.exists(path):
            os.unlink(path)

    @pytest.fixture
    async def daemon(self, socket_path, tmp_path, monkeypatch):
        # FUSION_OUT_DIR keeps evidence/spans off the user's real home dir.
        monkeypatch.setenv("FUSION_OUT_DIR", str(tmp_path / "out"))
        from agent_runtime.daemon_server import DaemonServer

        d = DaemonServer(
            socket_path=socket_path,
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=str(tmp_path / "test_store.db"),
        )
        await d.start()
        yield d
        await d.stop()

    async def _execute(self, d, graph_id, input_text, extra=None):
        params = {"graph_id": graph_id, "input": input_text}
        if extra:
            params.update(extra)
        res = await d._handle_graph_execute_async(params)
        task = d._active_executions[res["execution_id"]]
        await asyncio.wait_for(task, timeout=10.0)
        return res

    async def test_fork_and_continue_conversation(self, daemon):
        daemon.store.save_graph(_flat_graph("fork-flow"))
        # run 1 — plain start->end run
        run1 = await self._execute(daemon, "fork-flow", "first question")
        tree = daemon._get_event_tree()
        events = tree.list_events(run1["execution_id"])
        assert [e["type"] for e in events] == ["start", "end"]
        # messages snapshot at END carries the conversation
        end_event_id = events[-1]["event_id"]
        msgs = tree.messages_at(run1["execution_id"], end_event_id)
        assert msgs == [{"role": "user", "content": "first question"}]

        # fork at END event
        fork = await daemon._handle_run_fork(
            {"run_id": run1["execution_id"], "at_event_id": end_event_id, "title": "retry"}
        )
        assert fork["events_copied"] == 2
        assert fork["messages"] == [{"role": "user", "content": "first question"}]

        # fork_from continuation — seeded history, new user message appended
        run2 = await self._execute(
            daemon, "fork-flow", "second question", extra={"fork_from": {"run_id": fork["run_id"]}}
        )
        cont_msgs = tree.messages_at(run2["execution_id"])
        assert cont_msgs == [
            {"role": "user", "content": "first question"},
            {"role": "user", "content": "second question"},
        ]
        # lineage recorded on the continuation run
        meta = tree.get_run_meta(run2["execution_id"])
        assert meta["parent_run_id"] == fork["run_id"]
        assert meta["root_run_id"] == run1["execution_id"]

        # branch listing + activation over the family
        listing = await daemon._handle_run_branches({"run_id": run1["execution_id"]})
        branch_ids = [b["branch_id"] for b in listing["branches"]]
        assert fork["run_id"] in branch_ids
        activated = await daemon._handle_run_branch_activate(
            {"run_id": run1["execution_id"], "branch_id": fork["run_id"]}
        )
        assert activated["activated_branch_id"] == fork["run_id"]
        assert (
            daemon._get_event_tree().list_branches(run1["execution_id"])["active_branch_id"]
            == fork["run_id"]
        )

    async def test_fork_accepts_task_id_and_errors_cleanly(self, daemon):
        daemon.store.save_graph(_flat_graph("fork-task"))
        run = await self._execute(
            daemon, "fork-task", "q", extra={"task_id": "task-fork-1"}
        )
        assert run["task_id"] == "task-fork-1"
        # fork keyed by task id (the /v1/runs surface)
        fork = await daemon._handle_run_fork({"run_id": "task-fork-1"})
        assert fork["parent_run_id"] == run["execution_id"]
        # unknown run id -> ValueError
        with pytest.raises(ValueError):
            await daemon._handle_run_fork({"run_id": "no-such-run"})
        # fork_from pointing at an eventless run -> ValueError
        with pytest.raises(ValueError):
            await self._execute(
                daemon, "fork-task", "x", extra={"fork_from": {"run_id": "no-such-run"}}
            )
        with pytest.raises(ValueError):
            await daemon._handle_run_branches({"run_id": "no-such-run"})
        with pytest.raises(ValueError):
            await daemon._handle_run_branch_activate(
                {"run_id": run["execution_id"], "branch_id": "nope"}
            )

    async def test_branch_registry_survives_new_store_instance(self, daemon, tmp_path):
        # daemon restart == new AgentStore + EventTreeStore on the same db file.
        daemon.store.save_graph(_flat_graph("fork-durable"))
        run = await self._execute(daemon, "fork-durable", "d")
        fork = await daemon._handle_run_fork({"run_id": run["execution_id"]})
        db_path = daemon.store.db_path
        store2 = AgentStore(db_path=db_path)
        tree2 = EventTreeStore(store2)
        listing = tree2.list_branches(run["execution_id"])
        assert fork["run_id"] in [b["branch_id"] for b in listing["branches"]]
        assert [e["type"] for e in tree2.list_events(run["execution_id"])] == ["start", "end"]
        store2.close()
