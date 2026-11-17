# Issue #730: guided triage — one LLM call sorts the pending review
# artifact set into ordered chapters, most consequential first. The output
# is a manifest the web review surface renders deterministically; no model
# involvement at render time. Any LLM failure falls back to the unsorted
# single-chapter manifest so the review surface never blocks.
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MAX_ITEM_CONTENT_CHARS = 4000
MAX_ITEMS = 100
TRIAGE_TIMEOUT_S = 90.0

TRIAGE_PROMPT = """You are triaging a pending human review queue for an AI agent platform.
Sort the items into chapters, most consequential first (things a human must
read before anything else go in earlier chapters). Group related items.

Items (id | kind | title | content):
{items}

Respond with ONLY a JSON object, no prose, in this exact shape:
{{"chapters": [{{"title": "short chapter title", "overview": "1-2 sentence overview of what this chapter covers and why it matters", "item_ids": ["<item id>", "..."], "rationale": "why this chapter ranks here"}}]}}

Rules:
- every item id appears in exactly one chapter
- item_ids only reference ids from the list above
- order chapters most consequential first
- keep overviews under 60 words"""


class ReviewTriageEngine:
    def __init__(self, gateway: Any | None = None):
        self.gateway = gateway
        logger.info(
            "ReviewTriageEngine initialized (gateway=%s)",
            "enabled" if gateway else "disabled",
        )

    async def triage(self, items: list[dict]) -> dict:
        self._validate_items(items)
        if self.gateway is None:
            logger.info("review.triage: no gateway, returning unsorted manifest")
            return self._fallback_manifest(items, "no gateway configured")
        prompt = TRIAGE_PROMPT.format(
            items="\n".join(
                f"- {it['id']} | {it.get('kind', 'artifact')} | {it.get('title', '')} | "
                f"{str(it.get('content', ''))[:MAX_ITEM_CONTENT_CHARS]}"
                for it in items
            )
        )
        messages = [{"role": "user", "content": prompt}]
        try:
            resp = await asyncio.wait_for(
                self.gateway.chat(messages=messages, capability="chat"),
                timeout=TRIAGE_TIMEOUT_S,
            )
            if getattr(resp, "finish_reason", "") == "error":
                err = (resp.usage or {}).get("error", "gateway error")
                logger.warning("review.triage: gateway error %s, falling back", err)
                return self._fallback_manifest(items, "gateway error")
            content = getattr(resp, "content", "") or ""
            manifest = self._parse_manifest(content, items)
            if manifest is not None:
                return manifest
            logger.warning("review.triage: unparseable LLM output, falling back")
        except asyncio.TimeoutError:
            logger.warning("review.triage: LLM call timed out, falling back")
        except Exception as exc:
            logger.warning("review.triage: LLM call failed (%s), falling back", exc)
        return self._fallback_manifest(items, "llm triage failed")

    def _validate_items(self, items: list[dict]) -> list[str]:
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty list")
        if len(items) > MAX_ITEMS:
            raise ValueError(f"too many items (max {MAX_ITEMS})")
        ids: list[str] = []
        for it in items:
            if not isinstance(it, dict) or not str(it.get("id", "")).strip():
                raise ValueError("each item must be an object with a non-empty id")
            iid = str(it["id"]).strip()
            if iid in ids:
                raise ValueError(f"duplicate item id: {iid}")
            ids.append(iid)
        return ids

    def _parse_manifest(self, content: str, items: list[dict]) -> dict | None:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z0-9_-]*\n|\n```\s*$", "", text).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        chapters_raw = data.get("chapters")
        if not isinstance(chapters_raw, list) or not chapters_raw:
            return None
        known = {str(it["id"]).strip() for it in items}
        chapters: list[dict] = []
        seen: set[str] = set()
        for ch in chapters_raw:
            if not isinstance(ch, dict):
                continue
            raw_ids = ch.get("item_ids")
            if not isinstance(raw_ids, list):
                continue
            valid_ids = []
            for iid in raw_ids:
                iid = str(iid).strip()
                if iid in known and iid not in seen:
                    valid_ids.append(iid)
                    seen.add(iid)
            if not valid_ids:
                continue
            chapters.append(
                {
                    "title": str(ch.get("title", ""))[:120] or "Untitled chapter",
                    "overview": str(ch.get("overview", ""))[:600],
                    "item_ids": valid_ids,
                    "rationale": str(ch.get("rationale", ""))[:400],
                }
            )
        # Coverage guarantee: any item the model dropped lands in a trailing
        # "Other" chapter so the manifest always references every item.
        missing = [iid for iid in (str(it["id"]).strip() for it in items) if iid not in seen]
        if missing:
            chapters.append(
                {
                    "title": "Other",
                    "overview": "Items the triage call did not place explicitly.",
                    "item_ids": missing,
                    "rationale": "uncategorized fallback",
                }
            )
        if not chapters:
            return None
        return {
            "chapters": chapters,
            "sorted": True,
            "item_count": len(items),
            "items": {str(it["id"]).strip(): it for it in items},
        }

    def _fallback_manifest(self, items: list[dict], reason: str) -> dict:
        ordered = [str(it["id"]).strip() for it in items]
        logger.info("review.triage: unsorted fallback (%s), items=%d", reason, len(items))
        return {
            "chapters": [
                {
                    "title": "Pending review",
                    "overview": "",
                    "item_ids": ordered,
                    "rationale": reason,
                }
            ],
            "sorted": False,
            "item_count": len(items),
            "items": {str(it["id"]).strip(): it for it in items},
        }
