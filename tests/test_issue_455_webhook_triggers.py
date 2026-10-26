"""Tests for issue #455 — inbound webhook trigger surface.

Importers: pytest runner
API: /v1/webhooks CRUD + /v1/webhooks/{id}/invoke (HMAC, rate limit,
     idempotency, 64KB cap)
Data schemas: WebhookManager JSON store (transient in tests)
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient


def _hmac_sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from agent_runtime import api_server
    from agent_runtime.triggers import WebhookManager

    mgr = WebhookManager()  # transient: no store path
    monkeypatch.setattr(api_server, "_webhook_mgr", lambda: mgr)

    submits = []

    async def fake_rpc(method, params):
        if method == "graph.execute_async":
            return {"status": "ok"}
        assert method == "task.submit"
        submits.append(params)
        return {
            "task": {"task_id": "run-123", "status": "pending"},
            "deduped": False,
        }

    monkeypatch.setattr(api_server, "_rpc", fake_rpc)
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    return TestClient(api_server.app), mgr, submits


def _create_hook(client) -> dict:
    resp = client.post(
        "/v1/webhooks", json={"name": "demo", "graph_id": "graph-1"}
    )
    assert resp.status_code == 200
    return resp.json()["webhook"]


def test_crud_list_create_delete(client):
    tc, mgr, submits = client
    hook = _create_hook(tc)
    assert hook["id"].startswith("wh-")
    assert hook["secret"]  # returned once at creation
    assert mgr.count == 1

    listed = tc.get("/v1/webhooks").json()["webhooks"]
    assert len(listed) == 1
    assert "secret" not in listed[0]  # list never leaks secrets

    resp = tc.post(f"/v1/webhooks/{hook['id']}/enabled", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["webhook"]["enabled"] is False

    assert tc.delete(f"/v1/webhooks/{hook['id']}").status_code == 200
    assert mgr.count == 0
    assert tc.delete(f"/v1/webhooks/{hook['id']}").status_code == 404


def test_invoke_valid_signature_starts_run(client):
    tc, _, submits = client
    hook = _create_hook(tc)
    body = json.dumps({"text": "hi"}).encode()
    resp = tc.post(
        f"/v1/webhooks/{hook['id']}/invoke",
        content=body,
        headers={"content-type": "application/json", "x-signature": _hmac_sig(hook["secret"], body)},
    )
    assert resp.status_code == 200
    assert resp.json()["run_id"] == "run-123"


def test_invoke_wrong_signature_401_no_run(client):
    tc, _, submits = client
    hook = _create_hook(tc)
    resp = tc.post(
        f"/v1/webhooks/{hook['id']}/invoke",
        content=b'{"text":"hi"}',
        headers={"content-type": "application/json", "x-signature": "deadbeef"},
    )
    assert resp.status_code == 401
    assert submits == []  # #469: rejected invoke must not enqueue a run


def test_invoke_missing_signature_401(client):
    tc, _, submits = client
    hook = _create_hook(tc)
    resp = tc.post(
        f"/v1/webhooks/{hook['id']}/invoke",
        content=b'{"text":"hi"}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401
    assert submits == []  # #469: rejected invoke must not enqueue a run


def test_invoke_unknown_hook_404(client):
    tc, _, submits = client
    resp = tc.post(
        "/v1/webhooks/wh-nope/invoke",
        content=b"{}",
        headers={"x-signature": "x"},
    )
    assert resp.status_code == 404


def test_invoke_disabled_hook_404(client):
    tc, _, submits = client
    hook = _create_hook(tc)
    tc.post(f"/v1/webhooks/{hook['id']}/enabled", json={"enabled": False})
    body = b"{}"
    resp = tc.post(
        f"/v1/webhooks/{hook['id']}/invoke",
        content=body,
        headers={"x-signature": _hmac_sig(hook["secret"], body)},
    )
    assert resp.status_code == 404


def test_idempotency_key_dedupes_run(client):
    tc, _, submits = client
    hook = _create_hook(tc)
    body = b'{"text":"hi"}'
    headers = {
        "content-type": "application/json",
        "x-signature": _hmac_sig(hook["secret"], body),
        "x-idempotency-key": "abc",
    }
    first = tc.post(f"/v1/webhooks/{hook['id']}/invoke", content=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["deduped"] is False
    second = tc.post(f"/v1/webhooks/{hook['id']}/invoke", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["run_id"] == first.json()["run_id"]
    assert second.json()["deduped"] is True


def test_rate_limit_returns_429(client):
    tc, mgr, submits = client
    hook = mgr.create(name="flood", graph_id="g", rate_limit_per_min=3)
    body = b"{}"
    headers = {"x-signature": _hmac_sig(hook.secret, body)}
    statuses = [
        tc.post(f"/v1/webhooks/{hook.id}/invoke", content=body, headers=headers).status_code
        for _ in range(4)
    ]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    assert len(submits) == 3  # #469: rate-limited 4th invoke must not enqueue


def test_body_over_64kb_rejected(client):
    tc, _, submits = client
    hook = _create_hook(tc)
    big = json.dumps({"blob": "x" * (65 * 1024)}).encode()
    resp = tc.post(
        f"/v1/webhooks/{hook['id']}/invoke",
        content=big,
        headers={"x-signature": _hmac_sig(hook["secret"], big)},
    )
    assert resp.status_code == 413
    assert submits == []  # #469: oversized body must not enqueue a run


def test_invoke_count_audited_on_webhook(client):
    tc, mgr, submits = client
    hook = _create_hook(tc)
    body = b'{"text":"hi"}'
    tc.post(
        f"/v1/webhooks/{hook['id']}/invoke",
        content=body,
        headers={"x-signature": _hmac_sig(hook["secret"], body)},
    )
    refreshed = mgr.get(hook["id"])
    assert refreshed.invoke_count == 1
    assert refreshed.last_invoked_at > 0
