import asyncio

import pytest

from agent_runtime.daemon_server import DaemonServer


@pytest.fixture()
def daemon(tmp_path, monkeypatch):
    from agent_runtime.persistence import AgentStore

    monkeypatch.setenv("FUSION_HOME_DIR", str(tmp_path))
    d = DaemonServer.__new__(DaemonServer)
    d.store = AgentStore(db_path=str(tmp_path / "store.db"))
    d._cached_tool_registry = None
    return d


def _gid(d):
    return asyncio.run(d._handle_graph_create({
        "name": "T",
        "graph_data": {"nodes": {"n1": {"type": "start"}, "n2": {"type": "llm"}},
                       "edges": [{"source_id": "n1", "target_id": "n2"}]},
    }))["graph_id"]


def test_update_record_form(daemon):
    gid = _gid(daemon)
    res = asyncio.run(daemon._handle_graph_update({
        "graph_id": gid,
        "graph_data": {
            "name": "Renamed",
            "nodes": {"n1": {"type": "start", "label": "S1", "x": 10, "y": 20},
                      "n2": {"type": "llm", "label": "L1", "model": "qwen"},
                      "n3": {"type": "end"}},
            "edges": [{"source_id": "n1", "target_id": "n2", "label": "go"},
                      {"source_id": "n2", "target_id": "n3"}],
        },
    }))
    assert res["name"] == "Renamed"
    assert res["nodes"]["n1"]["label"] == "S1"
    assert res["nodes"]["n1"]["x"] == 10
    assert res["nodes"]["n2"]["model"] == "qwen"
    assert res["edges"][0]["label"] == "go"


def test_update_list_form_backcompat(daemon):
    gid = _gid(daemon)
    res = asyncio.run(daemon._handle_graph_update({
        "graph_id": gid,
        "nodes": [{"id": "n1", "type": "start", "label": "LIST"}],
    }))
    assert res["nodes"]["n1"]["label"] == "LIST"


def test_update_record_nested_config(daemon):
    gid = _gid(daemon)
    res = asyncio.run(daemon._handle_graph_update({
        "graph_id": gid,
        "graph_data": {
            "nodes": {"n2": {"type": "llm", "config": {"model": "m2"}, "position": {"x": 5, "y": 6}}},
            "edges": [],
        },
    }))
    assert res["nodes"]["n2"]["model"] == "m2"
    assert res["nodes"]["n2"]["x"] == 5


def test_update_unknown_node_keys_dropped(daemon):
    gid = _gid(daemon)
    res = asyncio.run(daemon._handle_graph_update({
        "graph_id": gid,
        "graph_data": {"nodes": {"n1": {"type": "start", "bogus_key": 1}},
                       "edges": []},
    }))
    assert "bogus_key" not in res["nodes"]["n1"]
