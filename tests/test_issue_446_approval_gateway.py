"""Issue #446 — external approval tasks + decision webhooks.

EnviPermit rides the approval workbench instead of a bespoke state
machine: REST task creation with requester != assigned reviewer, an
opaque consumer payload, decision records (no studio dispatch), and a
signed outbound webhook (approval.decided) so the consumer advances its
own project state without polling.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_runtime.agent_platform.door import (
    CapabilityApprovalStore,
    DoorError,
)
from agent_runtime.agent_platform.webhooks import (
    WebhookRegistry,
    verify_signature,
)


@pytest.fixture
def store(tmp_path):
    return CapabilityApprovalStore(str(tmp_path / "approvals.db"))


@pytest.fixture
def registry(tmp_path, monkeypatch):
    import agent_runtime.agent_platform.webhooks as wh

    reg = WebhookRegistry(str(tmp_path / "webhooks.json"))
    monkeypatch.setattr(wh, "_default_registry", reg)
    return reg


class TestExternalTaskCreation:

    def test_create_external_roundtrip(self, store):
        rec = store.create_external(
            title="Permit review: P-2026-114",
            requester="envipermit:engineer-a",
            assigned_to="envipermit:reviewer-b",
            params={"project_id": "P-2026-114", "doc_version": 3,
                    "evidence": ["soil report", "discharge plan"]})
        assert rec["status"] == "pending"
        assert rec["kind"] == "external"
        assert rec["capability_id"] == "external"
        assert rec["requester"] == "envipermit:engineer-a"
        assert rec["assigned_to"] == "envipermit:reviewer-b"
        assert rec["params"]["project_id"] == "P-2026-114"
        fetched = store.get(rec["id"])
        assert fetched["title"] == "Permit review: P-2026-114"

    def test_requester_equals_reviewer_rejected(self, store):
        with pytest.raises(DoorError) as e:
            store.create_external(title="t", requester="same",
                                  assigned_to="same", params={})
        assert e.value.status_code == 422

    def test_list_filter_by_status(self, store):
        rec = store.create_external(title="t", requester="a",
                                    assigned_to="b", params={})
        store.decide(rec["id"], "approved", "b")
        pending = store.list(status="pending")
        assert all(r["status"] == "pending" for r in pending)
        assert rec["id"] not in [r["id"] for r in pending]
        approved = store.list(status="approved")
        assert rec["id"] in [r["id"] for r in approved]

    def test_mutation_records_default_kind(self, store):
        rec = store.create("graph.create", "agent-x", 1, "studio",
                           {"name": "w"}, requester="agent:agent-x")
        assert rec["kind"] == "mutation"
        assert rec["assigned_to"] == ""


class TestExternalDecisionRules:

    async def test_self_approval_denied(self, store):
        rec = store.create_external(title="t", requester="eng",
                                    assigned_to="rev", params={})
        from agent_runtime.agent_platform.door import CapabilityDoor

        door = CapabilityDoor(approval_store=store)
        with pytest.raises(DoorError) as e:
            await door.approve(rec["id"], "eng")
        assert e.value.status_code == 403

    async def test_wrong_reviewer_denied(self, store):
        rec = store.create_external(title="t", requester="eng",
                                    assigned_to="rev", params={})
        from agent_runtime.agent_platform.door import CapabilityDoor

        door = CapabilityDoor(approval_store=store)
        with pytest.raises(DoorError) as e:
            await door.approve(rec["id"], "someone-else")
        assert e.value.status_code == 403

    async def test_approve_records_decision_no_dispatch(self, store):
        rec = store.create_external(
            title="t", requester="eng", assigned_to="rev",
            params={"project_id": "P-1"})
        from agent_runtime.agent_platform.door import CapabilityDoor

        door = CapabilityDoor(approval_store=store)
        out = await door.approve(rec["id"], "rev")
        assert out["status"] == "approved"
        assert out["decided_by"] == "rev"
        assert "dispatch_status_code" not in out

    async def test_reject_carries_reason(self, store):
        rec = store.create_external(title="t", requester="eng",
                                    assigned_to="rev", params={})
        from agent_runtime.agent_platform.door import CapabilityDoor

        door = CapabilityDoor(approval_store=store)
        out = await door.reject(rec["id"], "rev",
                                reason="discharge plan outdated")
        assert out["status"] == "rejected"
        assert out["result"] == "discharge plan outdated"

    async def test_double_decision_conflict(self, store):
        rec = store.create_external(title="t", requester="eng",
                                    assigned_to="rev", params={})
        from agent_runtime.agent_platform.door import CapabilityDoor

        door = CapabilityDoor(approval_store=store)
        await door.approve(rec["id"], "rev")
        with pytest.raises(DoorError) as e:
            await door.reject(rec["id"], "rev")
        assert e.value.status_code == 409


class TestWebhookRegistry:

    def test_register_and_list(self, registry):
        rec = registry.register("http://127.0.0.1:9/hook",
                                "s" * 16)
        assert rec["url"] == "http://127.0.0.1:9/hook"
        assert rec["events"] == ["approval.decided"]
        assert len(registry.list()) == 1
        assert "secret" not in registry.list()[0]

    def test_register_validates_url(self, registry):
        with pytest.raises(ValueError):
            registry.register("not-a-url", "s" * 16)
        with pytest.raises(ValueError):
            registry.register("ftp://x/hook", "s" * 16)

    def test_register_validates_secret(self, registry):
        with pytest.raises(ValueError):
            registry.register("http://127.0.0.1:9/hook", "short")

    def test_reregistration_rotates_secret(self, registry):
        first = registry.register("http://127.0.0.1:9/hook", "a" * 16)
        second = registry.register("http://127.0.0.1:9/hook", "b" * 16)
        assert first["id"] == second["id"]
        assert len(registry.list()) == 1
        assert registry._records[first["id"]]["secret"] == "b" * 16

    def test_delete(self, registry):
        rec = registry.register("http://127.0.0.1:9/hook", "s" * 16)
        assert registry.delete(rec["id"]) is True
        assert registry.delete(rec["id"]) is False
        assert registry.list() == []

    def test_verify_signature(self):
        import hashlib
        import hmac

        body = b'{"event": "approval.decided"}'
        real = hmac.new(b"secret-1234567890", body,
                        hashlib.sha256).hexdigest()
        assert verify_signature("secret-1234567890", body, real) is True
        # #580 wire format: prefixed value verifies too
        assert verify_signature("secret-1234567890", body,
                                "sha256=" + real) is True
        assert verify_signature("secret-1234567890", body, "0" * 64) is False


class _Sink(BaseHTTPRequestHandler):
    deliveries = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        _Sink.deliveries.append({
            "raw": raw,
            "body": json.loads(raw),
            "signature": self.headers.get("x-fusion-signature", ""),
        })
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def webhook_sink():
    _Sink.deliveries = []
    server = HTTPServer(("127.0.0.1", 0), _Sink)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/hook"
    server.shutdown()


class TestWebhookDelivery:

    async def test_decision_fires_signed_webhook(self, store, registry,
                                                 webhook_sink):
        registry.register(webhook_sink, "shared-secret-0123456789")
        rec = store.create_external(
            title="Permit P-2", requester="eng", assigned_to="rev",
            params={"project_id": "P-2", "doc_version": 7})
        from agent_runtime.agent_platform.door import CapabilityDoor

        door = CapabilityDoor(approval_store=store)
        await door.approve(rec["id"], "rev")
        assert len(_Sink.deliveries) == 1
        d = _Sink.deliveries[0]
        # #580: flat decision body — fields at the top level, `event` marker
        # alongside them, no nested envelope.
        body = d["body"]
        assert body["event"] == "approval.decided"
        assert body["approval_id"] == rec["id"]
        assert body["status"] == "approved"
        assert body["approved"] is True
        assert body["decided_by"] == "rev"
        assert body["params"]["project_id"] == "P-2"
        # #580 wire format: signature carries the algorithm prefix
        assert d["signature"].startswith("sha256=")
        assert verify_signature("shared-secret-0123456789", d["raw"],
                                d["signature"]) is True
        assert verify_signature("wrong-secret-987654321", d["raw"],
                                d["signature"]) is False

    async def test_cowork_consumer_contract(self, store, registry,
                                            webhook_sink):
        # #580: pin the exact extraction the cowork consumer performs
        # (fusion_cowork/server/web_api.py hooks_webhook): verify
        # `sha256=<hex>` HMAC over the raw body, then read top-level
        # `approved` to resume or deny the HITL run.
        import hashlib
        import hmac as _hmac

        registry.register(webhook_sink, "cowork-shared-secret-0123")
        rec = store.create_external(
            title="Gate resume", requester="cowork-bot", assigned_to="rev",
            params={})
        from agent_runtime.agent_platform.door import CapabilityDoor

        door = CapabilityDoor(approval_store=store)
        await door.approve(rec["id"], "rev")
        d = _Sink.deliveries[0]

        expected = "sha256=" + _hmac.new(
            b"cowork-shared-secret-0123", d["raw"], hashlib.sha256).hexdigest()
        assert d["signature"] == expected
        assert d["body"].get("approved") is True
        assert d["body"].get("decided_by") == "rev"

        rec2 = store.create_external(
            title="Gate resume deny", requester="cowork-bot",
            assigned_to="rev", params={})
        await door.reject(rec2["id"], "rev", "nope")
        d2 = _Sink.deliveries[1]
        expected2 = "sha256=" + _hmac.new(
            b"cowork-shared-secret-0123", d2["raw"], hashlib.sha256).hexdigest()
        assert d2["signature"] == expected2
        assert d2["body"].get("approved") is False
        assert d2["body"].get("reason") == "nope"

    async def test_no_webhooks_no_error(self, store, registry):
        rec = store.create_external(title="t", requester="eng",
                                    assigned_to="rev", params={})
        from agent_runtime.agent_platform.door import CapabilityDoor

        door = CapabilityDoor(approval_store=store)
        out = await door.reject(rec["id"], "rev", reason="none registered")
        assert out["status"] == "rejected"

    async def test_unreachable_webhook_does_not_break_decision(
            self, store, registry):
        registry.register("http://127.0.0.1:1/nope", "s" * 16)
        rec = store.create_external(title="t", requester="eng",
                                    assigned_to="rev", params={})
        from agent_runtime.agent_platform.door import CapabilityDoor

        door = CapabilityDoor(approval_store=store)
        out = await door.approve(rec["id"], "rev")
        assert out["status"] == "approved"
        entry = registry.list()[0]
        assert entry["last_status"] == 0


class TestRestSurface:

    def test_routes_registered(self):
        from agent_runtime import api_server

        paths = {getattr(r, "path", "") for r in api_server.app.routes}
        assert "/v1/agent-platform/approvals" in paths
        assert "/v1/agent-platform/webhooks" in paths
        assert "/v1/agent-platform/webhooks/{webhook_id}" in paths

    def test_schema_migration_on_old_store(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "old.db")
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE capability_approvals (
               id TEXT PRIMARY KEY, capability_id TEXT NOT NULL,
               agent_id TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
               publisher TEXT DEFAULT '', params TEXT NOT NULL DEFAULT '{}',
               status TEXT NOT NULL DEFAULT 'pending',
               requester TEXT DEFAULT '', decided_by TEXT DEFAULT '',
               created_at REAL NOT NULL, decided_at REAL,
               result TEXT DEFAULT '')""")
        conn.execute(
            "INSERT INTO capability_approvals VALUES "
            "('old1','graph.create','a',1,'', '{}','pending','r','',1.0,"
            "NULL,'')")
        conn.commit()
        conn.close()
        store = CapabilityApprovalStore(db)
        rec = store.get("old1")
        assert rec["kind"] == "mutation"
        rec2 = store.create_external(title="t", requester="a",
                                     assigned_to="b", params={})
        assert rec2["kind"] == "external"
