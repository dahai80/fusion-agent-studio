"""#568: NL graph assembly — graph.nl_plan registry-grounded op planning.

ACs:
1. LLM plan parsed from reply (fenced JSON ok) into validated ops.
2. Registry grounding: unknown node types rejected with actionable list.
3. Wiring validation: connect/remove/set_config targets must exist; add_node
   duplicate ids rejected with set_config hint.
4. Template grounding: instantiate_template limited to provided templates.
5. Catalog fallback to server node types when catalog omitted.
6. Handler is async and registered as graph.nl_plan.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_runtime.dispatchers.flow import FlowDispatcher
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from server.fusion_mlx_client import LLMResponse

CATALOG = [
    {"type": "start", "label": "Start"},
    {"type": "end", "label": "End"},
    {"type": "llm", "label": "LLM"},
    {"type": "human_input", "label": "Approval"},
]

GRAPH_STATE = {
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "llm_1", "type": "llm"},
        {"id": "end", "type": "end"},
    ],
    "edges": [{"source_id": "start", "target_id": "llm_1"}],
}

TEMPLATES = [
    {"name": "rag-qa", "description": "RAG QA"},
    {"name": "basic-llm-prompt", "description": "simple"},
]


class _Daemon:
    pass


class StubGateway(LLMGateway):
    def __init__(self, reply: str, calls: list | None = None):
        super().__init__()
        self._reply = reply
        self.calls = calls if calls is not None else []

    async def chat(self, messages, model="", capability="", tools=None, **kwargs):
        self.calls.append({"messages": messages, "model": model})
        return LLMResponse(
            content=self._reply,
            tool_calls=[],
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            finish_reason="stop",
        )


def _dispatcher(reply: str, calls: list | None = None) -> FlowDispatcher:
    d = _Daemon()
    rt = type("R", (), {})()
    rt.llm_gateway = StubGateway(reply, calls)
    d._get_runtime = lambda: rt
    return FlowDispatcher(d)


def _plan(reply: str, **params) -> dict:
    params.setdefault("prompt", "搭一个流程")
    params.setdefault("catalog", CATALOG)
    params.setdefault("graph_json", GRAPH_STATE)
    params.setdefault("templates", TEMPLATES)
    return asyncio.run(_dispatcher(reply)._handle_graph_nl_plan(params))


def test_handler_registered_async():
    handlers = FlowDispatcher(None).get_handlers()
    assert "graph.nl_plan" in handlers
    assert asyncio.iscoroutinefunction(handlers["graph.nl_plan"])


def test_valid_plan_returns_ops_and_reply():
    reply = """Here you go:
```json
{"reply": "已创建审批流程", "ops": [
  {"op": "add_node", "id": "approve", "type": "human_input", "label": "Approval", "config": {}},
  {"op": "connect", "source": "llm_1", "target": "approve"},
  {"op": "connect", "source": "approve", "target": "end"}
]}
```
"""
    out = _plan(reply)
    assert "error" not in out
    assert out["reply"] == "已创建审批流程"
    assert [o["op"] for o in out["ops"]] == ["add_node", "connect", "connect"]
    # chained validation: connect to approve (added earlier in same list) is legal
    assert out["ops"][2]["target"] == "end"


def test_prompt_and_catalog_reach_llm_messages():
    calls: list = []
    reply = '{"reply": "ok", "ops": []}'
    d = _dispatcher(reply, calls)
    asyncio.run(
        d._handle_graph_nl_plan(
            {
                "prompt": "build a review flow",
                "catalog": CATALOG,
                "graph_json": GRAPH_STATE,
                "templates": TEMPLATES,
                "model": "qwen3.5-9b",
            }
        )
    )
    assert len(calls) == 1
    msgs = calls[0]["messages"]
    assert msgs[-1]["content"] == "build a review flow"
    assert "human_input" in msgs[0]["content"]
    assert "rag-qa" in msgs[0]["content"]
    assert "llm_1" in msgs[0]["content"], "current graph state grounded into prompt"
    assert calls[0]["model"] == "qwen3.5-9b"


def test_unknown_node_type_rejected_with_catalog_list():
    reply = json_reply(
        {"reply": "", "ops": [{"op": "add_node", "id": "x", "type": "super_agent"}]}
    )
    out = _plan(reply)
    assert out["error"] == "invalid plan ops"
    assert any("super_agent" in d and "allowed" in d for d in out["details"])
    assert "human_input" in out["allowed_types"]


def test_connect_unknown_ids_rejected():
    reply = json_reply(
        {
            "reply": "",
            "ops": [{"op": "connect", "source": "llm_1", "target": "ghost"}],
        }
    )
    out = _plan(reply)
    assert "invalid plan ops" in out["error"]
    assert any("'ghost'" in d for d in out["details"])


def test_add_duplicate_id_rejected_with_set_config_hint():
    reply = json_reply(
        {
            "reply": "",
            "ops": [{"op": "add_node", "id": "llm_1", "type": "llm"}],
        }
    )
    out = _plan(reply)
    assert "invalid plan ops" in out["error"]
    assert any("set_config" in d for d in out["details"])


def test_remove_unknown_id_rejected():
    reply = json_reply({"reply": "", "ops": [{"op": "remove_node", "id": "nope"}]})
    out = _plan(reply)
    assert "invalid plan ops" in out["error"]


def test_unknown_template_rejected():
    reply = json_reply(
        {"reply": "", "ops": [{"op": "instantiate_template", "name": "no-such"}]}
    )
    out = _plan(reply)
    assert "invalid plan ops" in out["error"]
    assert any("rag-qa" in d for d in out["details"])


def test_known_template_accepted():
    reply = json_reply(
        {"reply": "用模板", "ops": [{"op": "instantiate_template", "name": "rag-qa"}]}
    )
    out = _plan(reply)
    assert "error" not in out and out["ops"][0]["name"] == "rag-qa"


def test_clear_graph_allows_subsequent_adds():
    reply = json_reply(
        {
            "reply": "",
            "ops": [
                {"op": "clear_graph"},
                {"op": "add_node", "id": "s", "type": "start"},
                {"op": "connect", "source": "s", "target": "s"},
            ],
        }
    )
    out = _plan(reply)
    # connect s->s legal after clear (s added in same list); only ops validity checked
    assert "error" not in out


def test_remove_frees_id_for_readd():
    reply = json_reply(
        {
            "reply": "",
            "ops": [
                {"op": "remove_node", "id": "llm_1"},
                {"op": "add_node", "id": "llm_1", "type": "llm"},
            ],
        }
    )
    out = _plan(reply)
    assert "error" not in out


def test_catalog_fallback_to_server_types():
    reply = json_reply(
        {"reply": "", "ops": [{"op": "add_node", "id": "v", "type": "verify"}]}
    )
    out = _plan(reply, catalog=[])
    assert "error" not in out


def test_non_json_reply_rejected_with_raw():
    out = _plan("I would build a nice flow for you!")
    assert "not valid plan JSON" in out["error"]
    assert out["raw"]


def test_empty_prompt_rejected():
    out = asyncio.run(
        _dispatcher("{}")._handle_graph_nl_plan({"prompt": "   "})
    )
    assert out["error"] == "prompt required"


def test_ops_as_bare_list_accepted():
    reply = json_reply([{"op": "connect", "source": "start", "target": "llm_1"}])
    out = _plan(reply)
    assert "error" not in out and out["ops"][0]["op"] == "connect"


def json_reply(obj) -> str:
    import json

    return f"```json\n{json.dumps(obj, ensure_ascii=False)}\n```"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
