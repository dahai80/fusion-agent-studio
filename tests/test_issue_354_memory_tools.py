import asyncio
import json

import pytest

from agent_runtime.dispatchers.memory import MemoryDispatcher
from agent_runtime.memory_engine import MemoryEngine
from agent_runtime.memory_pack import export_pack, import_pack
from agent_runtime.runtime import AgentRuntime
from tools.memory_tools import create_memory_tools
from tools.registry import ToolRegistry


@pytest.fixture()
def engine(tmp_path):
    return MemoryEngine(db_path=str(tmp_path / "mem.db"))


@pytest.fixture()
def tools(engine):
    return {t.name: t for t in create_memory_tools(lambda: engine)}


def _run(tool, args):
    return asyncio.run(tool.execute(**args))


def test_memory_write_and_read(tools):
    out = json.loads(_run(tools["memory_write"], {"content": "user prefers dark mode", "memory_type": "user"}))
    assert out["status"] == "stored"
    assert out["id"]
    got = json.loads(_run(tools["memory_read"], {"entry_id": out["id"]}))
    assert "dark mode" in got["content"]
    assert got["memory_type"] == "user"


def test_memory_read_list_recent(tools):
    _run(tools["memory_write"], {"content": "note one", "scope": "sc"})
    _run(tools["memory_write"], {"content": "note two", "scope": "sc"})
    rows = json.loads(_run(tools["memory_read"], {"scope": "sc"}))
    assert len(rows) == 2
    missing = _run(tools["memory_read"], {"entry_id": "nope"})
    assert missing.startswith("Error:")


def test_memory_search(tools):
    _run(tools["memory_write"], {"content": "deploy checklist: tag then release"})
    _run(tools["memory_write"], {"content": "lunch order: noodles"})
    rows = json.loads(_run(tools["memory_search"], {"query": "deploy"}))
    assert any("deploy" in e["content"] for e in rows)
    empty = _run(tools["memory_search"], {})
    assert empty.startswith("Error:")


def test_memory_forget(tools):
    entry_id = json.loads(_run(tools["memory_write"], {"content": "temp note"}))["id"]
    out = json.loads(_run(tools["memory_forget"], {"entry_id": entry_id}))
    assert out["status"] == "deleted"
    missing = _run(tools["memory_forget"], {"entry_id": "nope"})
    assert missing.startswith("Error:")


def test_readonly_flags(tools):
    assert tools["memory_read"].readonly is True
    assert tools["memory_search"].readonly is True
    assert tools["memory_write"].readonly is False
    assert tools["memory_forget"].readonly is False


def test_ensure_memory_tools_registers_once(engine):
    rt = AgentRuntime(memory_engine=engine, tool_registry=ToolRegistry())
    rt._ensure_memory_tools()
    names = [d["name"] for d in rt.tools.list_tools()]
    assert sum(1 for n in names if n.startswith("memory_")) == 4
    rt._memory_tools_registered = False
    rt._ensure_memory_tools()
    names2 = [d["name"] for d in rt.tools.list_tools()]
    assert sum(1 for n in names2 if n.startswith("memory_")) == 4


def test_ensure_memory_tools_noop_without_engine():
    rt = AgentRuntime(tool_registry=ToolRegistry())
    rt._ensure_memory_tools()
    assert rt._memory_tools_registered is False


def test_inject_budget_env(monkeypatch):
    monkeypatch.setenv("FUSION_MEMORY_INJECT_TOKENS", "10")
    rt = AgentRuntime()
    assert rt._memory_inject_tokens() == 40
    monkeypatch.setenv("FUSION_MEMORY_INJECT_TOKENS", "bad")
    assert AgentRuntime()._memory_inject_tokens() == 8000


def test_pack_roundtrip(engine, tmp_path):
    engine.store(content="alpha fact", scope="s1", memory_type="user")
    engine.store(content="beta fact", scope="s1", memory_type="feedback")
    out = export_pack(engine, str(tmp_path / "pack"), scope="s1")
    assert out["count"] == 2
    index = (tmp_path / "pack" / "MEMORY.md").read_text()
    assert "Memory Index" in index
    engine2 = MemoryEngine(db_path=str(tmp_path / "mem2.db"))
    res = import_pack(engine2, str(tmp_path / "pack"))
    assert res["count"] == 2
    assert res["skipped"] == 0
    got = engine2.list_recent(scope="imported", limit=10)
    bodies = {e.content.strip() for e in got}
    assert "alpha fact" in bodies


def test_pack_import_bad_dir(tmp_path):
    res = import_pack(None, str(tmp_path / "missing"))
    assert res["count"] == 0
    assert "not a directory" in res["error"]


def test_pack_rpc(engine, tmp_path):
    engine.store(content="rpc fact", scope="s9")

    class _Stub:
        _get_memory = staticmethod(lambda: engine)

    md = MemoryDispatcher(_Stub())
    out = asyncio.run(md._handle_memory_pack_export({"out_dir": str(tmp_path / "p2"), "scope": "s9"}))
    assert out["count"] == 1
    back = asyncio.run(md._handle_memory_pack_import({"in_dir": str(tmp_path / "p2")}))
    assert back["count"] == 1
    err = asyncio.run(md._handle_memory_pack_export({}))
    assert err["status"] == "error"
