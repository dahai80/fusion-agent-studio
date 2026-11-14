"""#681: fusion-memory export_skills consumer — sync into SkillsStore.

Covers: happy-path install (frontmatter preserved, Provenance footer kept in
body), bearer token pass-through, limit/namespace payload, invalid-name and
empty-markdown skip reporting, project target dir, HTTP/unreachable error
mapping, and the skills.sync_memory RPC handler.
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent_runtime.memory_skill_sync import sync_memory_skills
from agent_runtime.skills_store import SkillsStore


def _doc(name: str, description: str = "exported skill") -> dict:
    markdown = (
        f"---\nname: {name}\ndescription: {description}\nversion: 1\n"
        "source: fusion-memory\nlicense: Apache-2.0\n---\n\n"
        f"# {name}\n\nDo the thing.\n\n## Provenance\n\n"
        "episode: ep-123\nconfidence: 0.87\n"
    )
    return {"id": f"mem-{name}", "name": name, "confidence": 0.87, "markdown": markdown}


def _mock_handler(entries: list, seen: dict):
    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization", "")
        seen["payload"] = json.loads(request.content.decode() or "{}")
        return httpx.Response(200, json=entries)

    return httpx.MockTransport(handler)


@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_SKILLS_DIR", str(tmp_path / "skills"))
    return tmp_path


@pytest.mark.asyncio
async def test_sync_installs_skills(skills_home):
    seen: dict = {}
    entries = [_doc("deploy-flow"), _doc("troubleshoot-disk")]
    res = await sync_memory_skills(
        limit=2,
        token="tok-1",
        base_url="http://127.0.0.1:11435",
        transport=_mock_handler(entries, seen),
    )
    assert res["status"] == "ok"
    assert res["count"] == 2
    assert seen["path"] == "/v1/memory/export_skills"
    assert seen["auth"] == "Bearer tok-1"
    assert seen["payload"] == {"limit": 2}
    store = SkillsStore(skills_dir=str(skills_home / "skills"))
    loaded = store.load_skill("deploy-flow")
    assert loaded is not None
    assert loaded["meta"]["description"] == "exported skill"
    assert "## Provenance" in loaded["body"]
    assert "confidence: 0.87" in loaded["body"]


@pytest.mark.asyncio
async def test_sync_skips_invalid_entries(skills_home):
    seen: dict = {}
    entries = [
        _doc("good-one"),
        {"id": "x", "name": "../evil", "markdown": "---\nname: x\n---\nbody"},
        {"id": "y", "name": "empty-body", "markdown": "   "},
        "not-a-dict",
    ]
    res = await sync_memory_skills(transport=_mock_handler(entries, seen))
    assert res["status"] == "ok"
    assert res["count"] == 1
    assert len(res["skipped"]) == 3
    reasons = " ".join(s["reason"] for s in res["skipped"])
    assert "invalid skill name" in reasons
    assert "empty markdown" in reasons


@pytest.mark.asyncio
async def test_sync_project_target(skills_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen: dict = {}
    res = await sync_memory_skills(
        namespace="project",
        namespace_key="agent-studio",
        target="project",
        project_root=str(tmp_path),
        transport=_mock_handler([_doc("proj-skill")], seen),
    )
    assert seen["payload"] == {"limit": 20, "namespace": "project", "namespace_key": "agent-studio"}
    skill_md = tmp_path / ".fusion-agent-studio" / "skills" / "proj-skill" / "SKILL.md"
    assert skill_md.exists()
    assert res["skills_dir"].endswith(".fusion-agent-studio/skills")


@pytest.mark.asyncio
async def test_sync_http_error_maps_to_status(skills_home):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    res = await sync_memory_skills(transport=httpx.MockTransport(handler))
    assert res["status"] == "error"
    assert "401" in res["message"]


@pytest.mark.asyncio
async def test_sync_unreachable_maps_to_status(skills_home):
    res = await sync_memory_skills(base_url="http://127.0.0.1:1", transport=None)
    assert res["status"] == "error"
    assert "unreachable" in res["message"]


@pytest.mark.asyncio
async def test_sync_memory_rpc_handler(skills_home):
    # RPC handler passes params straight through to sync_memory_skills.
    from agent_runtime.dispatchers.skills import SkillsDispatcher

    class _Daemon:
        def _get_protocol_plane(self):
            return None

    dispatcher = SkillsDispatcher(_Daemon())
    captured: dict = {}

    async def stub(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "count": 0, "synced": [], "skipped": [], "skills_dir": ""}

    import agent_runtime.memory_skill_sync as mss

    real = mss.sync_memory_skills
    mss.sync_memory_skills = stub
    try:
        res = await dispatcher._handle_sync_memory(
            {"limit": 5, "target": "project", "project_root": "/tmp/x"}
        )
    finally:
        mss.sync_memory_skills = real
    assert captured["limit"] == 5
    assert captured["target"] == "project"
    assert captured["project_root"] == "/tmp/x"
    assert res["status"] == "ok"
