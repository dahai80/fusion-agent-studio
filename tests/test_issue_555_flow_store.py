"""#555: flow store API + version snapshots (tenant-scoped).

ACs:
1. Save/load round-trip preserves graph + config.
2. Version snapshots list + restore green.
3. Tenant isolation: tenant A cannot read tenant B flows.
4. Corrupt/stale flow fails visibly with actionable error.
5. flow.* RPC handlers wired into the daemon; /v1/flows REST surface.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_runtime.flow_store import FlowStore, FlowStoreError

GRAPH_V1 = {
    "schemaVersion": 1,
    "nodes": [
        {"id": "start", "type": "start", "position": {"x": 0, "y": 0}},
        {
            "id": "llm1",
            "type": "llm",
            "position": {"x": 200, "y": 0},
            "config": {"model": "qwen", "system_prompt": "be brief"},
        },
    ],
    "edges": [{"id": "e1", "source": "start", "target": "llm1"}],
}

GRAPH_V2 = {
    "schemaVersion": 1,
    "nodes": [
        {"id": "start", "type": "start", "position": {"x": 0, "y": 0}},
        {
            "id": "llm1",
            "type": "llm",
            "position": {"x": 200, "y": 0},
            "config": {"model": "qwen2", "system_prompt": "be brief"},
        },
        {"id": "end", "type": "end", "position": {"x": 400, "y": 0}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "llm1"},
        {"id": "e2", "source": "llm1", "target": "end"},
    ],
}


@pytest.fixture
def store(tmp_path):
    s = FlowStore(db_path=tmp_path / "flows.db")
    yield s
    s.close()


def test_roundtrip_preserves_graph_and_config(store):
    out = store.save_flow(GRAPH_V1, name="demo", description="d1")
    doc = store.load_flow(out["flow_id"])
    assert doc["graph"] == GRAPH_V1
    assert doc["name"] == "demo"
    assert doc["description"] == "d1"
    assert doc["version"] == 1


def test_version_snapshots_and_restore(store):
    out = store.save_flow(GRAPH_V1, name="f")
    fid = out["flow_id"]
    store.save_flow(GRAPH_V2, flow_id=fid)
    doc = store.load_flow(fid)
    assert doc["version"] == 2
    assert doc["graph"] == GRAPH_V2
    versions = store.list_versions(fid)
    assert [v["version"] for v in versions] == [2, 1]
    # restore v1 — becomes current v3, snapshot recorded
    res = store.restore_version(fid, 1)
    assert res["restored"] is True and res["version"] == 3
    doc = store.load_flow(fid)
    assert doc["graph"] == GRAPH_V1
    assert [v["version"] for v in store.list_versions(fid)] == [3, 2, 1]
    # restore is idempotent when graph identical
    res = store.restore_version(fid, 1)
    assert res["restored"] is False


def test_tenant_isolation(store):
    a = store.save_flow(GRAPH_V1, tenant_id="tenant-a", name="A-flow")
    store.save_flow(GRAPH_V1, tenant_id="tenant-b", name="B-flow")
    assert [f["name"] for f in store.list_flows(tenant_id="tenant-a")] == ["A-flow"]
    with pytest.raises(FlowStoreError, match="tenant-a"):
        store.load_flow(a["flow_id"] + "-x", tenant_id="tenant-a")
    # A cannot load B's flow by id
    with pytest.raises(FlowStoreError, match="tenant-a"):
        bflows = store.list_flows(tenant_id="tenant-b")
        store.load_flow(bflows[0]["flow_id"], tenant_id="tenant-a")
    # A cannot delete B's flow
    assert store.delete_flow(a["flow_id"], tenant_id="tenant-b") is False
    assert store.load_flow(a["flow_id"], tenant_id="tenant-a")["graph"] == GRAPH_V1


def test_corrupt_flow_fails_visibly(store, tmp_path):
    out = store.save_flow(GRAPH_V1)
    fid = out["flow_id"]
    store.close()
    conn = sqlite3.connect(tmp_path / "flows.db")
    conn.execute("UPDATE flows SET graph_json='{not json' WHERE flow_id=?", (fid,))
    conn.commit()
    conn.close()
    s2 = FlowStore(db_path=tmp_path / "flows.db")
    with pytest.raises(FlowStoreError, match="corrupt.*restore"):
        s2.load_flow(fid)
    s2.close()


def test_invalid_graph_rejected(store):
    with pytest.raises(FlowStoreError):
        store.save_flow({"nodes": []})
    with pytest.raises(FlowStoreError):
        store.save_flow({"nodes": [{"type": "start"}]})
    with pytest.raises(FlowStoreError):
        store.save_flow([1, 2])


def test_unknown_version_restore_errors(store):
    out = store.save_flow(GRAPH_V1)
    with pytest.raises(FlowStoreError, match="version 9"):
        store.restore_version(out["flow_id"], 9)


def test_dispatcher_wired_into_daemon(tmp_path):
    import asyncio
    import tempfile

    from agent_runtime.daemon_server import DaemonServer

    async def run():
        short = tempfile.mkdtemp(prefix="fas555-")
        d = DaemonServer(
            socket_path=str(Path(short) / "s.sock"),
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=str(tmp_path / "store.db"),
        )
        try:
            await d.start()
            d._flow_store = FlowStore(db_path=tmp_path / "flows.db")

            async def rpc(method, params):
                resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                if "error" in resp:
                    return {"error": resp["error"]}
                return resp.get("result", {})

            created = await rpc("flow.create", {"name": "w", "graph": GRAPH_V1})
            assert "error" not in created
            fid = created["flow_id"]
            saved = await rpc("flow.save", {"flow_id": fid, "graph": GRAPH_V2})
            assert saved["version"] == 2
            loaded = await rpc("flow.load", {"flow_id": fid})
            assert loaded["graph"] == GRAPH_V2
            versions = await rpc("flow.versions", {"flow_id": fid})
            assert versions["count"] == 2
            restored = await rpc("flow.restore", {"flow_id": fid, "version": 1})
            assert restored["restored"] is True
            lst = await rpc("flow.list", {})
            assert lst["count"] == 1
            deleted = await rpc("flow.delete", {"flow_id": fid})
            assert deleted["deleted"] is True
            missing = await rpc("flow.load", {"flow_id": fid})
            assert "error" in missing
        finally:
            await d.stop()

    asyncio.run(run())


def test_openapi_flow_routes_present(tmp_path, monkeypatch):
    from agent_runtime import api_server

    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with TestClient(api_server.app) as c:
        paths = c.get("/openapi.json").json()["paths"]
    for route in (
        "/v1/flows",
        "/v1/flows/{flow_id}",
        "/v1/flows/{flow_id}/versions",
        "/v1/flows/{flow_id}/restore",
    ):
        assert route in paths, route


def test_rest_surface(tmp_path, monkeypatch):
    from agent_runtime import api_server

    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with TestClient(api_server.app) as c:
        r = c.post("/v1/flows", json={"name": "f1", "graph": GRAPH_V1})
        assert r.status_code == 200, r.text
        fid = r.json()["flow_id"]
        r = c.put(f"/v1/flows/{fid}", json={"graph": GRAPH_V2})
        assert r.status_code == 200 and r.json()["version"] == 2
        r = c.get(f"/v1/flows/{fid}")
        assert r.status_code == 200 and r.json()["graph"] == GRAPH_V2
        r = c.get(f"/v1/flows/{fid}/versions")
        assert r.json()["count"] == 2
        r = c.post(f"/v1/flows/{fid}/restore", json={"version": 1})
        assert r.status_code == 200 and r.json()["restored"] is True
        r = c.get("/v1/flows")
        assert r.status_code == 200
        r = c.post("/v1/flows", json={"graph": {"nodes": []}})
        assert r.status_code == 422
        r = c.get("/v1/flows/missing-flow")
        assert r.status_code == 404
        r = c.delete(f"/v1/flows/{fid}")
        assert r.status_code == 200
        r = c.get(f"/v1/flows/{fid}")
        assert r.status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
