"""#487: subagents auto-register into DurableRoster; subagent_spawn default-on."""
import pytest

from agent_runtime.context import AgentContext
from agent_runtime.durable_roster import DurableRoster
from agent_runtime.runtime import AgentRuntime
from agent_runtime.subagent import SubAgentRunner, SubAgentSpec
from server.fusion_mlx_client import LLMResponse, StreamChunk
from tools.registry import ToolRegistry


class _StubClient:
    def __init__(self):
        self._i = 0

    async def chat(self, model, messages, tools=None, **kwargs):
        self._i += 1
        if self._i == 1:
            return LLMResponse(content="working")
        return LLMResponse(content="final report")

    async def chat_stream(self, messages, model="", **kwargs):
        yield StreamChunk(delta_content="final report", finish_reason="stop")

    async def embeddings(self, model, input, **kwargs):
        return [[0.1] * 8]


def _runtime():
    from agent_runtime.llm_gateway import LLMGateway, ModelConfig

    gw = LLMGateway()
    gw.set_default_client(_StubClient())
    gw.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    return AgentRuntime(llm_gateway=gw, tool_registry=ToolRegistry())


@pytest.mark.asyncio
async def test_spawn_registers_into_durable_roster(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_TEAM_DB", str(tmp_path / "team.db"))
    rt = _runtime()
    runner = SubAgentRunner(rt)
    report = await runner.run(
        SubAgentSpec(name="scout", task="do thing", team="alpha", tools=[])
    )
    assert report.ok is True
    roster = DurableRoster()
    entry = roster.get(report.agent_id)
    assert entry is not None
    assert entry["name"] == "scout"
    assert entry["team"] == "alpha"
    assert entry["status"] == "done"
    assert [e["name"] for e in roster.list(team="alpha")] == ["scout"]


@pytest.mark.asyncio
async def test_spawn_roster_status_failed_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_TEAM_DB", str(tmp_path / "team.db"))

    class BoomRunner(SubAgentRunner):
        async def _run_inner(self, spec, parent_ctx, model, seed_messages=None):
            raise RuntimeError("boom")

    rt = _runtime()
    runner = BoomRunner(rt)
    report = await runner.run(SubAgentSpec(name="bad", task="t", team="t1"))
    assert report.ok is False
    assert DurableRoster().get(report.agent_id)["status"] == "failed"


@pytest.mark.asyncio
async def test_roster_failure_never_breaks_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_TEAM_DB", str(tmp_path / "nope" / "sub" / "team.db"))
    monkeypatch.setattr(
        "agent_runtime.durable_roster._connect",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    rt = _runtime()
    report = await SubAgentRunner(rt).run(SubAgentSpec(name="solo", task="t"))
    assert report.ok is True


def test_subagent_tool_default_on(monkeypatch):
    monkeypatch.delenv("FUSION_SUBAGENT_TOOLS", raising=False)
    rt = _runtime()
    assert rt._subagent_tools_enabled is True
    rt._register_subagent_tools()
    assert rt.tools.has("subagent_spawn") is True
    assert rt.subagent_runner is not None
    assert rt.subagent_registry is not None
    # idempotent: second call does not double-register
    rt._register_subagent_tools()
    assert rt.tools.count == 1


def test_subagent_tool_opt_out(monkeypatch):
    monkeypatch.setenv("FUSION_SUBAGENT_TOOLS", "0")
    rt = _runtime()
    assert rt._subagent_tools_enabled is False
    rt._register_subagent_tools()
    assert rt.tools.has("subagent_spawn") is False


@pytest.mark.parametrize("val", ["0", "false", "no"])
def test_subagent_tool_opt_out_values(val, monkeypatch):
    monkeypatch.setenv("FUSION_SUBAGENT_TOOLS", val)
    rt = _runtime()
    assert rt._subagent_tools_enabled is False


def test_bare_runtime_registry_stays_isolated(monkeypatch):
    # #341 isolation: bare AgentRuntime never registers subagent_spawn;
    # only the top-level LLM surfaces (daemon/api) opt in.
    monkeypatch.delenv("FUSION_SUBAGENT_TOOLS", raising=False)
    rt = _runtime()
    assert rt.tools.count == 0
    assert rt.tools.has("subagent_spawn") is False


@pytest.mark.asyncio
async def test_parent_ctx_session_id_unaffected(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_TEAM_DB", str(tmp_path / "team.db"))
    rt = _runtime()
    runner = SubAgentRunner(rt)
    parent = AgentContext(session_id="p1")
    report = await runner.run(
        SubAgentSpec(name="w", task="t", team="beta"), parent_ctx=parent
    )
    assert report.ok is True
    assert DurableRoster().get(report.agent_id)["team"] == "beta"
