"""#500: shared E2E HTTP client — static key + JSON helpers.

The daemon subprocess runs with FUSION_AGENT_STUDIO_API_KEY=E2E_API_KEY so
auth is deterministic (machine-global key files under ~/.fusion-agent-studio
would otherwise make `_auth_configured()` true with an unknown key). Every
request — Python-side and browser-side — carries this key.
"""

from __future__ import annotations

import json
import urllib.request

E2E_API_KEY = "fas-e2e-static-key"


def http_json(base: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "x-api-key": E2E_API_KEY},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def rpc(base: str, method: str, params: dict | None = None) -> dict:
    return http_json(base, "POST", "/v1/rpc", {"method": method, "params": params or {}})
