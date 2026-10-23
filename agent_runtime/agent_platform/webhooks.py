"""#446: outbound approval-decision webhooks.

External consumers (e.g. EnviPermit) register a callback URL and receive a
signed POST every time an approval task is decided (approved/rejected) in
the workbench, so their own project state can advance without polling.

- Registry: JSON file (~/.fusion-agent-studio/approval_webhooks.json),
  one record per consumer. Registration is auth-gated REST
  (POST/GET/DELETE /v1/agent-platform/webhooks).
- Delivery: HMAC-SHA256 signature of the JSON body in the
  x-fusion-signature header (secret shared at registration), 3 attempts
  with short backoff, failures logged and surfaced in GET /webhooks
  (last_status/last_attempt_at) — never raise into the approval path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

WEBHOOK_FILE = Path.home() / ".fusion-agent-studio" / "approval_webhooks.json"
_SIGNATURE_HEADER = "x-fusion-signature"
_MAX_URL_LEN = 2048


def _validate_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body,
                    hashlib.sha256).hexdigest()


class WebhookRegistry:
    def __init__(self, path: str = ""):
        if not path:
            path = str(WEBHOOK_FILE)
        self.path = Path(path)
        self._records: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._records = {
                    k: v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            self._records = {}
        except Exception as e:
            logger.warning("webhook registry unreadable (%s): %s",
                           self.path, e)
            self._records = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._records, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            logger.warning("webhook registry write failed: %s", e)

    def register(self, url: str, secret: str,
                 events: Optional[List[str]] = None) -> dict:
        url = (url or "").strip()
        if not _validate_url(url):
            raise ValueError(f"invalid webhook url: {url[:120]}")
        if len(url) > _MAX_URL_LEN:
            raise ValueError("webhook url too long")
        if not secret or len(secret) < 16:
            raise ValueError("webhook secret must be at least 16 chars")
        events = events or ["approval.decided"]
        if "approval.decided" not in events:
            events.append("approval.decided")
        record = {
            "url": url,
            "secret": secret,
            "events": events,
            "created_at": time.time(),
            "last_status": 0,
            "last_attempt_at": None,
            "delivery_count": 0,
        }
        # one record per URL — re-registration rotates the secret
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        self._records[key] = record
        self._save()
        logger.info("approval webhook registered: %s (id=%s)", url, key)
        return {"id": key, "url": url, "events": events}

    def delete(self, webhook_id: str) -> bool:
        if webhook_id in self._records:
            del self._records[webhook_id]
            self._save()
            return True
        return False

    def list(self) -> List[dict]:
        out = []
        for key, rec in self._records.items():
            out.append({
                "id": key,
                "url": rec.get("url", ""),
                "events": rec.get("events", []),
                "created_at": rec.get("created_at"),
                "last_status": rec.get("last_status"),
                "last_attempt_at": rec.get("last_attempt_at"),
                "delivery_count": rec.get("delivery_count", 0),
            })
        return out

    async def fire(self, event: str, payload: Dict[str, Any]) -> None:
        """Deliver to every consumer subscribed to this event.

        Never raises: delivery problems are logged + recorded on the
        registry record; the approval decision itself already succeeded.
        """
        if not self._records:
            return
        body = json.dumps({"event": event, "payload": payload},
                          ensure_ascii=False).encode("utf-8")
        import httpx

        for key, rec in list(self._records.items()):
            if event not in (rec.get("events") or []):
                continue
            signature = _sign(rec.get("secret", ""), body)
            status = 0
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(
                            timeout=10, trust_env=False) as client:
                        resp = await client.post(
                            rec["url"], content=body,
                            headers={"Content-Type": "application/json",
                                     _SIGNATURE_HEADER: signature})
                    status = resp.status_code
                    if 200 <= status < 300:
                        break
                except Exception as e:
                    logger.warning(
                        "webhook delivery failed (id=%s attempt=%d): %s",
                        key, attempt + 1, e)
                await _backoff(attempt)
            rec["last_status"] = status
            rec["last_attempt_at"] = time.time()
            rec["delivery_count"] = int(rec.get("delivery_count", 0)) + 1
            self._save()
            if not 200 <= status < 300:
                logger.warning("webhook delivery gave up: id=%s url=%s "
                               "status=%s", key, rec.get("url", ""), status)
            else:
                logger.info("webhook delivered: id=%s status=%d", key, status)


async def _backoff(attempt: int) -> None:
    import asyncio

    await asyncio.sleep(0.5 * (attempt + 1))


_default_registry: Optional[WebhookRegistry] = None


def get_webhook_registry() -> WebhookRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = WebhookRegistry()
    return _default_registry


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(_sign(secret, body), signature)
