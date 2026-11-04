"""#579: transport-trust context for loopback credential issuance.

The daemon exposes a first-party recovery path for admin API keys
(apikey.loopback_create / apikey.loopback_rotate) that bypasses HTTP-key
auth. It must ONLY be reachable over the peer-uid-verified UDS control
plane (same trust model as the daemon itself, /tmp/fusion-studio.sock
0o600 + #209 peer uid check) — never via the HTTP REST surface or the
#330 web RPC bridge. The trust marker is a ContextVar set once per UDS
connection task after the peer-uid check, so concurrent connections and
HTTP requests cannot observe each other's transport.
"""

import contextvars
import logging

logger = logging.getLogger(__name__)

TRUST_HTTP = "http"
TRUST_UDS = "uds"

_transport_trust: contextvars.ContextVar[str] = contextvars.ContextVar(
    "fusion_transport_trust", default=TRUST_HTTP
)


def set_transport_trust(trust: str) -> None:
    _transport_trust.set(trust)
    logger.debug("transport trust set to %s", trust)


def current_transport_trust() -> str:
    return _transport_trust.get()


def is_loopback_trusted() -> bool:
    return current_transport_trust() == TRUST_UDS
