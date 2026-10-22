"""Issue #436 (K4) — gateway-issued fm- module keys.

Mirror of fusion-cowork #168: registration stops self-supplying api_key
(static env override kept for backward compat), the gateway issues an fm-
key in the registration response which is persisted to
~/.fusion-agent-studio/web_module_key (0600), heartbeat authenticates with
the issued key, and inbound gateway→studio auth dual-accepts env static +
issued keys during the migration grace window.
"""
from __future__ import annotations

import asyncio
import json
import stat
from unittest.mock import MagicMock

import pytest

from agent_runtime import shell_registry
from agent_runtime.shell_registry import (
    _extract_issued_key,
    _mask_key,
    accepted_module_keys,
    load_module_key,
    save_module_key,
    static_module_key,
)


@pytest.fixture(autouse=True)
def _key_path(tmp_path, monkeypatch):
    fake = tmp_path / "web_module_key"
    monkeypatch.setattr(shell_registry, "MODULE_KEY_FILE", fake)
    monkeypatch.delenv("FUSION_AGENT_STUDIO_API_KEY", raising=False)
    yield fake


def _response(status_code=200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    payload = payload if payload is not None else {}
    resp.text = json.dumps(payload)
    resp.json = lambda: payload
    return resp


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0) if self._responses else _response(200, {})


async def _drive(client, manifest, rounds=0.08):
    task = asyncio.create_task(
        shell_registry._registry_loop_body(client, manifest, interval=0.01)
    )
    await asyncio.sleep(rounds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestModuleKeyPersistence:

    def test_save_and_load_roundtrip(self, _key_path):
        assert load_module_key() == ""
        assert save_module_key("fm-fusion-agent-studio-abc123") is True
        assert load_module_key() == "fm-fusion-agent-studio-abc123"

    def test_file_mode_0600(self, _key_path):
        save_module_key("fm-fusion-agent-studio-abc123")
        assert stat.S_IMODE(_key_path.stat().st_mode) == 0o600

    def test_empty_key_not_saved(self, _key_path):
        assert save_module_key("") is False
        assert save_module_key("   ") is False
        assert not _key_path.exists()

    def test_load_missing_file_returns_empty(self):
        assert load_module_key() == ""

    def test_mask_key_never_plaintext(self):
        masked = _mask_key("fm-fusion-agent-studio-0123456789")
        assert "0123456789" not in masked
        assert masked.startswith("fm-f")
        assert _mask_key("short") == "****"


class TestExtractIssuedKey:

    def test_top_level_fields(self):
        assert _extract_issued_key({"api_key": "fm-1"}) == "fm-1"
        assert _extract_issued_key({"issued_key": "fm-2"}) == "fm-2"
        assert _extract_issued_key({"key": "fm-3"}) == "fm-3"
        assert _extract_issued_key({"module_key": "fm-4"}) == "fm-4"

    def test_nested_data(self):
        assert _extract_issued_key({"data": {"api_key": "fm-nested"}}) == "fm-nested"

    def test_no_key(self):
        assert _extract_issued_key({"status": "ok"}) == ""
        assert _extract_issued_key(None) == ""
        assert _extract_issued_key("text") == ""

    def test_priority_api_key_first(self):
        assert _extract_issued_key({"issued_key": "fm-b", "api_key": "fm-a"}) == "fm-a"


class TestRegistrationPayload:

    def test_static_override_kept_when_env_set(self, monkeypatch):
        monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "fk-mod-static")
        payload = shell_registry.build_registration_payload({"id": "m", "upstream": "http://h:1"})
        assert payload["api_key"] == "fk-mod-static"

    def test_omits_api_key_when_env_unset_even_with_file(self, monkeypatch, _key_path):
        save_module_key("fm-issued-from-earlier-run")
        payload = shell_registry.build_registration_payload({"id": "m", "upstream": "http://h:1"})
        assert "api_key" not in payload

    def test_source_manifest_not_mutated(self, monkeypatch):
        monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "k2")
        manifest = {"id": "m", "upstream": "http://h:2"}
        payload = shell_registry.build_registration_payload(manifest)
        assert payload["api_key"] == "k2"
        assert "api_key" not in manifest


class TestRegistryLoop:

    async def test_registration_persists_issued_key(self, _key_path):
        client = _FakeClient([_response(200, {"api_key": "fm-issued-1"})])
        await _drive(client, {"id": "mod-x", "upstream": "http://h:1"})
        assert load_module_key() == "fm-issued-1"
        assert stat.S_IMODE(_key_path.stat().st_mode) == 0o600

    async def test_no_key_in_response_nothing_persisted(self, _key_path):
        client = _FakeClient([_response(200, {"status": "ok"})])
        await _drive(client, {"id": "mod-x", "upstream": "http://h:1"})
        assert load_module_key() == ""
        assert not _key_path.exists()

    async def test_issued_key_not_rewritten_when_same(self, _key_path):
        save_module_key("fm-issued-1")
        client = _FakeClient([_response(200, {"api_key": "fm-issued-1"})])
        await _drive(client, {"id": "mod-x", "upstream": "http://h:1"})
        assert load_module_key() == "fm-issued-1"

    async def test_heartbeat_uses_issued_key(self, _key_path):
        save_module_key("fm-issued-hb")
        client = _FakeClient([
            _response(200, {"api_key": "fm-issued-hb"}),
            _response(200, {}),
        ])
        await _drive(client, {"id": "mod-x", "upstream": "http://h:1"})
        beats = [c for c in client.calls if c["url"].endswith("/heartbeat")]
        assert len(beats) >= 1
        assert beats[0]["headers"]["Authorization"] == "Bearer fm-issued-hb"

    async def test_heartbeat_falls_back_to_admin_key(self, _key_path, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_ADMIN_KEY", "admin-k")
        client = _FakeClient([
            _response(200, {}),
            _response(200, {}),
        ])
        await _drive(client, {"id": "mod-x", "upstream": "http://h:1"})
        beats = [c for c in client.calls if c["url"].endswith("/heartbeat")]
        assert beats[0]["headers"]["Authorization"] == "Bearer admin-k"

    async def test_heartbeat_401_with_issued_key_reregisters(self, _key_path, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_ADMIN_KEY", "admin")
        client = _FakeClient([
            _response(200, {"api_key": "fm-stale"}),
            _response(401, {}),
            _response(200, {"api_key": "fm-fresh"}),
            _response(200, {}),
        ])
        await _drive(client, {"id": "mod-x", "upstream": "http://h:1"}, rounds=0.12)
        assert load_module_key() == "fm-fresh"
        registers = [c for c in client.calls if c["url"].endswith("/manifests")]
        assert len(registers) == 2
        assert registers[1]["headers"]["Authorization"] == "Bearer admin"

    async def test_never_logs_plaintext(self, _key_path, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="agent_runtime.shell_registry")
        save_module_key("fm-very-secret-plaintext-value")
        client = _FakeClient([_response(200, {"api_key": "fm-very-secret-plaintext-value"})])
        await _drive(client, {"id": "mod-x", "upstream": "http://h:1"})
        assert "fm-very-secret-plaintext-value" not in caplog.text


class TestDualAccept:

    def test_env_key_accepted_as_static_env(self, monkeypatch):
        monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "fk-mod-env")
        keys = accepted_module_keys()
        assert keys == ["fk-mod-env"]
        assert static_module_key() == "fk-mod-env"

    def test_issued_file_key_accepted_as_module_issued(self, monkeypatch, _key_path):
        save_module_key("fm-issued-only")
        assert accepted_module_keys() == ["fm-issued-only"]
        assert static_module_key() == "fm-issued-only"

    def test_both_keys_during_grace_window(self, monkeypatch, _key_path):
        save_module_key("fm-issued-new")
        monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "fk-mod-old-static")
        keys = accepted_module_keys()
        assert keys == ["fk-mod-old-static", "fm-issued-new"]

    def test_check_static_key_dual_accepts(self, monkeypatch, _key_path):
        from agent_runtime.api_server import _check_static_key

        save_module_key("fm-issued-new")
        monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "fk-mod-old-static")
        assert _check_static_key("fk-mod-old-static")["key_id"] == "static-env"
        assert _check_static_key("fm-issued-new")["key_id"] == "module-issued"
        assert _check_static_key("fm-unknown") is None
        assert _check_static_key("") is None
