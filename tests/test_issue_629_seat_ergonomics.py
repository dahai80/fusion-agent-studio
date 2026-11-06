
from agent_runtime.seat_pack import load_seat_specs
from tools.base import BaseTool
from tools.file_tools import FileReadTool, FileWriteTool


class _FakeTool(BaseTool):
    name = "fake629"
    description = "fake tool for #629 schema check"
    parameters = {
        "path": {
            "type": "string",
            "description": "path",
        },
        "encoding": {
            "type": "string",
            "description": "encoding",
            "default": "utf-8",
        },
    }

    async def execute(self, **kwargs) -> str:
        return "ok"


def test_schema_defaulted_params_not_required():
    # #629 defect 1: encoding has a default in schema and code, but
    # openai_schema() listed every param as required, so models that
    # omitted it burned turns on a missing-optional arg.
    schema = _FakeTool().openai_schema()["function"]["parameters"]
    assert schema["required"] == ["path"]
    assert schema["properties"]["encoding"]["default"] == "utf-8"


def test_file_read_write_schemas_default_free():
    for tool in (FileReadTool(), FileWriteTool()):
        req = tool.openai_schema()["function"]["parameters"]["required"]
        assert "encoding" not in req
        assert "path" in req


def test_validate_tool_args_fills_schema_default():
    from agent_runtime._runtime_dynamic_tools import _DynamicToolsMixin

    tool = FileReadTool()
    out = _DynamicToolsMixin._validate_tool_args(None, tool, {"path": "a.md"})
    assert out["path"] == "a.md"
    assert out["encoding"] == "utf-8"


def test_validate_tool_args_keeps_explicit_value():
    from agent_runtime._runtime_dynamic_tools import _DynamicToolsMixin

    tool = FileReadTool()
    out = _DynamicToolsMixin._validate_tool_args(None, tool, {"path": "a.md", "encoding": "latin-1"})
    assert out["encoding"] == "latin-1"


def _spec(seat_id):
    specs = load_seat_specs()
    return specs[seat_id]


def test_architect_can_read_artifacts_and_discover_paths():
    # #629 defects 2+3: architect could create artifacts but not read
    # them, and had no discovery tool — handoff artifact was unreadable
    # by its designated consumer.
    tools = _spec("delivery-architect")["tools"]
    assert "artifact_get_source" in tools
    assert "file_list" in tools


def test_consuming_seats_have_artifact_get_source():
    for seat in ("delivery-env-mock", "delivery-devrel"):
        assert "artifact_get_source" in _spec(seat)["tools"], seat


def test_ba_has_discovery_tool():
    assert "file_list" in _spec("delivery-ba")["tools"]


def test_seat_yaml_valid():
    # load_seat_specs parses the shipped yaml; invalid yaml would raise
    # or drop seats here rather than at agent-creation time.
    specs = load_seat_specs()
    assert set(specs) >= {
        "delivery-ba",
        "delivery-architect",
        "delivery-env-mock",
        "delivery-doc",
        "delivery-devrel",
    }
