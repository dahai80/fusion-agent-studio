"""#459: best-effort peer-daemon wake on mailbox write.

After a successful mailbox insert, agent-studio POSTs the configured peer
daemons (fusion-cowork `POST /api/team/notify` contract) so delivery is
push-driven; the poll loop stays as the safety net. A peer that is down,
slow, or not running cowork degrades silently — notify_peers never raises
and never blocks the send path (fire-and-forget daemon thread).
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 2.0


def peer_notify_enabled() -> bool:
    raw = os.environ.get("FUSION_TEAM_PEER_NOTIFY", "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


def peer_notify_urls() -> list[str]:
    raw = os.environ.get("FUSION_TEAM_PEER_NOTIFY_URLS", "").strip()
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def peer_notify_key() -> str:
    # Optional static key for peers whose web API has auth configured
    # (fusion-cowork accepts Authorization: Bearer or x-api-key).
    return os.environ.get("FUSION_TEAM_PEER_NOTIFY_KEY", "").strip()


def notify_peers(team: str = "") -> int:
    # Returns the number of peers scheduled for notify (0 = disabled or
    # unconfigured). Errors surface only in logs.
    if not peer_notify_enabled():
        return 0
    urls = peer_notify_urls()
    if not urls:
        return 0
    thread = threading.Thread(
        target=_post_all, args=(urls, team, peer_notify_key()), daemon=True
    )
    thread.start()
    return len(urls)


def _post_all(urls: list[str], team: str, key: str) -> None:
    try:
        import httpx
    except ImportError:
        logger.debug("peer notify skipped: httpx unavailable")
        return
    payload = {"team": team} if team else {}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    for url in urls:
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=DEFAULT_TIMEOUT_S)
            logger.info("peer notify url=%s status=%s", url, resp.status_code)
        except Exception as e:
            # Deliberately quiet: down/unreachable peers are the normal case
            # (poll fallback covers them). Log at debug so a misconfigured
            # URL is still findable without spamming production logs.
            logger.debug("peer notify failed url=%s err=%s (poll fallback)", url, e)
