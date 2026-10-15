"""#327: union-web module — static env key auth, Bearer paths, web dist mount.

Contract:
- FUSION_AGENT_STUDIO_API_KEY alone counts as "auth configured": protected
  endpoints 401 without a key, accept the static key via x-api-key or
  Authorization: Bearer (the gateway /shell proxy path), reject wrong keys.
- SSE endpoint accepts Bearer (EventSource cannot set headers; the gateway
  injects the module key as Bearer).
- GET / serves the built SPA from web/dist (mounted after API routes so
  /v1/* wins); skipped when dist has not been built.
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC_KEY = "fk-static-test-327"


class _StubDaemon:
    # Minimal daemon: task.submit returns a pending task; graph.execute absent.
    def _get_handler(self, method):
        if method == "task.submit":
            return self._handle_task_submit
        if method == "task.get":
            return self._handle_task_get
        return None

    async def _handle_task_submit(self, params):
        return {"task": {"task_id": "task-327", "status": "pending", "title": params.get("title", "")}}

    async def _handle_task_get(self, params):
        return {"task": {"task_id": params.get("task_id", ""), "status": "pending"}}


@pytest.fixture
def static_key_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", STATIC_KEY)
    # Isolate the key store so _auth_configured() sees ONLY the static key
    # (no real ~/.fusion-agent-studio fk- keys on the dev machine).
    fake_home = tmp_path / "home"
    (fake_home / ".fusion-agent-studio").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return STATIC_KEY


@pytest.fixture
def stub_daemon():
    from agent_runtime import api_server

    api_server.set_daemon(_StubDaemon())
    yield
    api_server.set_daemon(None)


class TestStaticKeyHelpers:
    def test_check_static_key_match(self, static_key_env):
        from agent_runtime.api_server import _check_static_key

        result = _check_static_key(STATIC_KEY)
        assert result is not None
        assert result["valid"] is True
        assert result["key_id"] == "static-env"
        assert "admin" in result["permissions"]

    def test_check_static_key_mismatch(self, static_key_env):
        from agent_runtime.api_server import _check_static_key

        assert _check_static_key("fk-wrong") is None
        assert _check_static_key("") is None

    def test_auth_configured_with_static_key_only(self, static_key_env):
        from agent_runtime.api_server import _auth_configured

        assert _auth_configured() is True

    def test_extract_caller_key_bearer_and_header(self):
        class _FakeHeaders(dict):
            def get(self, k, default=""):
                return super().get(k.lower(), default)

        class _FakeRequest:
            headers = _FakeHeaders()

        from agent_runtime.api_server import _extract_caller_key

        req = _FakeRequest()
        req.headers = _FakeHeaders({"authorization": f"Bearer {STATIC_KEY}"})
        assert _extract_caller_key(req) == STATIC_KEY
        req.headers = _FakeHeaders({"x-api-key": STATIC_KEY})
        assert _extract_caller_key(req) == STATIC_KEY
        req.headers = _FakeHeaders({"authorization": "Basic abc"})
        assert _extract_caller_key(req) == ""


class TestHttpAuthGate:
    def _client(self):
        from fastapi.testclient import TestClient

        from agent_runtime.api_server import app

        return TestClient(app)

    def test_no_key_401(self, static_key_env):
        resp = self._client().post("/v1/runs", json={"goal": "hi"})
        assert resp.status_code == 401

    def test_bearer_static_key_passes_auth(self, static_key_env, stub_daemon):
        resp = self._client().post(
            "/v1/runs", json={"goal": "hi"}, headers={"Authorization": f"Bearer {STATIC_KEY}"}
        )
        assert resp.status_code == 200
        assert resp.json()["run_id"] == "task-327"
        assert resp.json()["status"] == "pending"

    def test_x_api_key_static_key_passes_auth(self, static_key_env, stub_daemon):
        resp = self._client().post("/v1/runs", json={"goal": "hi"}, headers={"x-api-key": STATIC_KEY})
        assert resp.status_code == 200
        assert resp.json()["run_id"] == "task-327"

    def test_wrong_bearer_401(self, static_key_env):
        resp = self._client().post(
            "/v1/runs", json={"goal": "hi"}, headers={"Authorization": "Bearer fk-wrong"}
        )
        assert resp.status_code == 401

    def test_sse_bearer_static_key_passes_auth(self, static_key_env):
        # SSE (GET, EventSource): auth passes via Bearer, then graph lookup 404.
        resp = self._client().get(
            "/v1/graphs/nonexistent-327/execute/stream",
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert resp.status_code == 404

    def test_sse_no_auth_401(self, static_key_env):
        resp = self._client().get("/v1/graphs/nonexistent-327/execute/stream")
        assert resp.status_code == 401

    def test_sse_query_api_key_static_passes_auth(self, static_key_env):
        resp = self._client().get(
            f"/v1/graphs/nonexistent-327/execute/stream?api_key={STATIC_KEY}"
        )
        assert resp.status_code == 404


class TestWebDistMount:
    def test_root_serves_spa(self):
        dist = Path(__file__).resolve().parent.parent / "web" / "dist"
        if not (dist / "index.html").exists():
            pytest.skip("web/dist not built — run `bun install && bun run build` in web/")
        from fastapi.testclient import TestClient

        from agent_runtime.api_server import app

        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert 'id="root"' in resp.text
        # API routes must still win over the catch-all mount.
        api_resp = client.get("/v1/health")
        assert api_resp.status_code == 200
        assert "mlx_reachable" in api_resp.json()

    def test_manifest_fields_valid(self):
        import json

        manifest_path = Path(__file__).resolve().parent.parent / "web" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["id"] == "fusion-agent-studio"
        assert manifest["entry"] == "/shell/fusion-agent-studio/"
        # backend points at the gateway shell proxy (11432), which forwards
        # to the daemon on 11442 and injects the module key as Bearer.
        assert manifest["backend"] == "http://127.0.0.1:11432/shell/fusion-agent-studio/"
        assert manifest["category"] in ("platform", "domain", "infra", "dev")
        assert manifest["icon"]
        assert manifest["title"] and manifest["title_en"]
        assert (
            manifest["health"]
            == "http://127.0.0.1:11432/shell/fusion-agent-studio/v1/health"
        )

    def test_vendored_packages_pinned(self):
        vendor = Path(__file__).resolve().parent.parent / "web" / "vendor" / "@fusion"
        for pkg in ("web-ui", "web-sdk"):
            pkg_json = vendor / pkg / "package.json"
            assert pkg_json.exists(), f"vendored {pkg} missing"
            assert (vendor / pkg / "src" / "index.ts").exists()
        # No workspace:* references — must be self-contained for CI.
        root_pkg = Path(__file__).resolve().parent.parent / "web" / "package.json"
        assert "workspace:*" not in root_pkg.read_text(encoding="utf-8")
