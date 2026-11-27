"""Artifact HTTP content endpoint (GET /v1/agents/{id}/artifacts/{artifact_id}).

Tests: owner-scoped fetch, 404 on missing, 404 on cross-agent access,
preview truncation, auth dependency present.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_runtime import api_server
from agent_runtime.artifact_tools import ArtifactManager


@pytest.fixture()
def client(monkeypatch, tmp_path):
    mgr = ArtifactManager(artifacts_dir=str(tmp_path / "artifacts"))
    monkeypatch.setattr(api_server, "_artifact_mgr", mgr)
    # local-trust mode: no keys configured (test env may carry FUSION_AGENT_STUDIO_API_KEY)
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    from agent_runtime.api_server import app

    return TestClient(app), mgr


class TestArtifactHttpContent:
    def test_get_artifact_full(self, client):
        c, mgr = client
        created = mgr.create_artifact(
            agent_id="ag1", name="prd.md", artifact_type="document", content="# PRD\nbody"
        )
        aid = created["artifact_id"]
        resp = c.get("/v1/agents/ag1/artifacts/" + aid)
        assert resp.status_code == 200
        body = resp.json()
        assert body["artifact"]["content"] == "# PRD\nbody"
        assert body["artifact"]["owner_agent_id"] == "ag1"

    def test_get_missing_404(self, client):
        c, _ = client
        resp = c.get("/v1/agents/ag1/artifacts/nope123")
        assert resp.status_code == 404

    def test_cross_agent_404(self, client):
        c, mgr = client
        created = mgr.create_artifact(
            agent_id="ag1", name="secret.md", artifact_type="document", content="x"
        )
        resp = c.get("/v1/agents/ag2/artifacts/" + created["artifact_id"])
        assert resp.status_code == 404

    def test_preview_truncates(self, client):
        c, mgr = client
        created = mgr.create_artifact(
            agent_id="ag1", name="big.md", artifact_type="document", content="A" * 2000
        )
        resp = c.get("/v1/agents/ag1/artifacts/" + created["artifact_id"] + "?preview=true")
        assert resp.status_code == 200
        assert len(resp.json()["artifact"]["content"]) == 500

    def test_endpoint_has_auth_dependency(self):
        from agent_runtime.api_server import app

        route = next(
            r
            for r in app.routes
            if getattr(r, "path", "") == "/v1/agents/{agent_id}/artifacts/{artifact_id}"
        )
        deps = [d.dependency.__name__ for d in route.dependencies]
        assert "_require_auth_if_configured" in deps
