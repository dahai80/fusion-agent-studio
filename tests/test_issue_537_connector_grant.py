from __future__ import annotations

import pytest

from agent_runtime.connectors import ConnectorManager
from agent_runtime.dispatchers.infra import InfraDispatcher

# #537-2 backend: builder connector capabilities carry a stable connector id
# (catalog.route.connector). connector.create must honor it so the grant the
# UI records is the one the capability dispatches through; a failed auth
# probe must surface its reason through the RPC bridge's `message`.


class FakeDaemon:
    def __init__(self, mgr):
        self._mgr = mgr

    def _get_connector_manager(self):
        return self._mgr


@pytest.fixture
def infra(tmp_path):
    mgr = ConnectorManager(tmp_path)
    return InfraDispatcher(FakeDaemon(mgr)), mgr


def _handlers(dispatcher):
    return dispatcher.get_handlers()


async def test_connector_create_forwards_catalog_connector_id(infra):
    dispatcher, mgr = infra
    handler = _handlers(dispatcher)["connector.create"]
    result = await handler({
        "connector_id": "connector.inventory-prod",
        "name": "Inventory",
        "type": "api_key",
        "auth_config": {"api_key": "k-1"},
    })
    assert result["connector_id"] == "connector.inventory-prod"
    assert mgr.get("connector.inventory-prod") is not None


async def test_connector_create_without_id_still_generates(infra):
    dispatcher, _mgr = infra
    handler = _handlers(dispatcher)["connector.create"]
    result = await handler({"name": "Console", "type": "api_key"})
    assert result["connector_id"]
    assert result["connector"]["status"] == "disconnected"


async def test_connect_failure_carries_message(infra):
    dispatcher, mgr = infra
    handlers = _handlers(dispatcher)
    await handlers["connector.create"]({
        "connector_id": "connector.no-key",
        "name": "Missing key",
        "type": "api_key",
        "auth_config": {},
    })
    result = await handlers["connector.connect"]({"connector_id": "connector.no-key"})
    assert result["status"] == "error"
    assert result["message"]
    assert mgr.get("connector.no-key")["status"] == "error"


async def test_connect_success_has_no_error_message(infra):
    dispatcher, mgr = infra
    handlers = _handlers(dispatcher)
    await handlers["connector.create"]({
        "connector_id": "connector.ok",
        "name": "Good one",
        "type": "api_key",
        "auth_config": {"api_key": "secret"},
    })
    result = await handlers["connector.connect"]({"connector_id": "connector.ok"})
    assert result["status"] == "connected"
    assert mgr.get("connector.ok")["status"] == "connected"
    assert "message" not in result
