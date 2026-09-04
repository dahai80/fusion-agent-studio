"""#282/#283/#284: fusion-osagent integration hooks.

#282: AX-tree-backed locator mode for MouseTool/KeyboardTool (env-gated).
#283: model_router callback on LLM nodes for fast/slow dual-core.
#284: post-action screen capture + assertion hook on tool nodes.
All default-off / env-gated; current behavior preserved when unset.
"""

from __future__ import annotations

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from tools.registry import ToolRegistry


class ModelRecordingClient:
    def __init__(self):
        self.models_used: list[str] = []
        self.responses: list[str] = []

    def add_response(self, content: str):
        self.responses.append(content)

    async def chat(
        self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs
    ):
        self.models_used.append(model)
        from server.fusion_mlx_client import LLMResponse

        content = self.responses.pop(0) if self.responses else "ok"
        return LLMResponse(content=content, tool_calls=[])

    async def chat_stream(self, **kwargs):
        return None

    def set_compactor(self, compactor):
        pass


# ── #283: model_router ──


async def test_model_router_overrides_model():
    client = ModelRecordingClient()
    client.add_response("slow done")
    registry = ToolRegistry()

    graph = AgentGraph(name="Router")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    llm = NodeConfig(type="llm", label="LLM", model="fast-7b")
    # router: always pick slow model
    llm.model_router = lambda node, model, prior: "slow-27b"
    graph.add_node("llm", llm)
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")

    runtime = AgentRuntime(client, registry)
    async for _ in runtime.execute_graph(graph, "complex task"):
        pass

    assert client.models_used == ["slow-27b"], client.models_used


async def test_model_router_empty_keeps_current_model():
    client = ModelRecordingClient()
    client.add_response("done")
    registry = ToolRegistry()

    graph = AgentGraph(name="NoRouter")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("llm", NodeConfig(type="llm", label="LLM", model="base-model"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")

    runtime = AgentRuntime(client, registry)
    async for _ in runtime.execute_graph(graph, "hi"):
        pass

    assert client.models_used == ["base-model"], client.models_used


async def test_model_router_returning_empty_falls_back():
    client = ModelRecordingClient()
    client.add_response("done")
    registry = ToolRegistry()

    graph = AgentGraph(name="RouterFallback")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    llm = NodeConfig(type="llm", label="LLM", model="base-model")
    llm.model_router = lambda node, model, prior: ""
    graph.add_node("llm", llm)
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")

    runtime = AgentRuntime(client, registry)
    async for _ in runtime.execute_graph(graph, "hi"):
        pass

    assert client.models_used == ["base-model"], client.models_used


async def test_model_router_raising_falls_back():
    client = ModelRecordingClient()
    client.add_response("done")
    registry = ToolRegistry()

    graph = AgentGraph(name="RouterRaises")
    graph.add_node("start", NodeConfig(type="start", label="Start"))

    def bad_router(node, model, prior):
        raise RuntimeError("router boom")

    llm = NodeConfig(type="llm", label="LLM", model="base-model")
    llm.model_router = bad_router
    graph.add_node("llm", llm)
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")

    runtime = AgentRuntime(client, registry)
    async for _ in runtime.execute_graph(graph, "hi"):
        pass

    assert client.models_used == ["base-model"], client.models_used


def test_node_config_model_router_not_serialized():
    node = NodeConfig(type="llm", label="L", model="m")
    node.model_router = lambda n, m, p: "x"
    d = node.to_dict()
    assert "model_router" not in d, "model_router must NOT be in to_dict (callable)"


def test_node_config_post_action_capture_serialized():
    node = NodeConfig(
        type="tool",
        label="T",
        tool_name="mouse",
        post_action_capture=True,
        assertion={"type": "ui_changed"},
    )
    d = node.to_dict()
    assert d["post_action_capture"] is True
    assert d["assertion"] == {"type": "ui_changed"}
    restored = NodeConfig.from_dict(d)
    assert restored.post_action_capture is True
    assert restored.assertion == {"type": "ui_changed"}


def test_node_config_defaults_off():
    node = NodeConfig(type="llm", label="L")
    assert node.model_router == ""
    assert node.post_action_capture is False
    assert node.assertion == {}


# ── #282: AX locator ──


def test_ax_locator_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FUSION_AX_LOCATOR_ENABLED", raising=False)
    from tools.computer_use_tools import _ax_locator_enabled

    assert _ax_locator_enabled() is False


def test_ax_locator_enabled_when_set(monkeypatch):
    monkeypatch.setenv("FUSION_AX_LOCATOR_ENABLED", "1")
    from tools.computer_use_tools import _ax_locator_enabled

    assert _ax_locator_enabled() is True


def test_ax_locator_missing_fusion_executor_returns_error(monkeypatch):
    monkeypatch.setenv("FUSION_AX_LOCATOR_ENABLED", "1")
    import sys

    # force ImportError on fusion_executor
    monkeypatch.setitem(sys.modules, "fusion_executor", None)
    from tools.computer_use_tools import _resolve_ax_locator

    res = _resolve_ax_locator({"ax_label": "Submit"})
    assert isinstance(res, str)
    assert res.startswith("Error:")


def test_ax_locator_empty_query_returns_error(monkeypatch):
    monkeypatch.setenv("FUSION_AX_LOCATOR_ENABLED", "1")
    from tools.computer_use_tools import _resolve_ax_locator

    res = _resolve_ax_locator({})
    assert isinstance(res, str)
    assert "Error:" in res


def test_ax_locator_structured_match(monkeypatch):
    monkeypatch.setenv("FUSION_AX_LOCATOR_ENABLED", "1")
    from tools.computer_use_tools import _resolve_ax_locator

    fake_tree = [
        {"ax_label": "Cancel", "ax_role": "AXButton", "frame": {"x": 10, "y": 20, "width": 80, "height": 30}},
        {"ax_label": "Submit", "ax_role": "AXButton", "frame": {"x": 100, "y": 200, "width": 60, "height": 40}},
    ]

    class _FakeResult:
        node_tree = __import__("json").dumps(fake_tree)

    class _FakeExecutor:
        def gui_action(self, action):
            return _FakeResult()

    import sys
    import types
    from unittest.mock import patch

    fake_mod = types.ModuleType("fusion_executor")
    fake_mod.Executor = _FakeExecutor
    with patch.dict(sys.modules, {"fusion_executor": fake_mod}):
        res = _resolve_ax_locator({"ax_label": "Submit"})
    assert res == (130, 220)


def test_ax_locator_no_match_returns_error(monkeypatch):
    monkeypatch.setenv("FUSION_AX_LOCATOR_ENABLED", "1")
    from tools.computer_use_tools import _resolve_ax_locator

    fake_tree = [{"ax_label": "Other", "ax_role": "AXButton", "frame": {"x": 1, "y": 2, "width": 3, "height": 4}}]

    class _FakeResult:
        node_tree = __import__("json").dumps(fake_tree)

    class _FakeExecutor:
        def gui_action(self, action):
            return _FakeResult()

    import sys
    import types
    from unittest.mock import patch

    fake_mod = types.ModuleType("fusion_executor")
    fake_mod.Executor = _FakeExecutor
    with patch.dict(sys.modules, {"fusion_executor": fake_mod}):
        res = _resolve_ax_locator({"ax_label": "Nonexistent"})
    assert isinstance(res, str)
    assert "no match" in res.lower()


# ── #284: post-action capture + assertion ──


class _FakeScreenCapture:
    name = "screenshot"
    description = "fake"
    parameters = {}

    async def execute(self, **kwargs):
        import json

        return json.dumps({"path": "", "width": 100, "height": 50})


async def test_post_action_capture_off_by_default():
    client = ModelRecordingClient()
    registry = ToolRegistry()

    graph = AgentGraph(name="NoCapture")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "tool",
        NodeConfig(type="tool", label="T", tool_name="echo", tool_params={}),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "tool")
    graph.add_edge("tool", "end")

    events: list = []
    runtime = AgentRuntime(client, registry)
    async for e in runtime.execute_graph(graph, "hi"):
        events.append(e)

    capture_events = [e for e in events if getattr(e, "name", "") == "post_action_capture"]
    assert len(capture_events) == 0, "post_action_capture off by default"


async def test_post_action_capture_emits_event():
    client = ModelRecordingClient()
    registry = ToolRegistry()
    # register a fake screenshot tool
    from tools.base import BaseTool

    class _Cap(BaseTool):
        name = "screenshot"
        description = "fake cap"
        parameters = {}

        async def execute(self, **kwargs):
            import json

            return json.dumps({"path": "", "width": 100, "height": 50})

    registry.register(_Cap())
    # need an echo tool too
    class _Echo(BaseTool):
        name = "echo"
        description = "echo"
        parameters = {"text": {"type": "string"}}

        async def execute(self, **kwargs):
            return "echoed"

    registry.register(_Echo())

    graph = AgentGraph(name="Capture")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "tool",
        NodeConfig(
            type="tool",
            label="T",
            tool_name="echo",
            tool_params={},
            post_action_capture=True,
        ),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "tool")
    graph.add_edge("tool", "end")

    runtime = AgentRuntime(client, registry)
    events = []
    async for e in runtime.execute_graph(graph, "hi"):
        events.append(e)

    capture_events = [e for e in events if getattr(e, "name", "") == "post_action_capture"]
    assert len(capture_events) == 1, "expected one post_action_capture event"
    assert capture_events[0].metadata["post_action_capture"] is True
    assert capture_events[0].metadata["width"] == 100


async def test_post_action_assertion_failure_emits_error():
    client = ModelRecordingClient()
    registry = ToolRegistry()
    from tools.base import BaseTool

    class _Cap(BaseTool):
        name = "screenshot"
        description = "fake cap"
        parameters = {}

        async def execute(self, **kwargs):
            import json

            return json.dumps({"path": "", "width": 100, "height": 50})

    registry.register(_Cap())

    class _Echo(BaseTool):
        name = "echo"
        description = "echo"
        parameters = {"text": {"type": "string"}}

        async def execute(self, **kwargs):
            return "echoed"

    registry.register(_Echo())

    graph = AgentGraph(name="AssertFail")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "tool",
        NodeConfig(
            type="tool",
            label="T",
            tool_name="echo",
            tool_params={},
            post_action_capture=True,
            assertion={"type": "ui_changed"},
        ),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "tool")
    graph.add_edge("tool", "end")

    runtime = AgentRuntime(client, registry)
    runtime.post_action_assertion_fn = lambda ctx, node, res, b64, w, h: "UI did not change"

    events = []
    async for e in runtime.execute_graph(graph, "hi"):
        events.append(e)

    assert_events = [
        e for e in events
        if getattr(e, "name", "") == "post_action_assertion"
        and e.type == AgentEventType.ERROR
    ]
    assert len(assert_events) == 1, "expected assertion-failure error event"
    assert "UI did not change" in assert_events[0].content
    assert assert_events[0].metadata["assertion_failed"] is True


async def test_post_action_assertion_pass_no_error():
    client = ModelRecordingClient()
    registry = ToolRegistry()
    from tools.base import BaseTool

    class _Cap(BaseTool):
        name = "screenshot"
        description = "fake cap"
        parameters = {}

        async def execute(self, **kwargs):
            import json

            return json.dumps({"path": "", "width": 100, "height": 50})

    registry.register(_Cap())

    class _Echo(BaseTool):
        name = "echo"
        description = "echo"
        parameters = {"text": {"type": "string"}}

        async def execute(self, **kwargs):
            return "echoed"

    registry.register(_Echo())

    graph = AgentGraph(name="AssertPass")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "tool",
        NodeConfig(
            type="tool",
            label="T",
            tool_name="echo",
            tool_params={},
            post_action_capture=True,
            assertion={"type": "ui_changed"},
        ),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "tool")
    graph.add_edge("tool", "end")

    runtime = AgentRuntime(client, registry)
    runtime.post_action_assertion_fn = lambda ctx, node, res, b64, w, h: ""

    events = []
    async for e in runtime.execute_graph(graph, "hi"):
        events.append(e)

    assert_errors = [
        e for e in events
        if getattr(e, "name", "") == "post_action_assertion"
        and e.type == AgentEventType.ERROR
    ]
    assert len(assert_errors) == 0, "passing assertion must not emit error"
