"""Issue #399 — shell manifest upstream/api_key injection at registration.

web/manifest.json lacks upstream/api_key routing fields, so the gateway
dynamic proxy (#232 contract, UpstreamFor) cannot resolve /shell/<id>/
to the daemon. Fix: manifest.json carries `upstream`; api_key is injected
at registration time from static_module_key() (never in the repo file).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from agent_runtime import shell_registry

ROOT = Path(__file__).resolve().parents[1]


class TestResolveUpstream:

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("FUSION_SHELL_UPSTREAM", "http://127.0.0.1:7777/")
        assert shell_registry.resolve_upstream({"upstream": "http://x:1"}) == "http://127.0.0.1:7777"

    def test_manifest_value_fallback(self, monkeypatch):
        monkeypatch.delenv("FUSION_SHELL_UPSTREAM", raising=False)
        assert shell_registry.resolve_upstream({"upstream": "http://10.0.0.5:11442"}) == "http://10.0.0.5:11442"

    def test_default_when_missing(self, monkeypatch):
        monkeypatch.delenv("FUSION_SHELL_UPSTREAM", raising=False)
        assert shell_registry.resolve_upstream({}) == shell_registry.DEFAULT_UPSTREAM

    def test_relative_url_rejected(self, monkeypatch):
        monkeypatch.delenv("FUSION_SHELL_UPSTREAM", raising=False)
        with pytest.raises(ValueError, match="absolute http"):
            shell_registry.resolve_upstream({"upstream": "/shell/foo/"})


class TestRegistrationPayload:

    def test_injects_upstream_and_api_key(self, monkeypatch):
        monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "fk-mod-secret")
        payload = shell_registry.build_registration_payload({"id": "m", "upstream": "http://h:1"})
        assert payload["upstream"] == "http://h:1"
        assert payload["api_key"] == "fk-mod-secret"

    def test_no_api_key_when_module_key_empty(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FUSION_AGENT_STUDIO_API_KEY", raising=False)
        monkeypatch.setattr(shell_registry, "MODULE_KEY_FILE", tmp_path / "absent")
        payload = shell_registry.build_registration_payload({"id": "m"})
        assert "api_key" not in payload

    def test_source_manifest_not_mutated(self, monkeypatch):
        monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "k2")
        manifest = {"id": "m", "upstream": "http://h:2"}
        payload = shell_registry.build_registration_payload(manifest)
        assert payload["api_key"] == "k2"
        assert "api_key" not in manifest

    def test_repo_manifest_has_no_api_key(self):
        data = json.loads((ROOT / "web" / "manifest.json").read_text())
        assert "api_key" not in data
        assert data.get("upstream") == "http://127.0.0.1:11442"


class TestLoopWiring:

    def _run_one_tick(self, monkeypatch, tmp_path, manifest, env_upstream=None):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest))
        monkeypatch.setenv("FUSION_SHELL_MANIFEST", str(path))
        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:19999")
        monkeypatch.setenv("FUSION_GATEWAY_ADMIN_KEY", "admin")
        monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "fk-mod-secret")
        if env_upstream is not None:
            monkeypatch.setenv("FUSION_SHELL_UPSTREAM", env_upstream)
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        real_client = shell_registry.httpx.AsyncClient

        def factory(**kwargs):
            kwargs["transport"] = transport
            return real_client(**kwargs)

        monkeypatch.setattr(shell_registry.httpx, "AsyncClient", factory)

        async def runner():
            task = asyncio.create_task(shell_registry.shell_registry_loop())
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(runner())
        return captured

    def test_loop_posts_upstream_and_api_key(self, monkeypatch, tmp_path):
        captured = self._run_one_tick(monkeypatch, tmp_path, {"id": "mod-x", "title": "T"})
        assert len(captured) == 1
        body = json.loads(captured[0].content)
        assert body["upstream"] == shell_registry.DEFAULT_UPSTREAM
        assert body["api_key"] == "fk-mod-secret"

    def test_loop_env_upstream_override(self, monkeypatch, tmp_path):
        captured = self._run_one_tick(
            monkeypatch, tmp_path, {"id": "mod-x"},
            env_upstream="http://127.0.0.1:7788",
        )
        body = json.loads(captured[0].content)
        assert body["upstream"] == "http://127.0.0.1:7788"

    def test_loop_aborts_on_bad_upstream(self, monkeypatch, tmp_path):
        captured = self._run_one_tick(
            monkeypatch, tmp_path, {"id": "mod-x", "upstream": "not-a-url"},
        )
        assert captured == []
