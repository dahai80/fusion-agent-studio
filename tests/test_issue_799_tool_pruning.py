# Issue #799: evidence-based tool pruning telemetry (TokenEconomizer borrow).
# Telemetry-only phase: per-session usage ledger + deterministic unused-streak
# proposals + tool.pruning_report RPC. Pruning apply is explicitly out of
# scope here; the ledger never mutates tool registration.
from __future__ import annotations

import pytest

from agent_runtime.context import AgentContext
from agent_runtime.daemon_server import DaemonServer
from agent_runtime.tool_pruner import (
    ToolUsageLedger,
    is_protected_tool,
    is_rare_class_tool,
)
from tests.test_issue_707_teams_dispatcher import _run, _runtime


def _ledger(tmp_path, **kwargs):
    return ToolUsageLedger(str(tmp_path / "ledger.json"), **kwargs)


def _seed(ledger, sessions):
    # sessions: list of (session_id, [tools]) oldest first
    for sid, tools in sessions:
        for t in tools:
            ledger.record(sid, t)


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


@pytest.fixture
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


class TestLedgerBasics:
    async def test_record_and_roundtrip(self, tmp_path):
        led = _ledger(tmp_path)
        _seed(led, [("s1", ["file_read", "db_query"]), ("s2", ["file_read"])])
        led2 = ToolUsageLedger(led.path)
        rows = led2.ordered_sessions()
        assert [r["session_id"] for r in rows] == ["s1", "s2"]
        assert rows[0]["tools"] == ["file_read", "db_query"]

    async def test_empty_inputs_ignored(self, tmp_path):
        led = _ledger(tmp_path)
        led.record("", "file_read")
        led.record("s1", "")
        assert led.ordered_sessions() == []

    async def test_max_sessions_pruned_oldest_first(self, tmp_path):
        led = _ledger(tmp_path, max_sessions=3)
        for i in range(5):
            led.record(f"s{i}", "file_read")
        rows = led.ordered_sessions()
        assert [r["session_id"] for r in rows] == ["s2", "s3", "s4"]

    async def test_corrupt_file_degrades_empty(self, tmp_path):
        p = tmp_path / "ledger.json"
        p.write_text("{not json", encoding="utf-8")
        led = ToolUsageLedger(str(p))
        led.record("s1", "db_query")
        assert led.ordered_sessions()[0]["tools"] == ["db_query"]


class TestClassification:
    async def test_protected_names_and_prefixes(self):
        assert is_protected_tool("file_read")
        assert is_protected_tool("shell_exec")
        assert is_protected_tool("git_status")
        assert is_protected_tool("register_tool")
        assert not is_protected_tool("db_query")
        assert not is_protected_tool("http_get")

    async def test_rare_class(self):
        assert is_rare_class_tool("mcp_fetch")
        assert is_rare_class_tool("computer_screenshot")
        assert not is_rare_class_tool("db_query")


class TestProposals:
    async def test_unused_tool_proposed_after_threshold(self, tmp_path):
        led = _ledger(tmp_path)
        _seed(led, [(f"s{i}", ["file_read"]) for i in range(5)])
        out = led.proposals(loaded_tools=["file_read", "db_query"], unused_sessions=5)
        names = [p["tool"] for p in out]
        assert "file_read" not in names
        assert "db_query" in names
        cand = next(p for p in out if p["tool"] == "db_query")
        assert cand["unused_streak"] == 5
        assert cand["last_used_ts"] == 0.0

    async def test_use_resets_streak(self, tmp_path):
        led = _ledger(tmp_path)
        _seed(
            led,
            [
                ("s1", ["db_query"]),
                ("s2", ["file_read"]),
                ("s3", ["db_query", "file_read"]),
                ("s4", ["file_read"]),
                ("s5", ["file_read"]),
                ("s6", ["file_read"]),
                ("s7", ["file_read"]),
                ("s8", ["file_read"]),
            ],
        )
        # db_query last used in s3: streak over s4..s8 = 5 consecutive
        # newest sessions... but s3 use means streak stops at 5, which
        # still crosses the threshold — use a shorter window to assert
        # the reset actually shrinks the streak below the threshold.
        out = led.proposals(loaded_tools=["db_query"], unused_sessions=6)
        assert out == []

    async def test_rare_class_needs_longer_streak(self, tmp_path):
        led = _ledger(tmp_path)
        _seed(led, [(f"s{i}", ["file_read"]) for i in range(8)])
        out = led.proposals(loaded_tools=["mcp_fetch"], unused_sessions=5)
        assert out == []
        _seed(led, [(f"t{i}", ["file_read"]) for i in range(10, 17)])
        out = led.proposals(loaded_tools=["mcp_fetch"], unused_sessions=5)
        assert [p["tool"] for p in out] == ["mcp_fetch"]
        assert out[0]["threshold"] == 15

    async def test_empty_ledger_no_proposals(self, tmp_path):
        led = _ledger(tmp_path)
        assert led.proposals(loaded_tools=["db_query"], unused_sessions=5) == []

    async def test_proposals_sorted_by_streak(self, tmp_path):
        led = _ledger(tmp_path)
        _seed(
            led,
            [
                ("s1", ["older_tool"]),
                ("s2", ["file_read"]),
                ("s3", ["file_read"]),
                ("s4", ["file_read"]),
                ("s5", ["file_read"]),
                ("s6", ["file_read"]),
            ],
        )
        out = led.proposals(loaded_tools=["older_tool", "never_tool"], unused_sessions=3)
        assert out[0]["tool"] == "never_tool"
        assert out[0]["unused_streak"] == 6


class TestReport:
    async def test_report_shape_and_saving(self, tmp_path):
        led = _ledger(tmp_path)
        _seed(led, [(f"s{i}", ["file_read"]) for i in range(5)])
        rep = led.report(
            loaded_tools=["file_read", "db_query"],
            schema_tokens={"db_query": 100},
            unused_sessions=5,
        )
        assert rep["mode"] == "telemetry_only"
        assert rep["sessions_observed"] == 5
        assert rep["protected_count"] == 1
        assert rep["est_tokens_saved_per_request"] == 100
        assert rep["candidates"][0]["tool"] == "db_query"
        assert "explicit apply step" in rep["note"]


class TestRpc:
    async def test_report_disabled_without_ledger(self, daemon, _home):
        rt, _client = _runtime()
        rt.tool_usage_ledger = None
        daemon._get_runtime = lambda: rt
        res = await _run(daemon, "tool.pruning_report", {})
        assert res == {"enabled": False, "report": None}

    async def test_report_lists_unused_loaded_tool(self, daemon, _home):
        rt, _client = _runtime()
        led = ToolUsageLedger(str(_home / "ledger.json"))
        for i in range(5):
            led.record(f"run-{i}", "file_read")
        rt.tool_usage_ledger = led
        daemon._get_runtime = lambda: rt
        res = await _run(daemon, "tool.pruning_report", {})
        assert res["enabled"] is True
        rep = res["report"]
        loaded = rep["loaded_tools"]
        assert loaded >= 3
        names = [c["tool"] for c in rep["candidates"]]
        # stub registry loads file_read/file_write/terminal_exec — all
        # protected work-surface tools, so nothing else is ever proposed
        # unless the registry grows; candidates must exclude protected tools.
        for n in ("file_read", "file_write", "terminal_exec"):
            assert n not in names

    async def test_report_counts_schema_tokens(self, daemon, _home):
        rt, _client = _runtime()
        led = ToolUsageLedger(str(_home / "ledger.json"))
        rt.tool_usage_ledger = led
        daemon._get_runtime = lambda: rt
        res = await _run(daemon, "tool.pruning_report", {})
        assert res["enabled"] is True
        assert isinstance(res["report"]["est_tokens_saved_per_request"], int)


class TestPipelineRecording:
    async def test_allowed_admission_recorded(self, _home):
        rt, _client = _runtime()
        led = ToolUsageLedger(str(_home / "ledger.json"))
        rt.tool_usage_ledger = led
        ctx = AgentContext(session_id="sess-799")
        outcome = await rt.tool_funnel.admit(
            ctx=ctx, tool_name="file_read", args={"path": "x"}, node_label="n"
        )
        assert outcome.allowed
        rows = led.ordered_sessions()
        assert rows, "ledger must have the session"
        assert "file_read" in rows[0]["tools"]
        assert rows[0]["session_id"] == ctx.session_id

    async def test_ledger_failure_never_breaks_admission(self, _home):
        class Boom:
            def record(self, *a, **k):
                raise RuntimeError("disk full")

        rt, _client = _runtime()
        rt.tool_usage_ledger = Boom()
        ctx = AgentContext(session_id="sess-799")
        outcome = await rt.tool_funnel.admit(
            ctx=ctx, tool_name="file_read", args={"path": "x"}, node_label="n"
        )
        assert outcome.allowed
