"""#484: canvas residual — graph versioning (immutable per-save version
records, list/restore) on the store and via daemon RPCs."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.persistence import AgentStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        s = AgentStore(str(db_path))
        yield s
        s.close()


@pytest.fixture
def daemon(tmp_path):
    db = tmp_path / "test_store.db"
    d = DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=str(db),
    )
    yield d


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    return await handler(params or {})


def _make_graph(name="g", label="hello"):
    graph = AgentGraph(name=name, description="")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("end", NodeConfig(type="end", label=label))
    graph.add_edge("start", "end")
    return graph


class TestStoreVersioning:
    def test_each_save_records_version(self, store):
        graph = _make_graph()
        store.save_graph(graph)
        graph.nodes["end"].label = "second"
        store.save_graph(graph)
        versions = store.list_graph_versions(graph.id)
        assert [v["version"] for v in versions] == [2, 1]

    def test_version_records_immutable_content(self, store):
        graph = _make_graph()
        store.save_graph(graph)
        graph.nodes["end"].label = "changed"
        store.save_graph(graph)
        v1 = store.load_graph_version(graph.id, 1)
        assert v1 is not None
        assert v1.nodes["end"].label == "hello"

    def test_load_missing_version_returns_none(self, store):
        graph = _make_graph()
        store.save_graph(graph)
        assert store.load_graph_version(graph.id, 99) is None
        assert store.load_graph_version("no-such-graph", 1) is None

    def test_list_has_no_data_blob(self, store):
        graph = _make_graph()
        store.save_graph(graph)
        versions = store.list_graph_versions(graph.id)
        assert len(versions) == 1
        assert "data" not in versions[0]
        assert versions[0]["graph_id"] == graph.id

    def test_history_cap_prunes_old_versions(self, store):
        graph = _make_graph()
        for i in range(AgentStore.GRAPH_VERSION_HISTORY_CAP + 10):
            graph.name = f"g-{i}"
            store.save_graph(graph)
        versions = store.list_graph_versions(graph.id)
        assert len(versions) == AgentStore.GRAPH_VERSION_HISTORY_CAP
        # newest kept, oldest pruned
        assert versions[0]["version"] == AgentStore.GRAPH_VERSION_HISTORY_CAP + 10
        assert versions[-1]["version"] == 11

    def test_versions_scoped_per_graph(self, store):
        a = _make_graph(name="a")
        b = _make_graph(name="b")
        store.save_graph(a)
        store.save_graph(a)
        store.save_graph(b)
        assert len(store.list_graph_versions(a.id)) == 2
        assert len(store.list_graph_versions(b.id)) == 1


class TestDaemonVersionRpcs:
    async def test_version_list_rpc(self, daemon):
        created = await _run(
            daemon,
            "graph.create",
            {"name": "v-graph", "graph_data": {"nodes": {}, "edges": []}},
        )
        gid = created["graph_id"]
        res = await _run(daemon, "graph.version_list", {"graph_id": gid})
        assert res["graph_id"] == gid
        assert len(res["versions"]) == 1
        assert res["versions"][0]["version"] == 1

    async def test_update_then_restore_roundtrip(self, daemon):
        created = await _run(
            daemon,
            "graph.create",
            {
                "name": "v-graph",
                "graph_data": {
                    "nodes": {
                        "start": {"type": "start", "label": "Start"},
                        "end": {"type": "end", "label": "hello"},
                    },
                    "edges": [{"source": "start", "target": "end"}],
                },
            },
        )
        gid = created["graph_id"]

        await _run(
            daemon,
            "graph.update",
            {
                "graph_id": gid,
                "graph_data": {
                    "nodes": {
                        "start": {"type": "start", "label": "Start"},
                        "end": {"type": "end", "label": "rewritten"},
                    },
                    "edges": [{"source": "start", "target": "end"}],
                },
            },
        )
        res = await _run(daemon, "graph.version_list", {"graph_id": gid})
        assert len(res["versions"]) == 2

        restored = await _run(
            daemon, "graph.version_restore", {"graph_id": gid, "version": 1}
        )
        assert restored["restored_from"] == 1
        assert restored["nodes"]["end"]["label"] == "hello"

        # restore recorded as a new version; current graph is the restored one
        res = await _run(daemon, "graph.version_list", {"graph_id": gid})
        assert len(res["versions"]) == 3
        got = await _run(daemon, "graph.get", {"graph_id": gid})
        assert got["nodes"]["end"]["label"] == "hello"

    async def test_restore_missing_version_errors(self, daemon):
        created = await _run(
            daemon,
            "graph.create",
            {"name": "v-graph", "graph_data": {"nodes": {}, "edges": []}},
        )
        gid = created["graph_id"]
        with pytest.raises(ValueError, match="Version not found"):
            await _run(daemon, "graph.version_restore", {"graph_id": gid, "version": 42})

    async def test_version_list_requires_graph_id(self, daemon):
        with pytest.raises(ValueError, match="graph_id"):
            await _run(daemon, "graph.version_list", {})

