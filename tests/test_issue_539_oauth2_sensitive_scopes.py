from __future__ import annotations

import time

import pytest

from agent_runtime.connectors import ConnectorManager
from agent_runtime.dispatchers.infra import InfraDispatcher

# #539 residuals from the #537 connector gate:
#   R1 — the OAuth2 modal collects an access token and the backend probe
#        requires auth_config.access_token; the two sides now agree and an
#        OAuth2 grant completes end-to-end.
#   R2 — connectors with sensitive scopes refuse connector.connect without
#        an approval reference; a granted reference is recorded on the
#        config (approval_ref + approved_at) for the audit trail.


class FakeDaemon:
    def __init__(self, mgr):
        self._mgr = mgr

    def _get_connector_manager(self):
        return self._mgr


@pytest.fixture
def infra(tmp_path):
    mgr = ConnectorManager(tmp_path)
    return InfraDispatcher(FakeDaemon(mgr)), mgr


def _create(dispatcher, **overrides):
    params = {
        "connector_id": "connector.gh",
        "name": "GitHub",
        "type": "oauth2",
        "auth_config": {"access_token": "gho-token"},
    }
    params.update(overrides)
    return dispatcher.get_handlers()["connector.create"](params)


async def test_oauth2_grant_completes_with_access_token(infra):
    dispatcher, mgr = infra
    handlers = dispatcher.get_handlers()
    created = await _create(dispatcher)
    assert created["connector_id"] == "connector.gh"
    result = await handlers["connector.connect"]({"connector_id": "connector.gh"})
    assert result["status"] == "connected", result
    assert mgr.get("connector.gh")["status"] == "connected"


async def test_oauth2_without_token_still_fails_with_reason(infra):
    dispatcher, _mgr = infra
    handlers = dispatcher.get_handlers()
    await _create(dispatcher, auth_config={})
    result = await handlers["connector.connect"]({"connector_id": "connector.gh"})
    assert result["status"] == "error"
    assert result["message"]


async def test_sensitive_scopes_block_connect_without_approval_ref(infra):
    dispatcher, mgr = infra
    handlers = dispatcher.get_handlers()
    await _create(dispatcher, sensitive_scopes=["billing.write", "pii.read"])
    result = await handlers["connector.connect"]({"connector_id": "connector.gh"})
    assert result["status"] == "error"
    assert "approval" in result["message"]
    # the connector stays ungranted — no partial state leaked
    stored = mgr.get("connector.gh")
    assert stored["status"] == "disconnected"
    assert stored["approval_ref"] == ""
    assert stored["approved_at"] is None


async def test_sensitive_scopes_connect_records_approval_reference(infra):
    dispatcher, mgr = infra
    handlers = dispatcher.get_handlers()
    await _create(dispatcher, sensitive_scopes=["billing.write"])
    before = time.time()
    result = await handlers["connector.connect"](
        {"connector_id": "connector.gh", "approval_ref": " APPR-2026-0042 "}
    )
    assert result["status"] == "connected"
    stored = mgr.get("connector.gh")
    assert stored["status"] == "connected"
    assert stored["approval_ref"] == "APPR-2026-0042"
    assert stored["approved_at"] is not None and stored["approved_at"] >= before


async def test_non_sensitive_connector_needs_no_approval_ref(infra):
    dispatcher, _mgr = infra
    handlers = dispatcher.get_handlers()
    await _create(dispatcher, sensitive_scopes=[])
    result = await handlers["connector.connect"]({"connector_id": "connector.gh"})
    assert result["status"] == "connected"
    assert "message" not in result


async def test_sensitive_scopes_survive_persistence(tmp_path):
    mgr = ConnectorManager(tmp_path)
    mgr.create("Audited", "api_key", {"api_key": "k"}, connector_id="connector.a",
               sensitive_scopes=["pii.read"])
    reloaded = ConnectorManager(tmp_path)
    stored = reloaded.get("connector.a")
    assert stored["sensitive_scopes"] == ["pii.read"]


def test_connector_capability_passes_sensitive_scopes_through():
    import copy

    from agent_runtime.agent_platform import capabilities as caps_mod

    saved = copy.deepcopy(caps_mod.CAPABILITIES)
    try:
        caps_mod.add_connector_capabilities(
            "erp", [{"id": "orders.list", "title": "Orders",
                     "sensitive_scopes": ["pii.read"]}], "test"
        )
        caps = {c["id"]: c for c in caps_mod.active_capabilities()}
        assert caps["connector.erp.orders.list"]["sensitive_scopes"] == ["pii.read"]
    finally:
        caps_mod.CAPABILITIES[:] = copy.deepcopy(saved)
