"""#297: graph.create/graph.update preserve full NodeConfig + position.

Client (fusion-studio AgentWorkflowCanvas) nests full NodeConfig under a
`config` key and sends `position` separately. Server must merge both into
flat NodeConfig fields. Also covers from_dict tolerance of unknown keys.
"""

from __future__ import annotations

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.graph import AgentGraph, NodeConfig


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


class TestIssue297NodeConfigFromPayload:
    def test_merges_nested_config(self, daemon):
        n = {
            "id": "n1",
            "type": "tool",
            "label": "Fetch",
            "config": {"tool_name": "http_get", "tool_params": {"url": "x"}},
        }
        cfg = daemon._node_config_from_payload(n)
        assert cfg.tool_name == "http_get"
        assert cfg.tool_params == {"url": "x"}
        assert cfg.type == "tool"

    def test_merges_position(self, daemon):
        n = {
            "id": "n1",
            "type": "llm",
            "label": "L",
            "position": {"x": 1.0, "y": 2.0},
        }
        cfg = daemon._node_config_from_payload(n)
        assert cfg.x == 1.0
        assert cfg.y == 2.0

    def test_flat_top_level_back_compat(self, daemon):
        n = {
            "id": "n1",
            "type": "condition",
            "label": "C",
            "condition_expr": "iteration >= 3",
        }
        cfg = daemon._node_config_from_payload(n)
        assert cfg.condition_expr == "iteration >= 3"

    def test_nested_config_overrides_flat(self, daemon):
        # explicit nested config wins over flat top-level for same field.
        n = {
            "id": "n1",
            "type": "llm",
            "label": "L",
            "model": "flat-model",
            "config": {"model": "nested-model"},
        }
        cfg = daemon._node_config_from_payload(n)
        assert cfg.model == "nested-model"

    def test_unknown_keys_filtered(self, daemon):
        n = {
            "id": "n1",
            "type": "llm",
            "label": "L",
            "config": {"model": "m", "future_field": "ignored"},
            "position": {"x": 5.0, "z_index": 99},
        }
        cfg = daemon._node_config_from_payload(n)
        assert cfg.model == "m"
        assert not hasattr(cfg, "future_field")

    def test_preserves_all_node_types_fields(self, daemon):
        n = {
            "id": "n1",
            "type": "llm",
            "label": "L",
            "config": {
                "model": "gpt",
                "system_prompt": "sp",
                "temperature": 0.3,
                "max_tokens": 512,
                "effort": "high",
                "tool_choice": "auto",
                "parallel_tool_calls": True,
                "loop_mode": "agent",
                "max_loop_iterations": 4,
                "disable_tools": True,
            },
        }
        cfg = daemon._node_config_from_payload(n)
        assert cfg.temperature == 0.3
        assert cfg.max_tokens == 512
        assert cfg.effort == "high"
        assert cfg.tool_choice == "auto"
        assert cfg.parallel_tool_calls is True
        assert cfg.loop_mode == "agent"
        assert cfg.max_loop_iterations == 4
        assert cfg.disable_tools is True


class TestIssue297GraphCreateRoundTrip:
    @pytest.mark.asyncio
    async def test_create_preserves_nested_config_and_position(self, daemon):
        result = await _run(
            daemon,
            "graph.create",
            {
                "name": "RoundTrip",
                "nodes": [
                    {"id": "start", "type": "start", "label": "Start"},
                    {
                        "id": "tool1",
                        "type": "tool",
                        "label": "Fetch",
                        "config": {"tool_name": "http_get"},
                        "position": {"x": 10.0, "y": 20.0},
                    },
                    {
                        "id": "cond1",
                        "type": "condition",
                        "label": "Gate",
                        "config": {"condition_expr": "iteration >= 2"},
                    },
                    {"id": "end", "type": "end", "label": "End"},
                ],
                "edges": [
                    {"source_id": "start", "target_id": "tool1"},
                    {"source_id": "tool1", "target_id": "cond1"},
                    {"source_id": "cond1", "target_id": "end"},
                ],
            },
        )
        nodes = result["nodes"]
        assert nodes["tool1"]["tool_name"] == "http_get"
        assert nodes["tool1"]["x"] == 10.0
        assert nodes["tool1"]["y"] == 20.0
        assert nodes["cond1"]["condition_expr"] == "iteration >= 2"

    @pytest.mark.asyncio
    async def test_create_flat_fields_still_work(self, daemon):
        # back-compat: flat top-level model/system_prompt (pre-#297 style).
        result = await _run(
            daemon,
            "graph.create",
            {
                "name": "Flat",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {
                        "id": "llm1",
                        "type": "llm",
                        "model": "flat-model",
                        "system_prompt": "be brief",
                    },
                    {"id": "end", "type": "end"},
                ],
                "edges": [
                    {"source_id": "start", "target_id": "llm1"},
                    {"source_id": "llm1", "target_id": "end"},
                ],
            },
        )
        assert result["nodes"]["llm1"]["model"] == "flat-model"
        assert result["nodes"]["llm1"]["system_prompt"] == "be brief"


class TestIssue297GraphUpdateRoundTrip:
    @pytest.mark.asyncio
    async def test_update_preserves_nested_config(self, daemon):
        create = await _run(
            daemon,
            "graph.create",
            {
                "name": "U",
                "nodes": [{"id": "start", "type": "start"}, {"id": "end", "type": "end"}],
                "edges": [{"source_id": "start", "target_id": "end"}],
            },
        )
        gid = create["graph_id"]
        upd = await _run(
            daemon,
            "graph.update",
            {
                "graph_id": gid,
                "nodes": [
                    {"id": "start", "type": "start"},
                    {
                        "id": "tool1",
                        "type": "tool",
                        "config": {"tool_name": "git_status"},
                        "position": {"x": 3.0, "y": 4.0},
                    },
                    {"id": "end", "type": "end"},
                ],
                "edges": [
                    {"source_id": "start", "target_id": "tool1"},
                    {"source_id": "tool1", "target_id": "end"},
                ],
            },
        )
        assert upd["nodes"]["tool1"]["tool_name"] == "git_status"
        assert upd["nodes"]["tool1"]["x"] == 3.0
        assert upd["nodes"]["tool1"]["y"] == 4.0


class TestIssue297NodeConfigFromDictTolerance:
    def test_from_dict_ignores_unknown_keys(self):
        cfg = NodeConfig.from_dict(
            {"type": "llm", "label": "L", "model": "m", "unknown_future": 1}
        )
        assert cfg.model == "m"
        assert cfg.type == "llm"
        assert not hasattr(cfg, "unknown_future")

    def test_from_dict_preserves_all_known(self):
        cfg = NodeConfig.from_dict(
            {
                "type": "llm",
                "label": "L",
                "temperature": 0.1,
                "max_tokens": 100,
                "tool_choice": "required",
                "parallel_tool_calls": True,
                "x": 7.0,
                "y": 8.0,
            }
        )
        assert cfg.temperature == 0.1
        assert cfg.max_tokens == 100
        assert cfg.tool_choice == "required"
        assert cfg.parallel_tool_calls is True
        assert cfg.x == 7.0
        assert cfg.y == 8.0

    def test_graph_from_dict_tolerant_node_fields(self):
        # AgentGraph.from_dict -> NodeConfig.from_dict path must not crash
        # on a node dict carrying config/position/unknown extras.
        g = AgentGraph.from_dict(
            {
                "id": "g1",
                "name": "G",
                "nodes": {
                    "n1": {
                        "type": "tool",
                        "label": "T",
                        "tool_name": "t",
                        "x": 1.0,
                        "y": 2.0,
                        "config": {"future": 1},
                    }
                },
                "edges": [],
                "start_node_id": "n1",
            }
        )
        assert g.nodes["n1"].tool_name == "t"
        assert g.nodes["n1"].x == 1.0
