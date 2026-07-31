"""Aware engine — 3-tier file awareness: debounce + AST diff + model gate.

Tier 1: 5s debounce — coalesce rapid file changes
Tier 2: AST diff — skip if no semantic change
Tier 3: 0.5B model gate — lightweight LLM judges significance

Prevents unnecessary LLM calls when files change but meaning doesn't.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 5.0
AST_DIFF_CACHE_SIZE = 200


@dataclass
class FileEvent:
    path: str = ""
    event_type: str = "modified"
    content_hash: str = ""
    timestamp: float = 0.0
    event_id: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.event_id:
            self.event_id = uuid.uuid4().hex[:8]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "path": self.path,
            "event_type": self.event_type,
            "content_hash": self.content_hash,
            "timestamp": self.timestamp,
        }


@dataclass
class AwareResult:
    path: str = ""
    tier: int = 0
    significant: bool = False
    reason: str = ""
    event: FileEvent | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "tier": self.tier,
            "significant": self.significant,
            "reason": self.reason,
            "event": self.event.to_dict() if self.event else None,
        }


class DebounceLayer:
    """Tier 1: Coalesce rapid file changes within a time window."""

    def __init__(self, debounce_seconds: float = DEBOUNCE_SECONDS):
        self.debounce_seconds = debounce_seconds
        self._last_event: dict[str, float] = {}
        self._pending: dict[str, FileEvent] = {}

    def process(self, event: FileEvent) -> AwareResult | None:
        path = event.path
        now = time.time()
        last = self._last_event.get(path, 0)

        if now - last < self.debounce_seconds:
            self._pending[path] = event
            logger.debug("Debounced: %s (%.1fs since last)", path, now - last)
            return None

        self._last_event[path] = now
        self._pending.pop(path, None)
        return AwareResult(
            path=path,
            tier=1,
            significant=True,
            reason="Passed debounce window",
            event=event,
        )

    def flush_pending(self) -> list[AwareResult]:
        results = []
        for path, event in list(self._pending.items()):
            results.append(AwareResult(
                path=path,
                tier=1,
                significant=True,
                reason="Flushed from debounce queue",
                event=event,
            ))
        self._pending.clear()
        self._last_event.clear()
        return results


class ASTDiffLayer:
    """Tier 2: Skip if no semantic (AST-level) change detected."""

    def __init__(self, cache_size: int = AST_DIFF_CACHE_SIZE):
        self.cache_size = cache_size
        self._ast_cache: dict[str, str] = {}

    def process(self, event: FileEvent, current_content: str = "") -> AwareResult:
        path = event.path
        try:
            tree = ast.parse(current_content)
            current_ast_hash = hashlib.md5(ast.dump(tree).encode()).hexdigest()
        except SyntaxError:
            current_ast_hash = hashlib.md5(current_content.encode()).hexdigest()
        except Exception:
            current_ast_hash = hashlib.md5(current_content.encode()).hexdigest()

        cached = self._ast_cache.get(path)
        self._ast_cache[path] = current_ast_hash

        if len(self._ast_cache) > self.cache_size:
            oldest_key = next(iter(self._ast_cache))
            del self._ast_cache[oldest_key]

        if cached and cached == current_ast_hash:
            logger.debug("AST diff: no semantic change for %s", path)
            return AwareResult(
                path=path,
                tier=2,
                significant=False,
                reason="No AST change detected",
                event=event,
            )

        return AwareResult(
            path=path,
            tier=2,
            significant=True,
            reason="AST change detected",
            event=event,
        )

    def clear_cache(self) -> None:
        self._ast_cache.clear()

    def get_cached_hash(self, path: str) -> str | None:
        return self._ast_cache.get(path)


class ModelGateLayer:
    """Tier 3: Lightweight model judges if change is significant.

    Falls back to heuristic if no model client available.
    """

    def __init__(self, model_client=None, model_name: str = ""):
        self.model_client = model_client
        self.model_name = model_name
        self._call_count = 0

    def process(self, event: FileEvent, old_content: str = "", new_content: str = "") -> AwareResult:
        self._call_count += 1
        path = event.path

        if not self.model_client:
            result = self._heuristic_gate(old_content, new_content)
            return AwareResult(
                path=path,
                tier=3,
                significant=result,
                reason="Heuristic gate (no model)" if not result else "Heuristic gate: significant",
                event=event,
            )

        return self._model_gate(path, old_content, new_content, event)

    def _heuristic_gate(self, old_content: str, new_content: str) -> bool:
        if not old_content and new_content:
            return True
        if not new_content:
            return True
        old_lines = set(old_content.splitlines())
        new_lines = set(new_content.splitlines())
        diff_lines = old_lines.symmetric_difference(new_lines)
        ratio = len(diff_lines) / max(len(old_lines), len(new_lines), 1)
        return ratio > 0.05

    def _model_gate(self, path: str, old_content: str, new_content: str, event: FileEvent) -> AwareResult:
        try:
            prompt = (
                "Determine if this code change is significant enough to warrant "
                "re-analysis. Reply ONLY 'yes' or 'no'.\n\n"
                f"--- OLD ---\n{old_content[-2000:]}\n\n"
                f"--- NEW ---\n{new_content[-2000:]}"
            )
            response = self.model_client.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=10,
            )
            answer = response.strip().lower()
            significant = answer.startswith("yes")
            logger.info("Model gate for %s: %s", path, answer)
            return AwareResult(
                path=path,
                tier=3,
                significant=significant,
                reason=f"Model gate: {answer}",
                event=event,
            )
        except Exception as e:
            logger.warning("Model gate failed for %s: %s, using heuristic", path, e)
            result = self._heuristic_gate(old_content, new_content)
            return AwareResult(
                path=path,
                tier=3,
                significant=result,
                reason=f"Model gate error, heuristic: {result}",
                event=event,
            )


class AwareEngine:
    """3-tier aware cascade: debounce -> AST diff -> model gate."""

    def __init__(
        self,
        debounce_seconds: float = DEBOUNCE_SECONDS,
        model_client=None,
        model_name: str = "",
    ):
        self.tier1 = DebounceLayer(debounce_seconds)
        self.tier2 = ASTDiffLayer()
        self.tier3 = ModelGateLayer(model_client, model_name)
        self._content_cache: dict[str, str] = {}
        self._stats = {"tier1_passed": 0, "tier2_blocked": 0, "tier3_called": 0, "significant": 0}

    def process_event(self, event: FileEvent, content: str = "") -> AwareResult:
        old_content = self._content_cache.get(event.path, "")
        if content:
            self._content_cache[event.path] = content

        result = self.tier1.process(event)
        if result is None:
            return AwareResult(path=event.path, tier=0, significant=False, reason="Debounced", event=event)

        self._stats["tier1_passed"] += 1

        if content:
            result = self.tier2.process(event, content)
            if not result.significant:
                self._stats["tier2_blocked"] += 1
                return result

        self._stats["tier3_called"] += 1
        result = self.tier3.process(event, old_content, content)
        if result.significant:
            self._stats["significant"] += 1
        return result

    def process_file_change(self, path: str, event_type: str = "modified", content: str = "") -> AwareResult:
        try:
            if not content:
                content = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            content = ""

        content_hash = hashlib.md5(content.encode()).hexdigest() if content else ""
        event = FileEvent(path=path, event_type=event_type, content_hash=content_hash)
        return self.process_event(event, content)

    def get_stats(self) -> dict[str, Any]:
        return dict(self._stats)

    def reset_stats(self) -> None:
        self._stats = {"tier1_passed": 0, "tier2_blocked": 0, "tier3_called": 0, "significant": 0}

    def flush(self) -> list[AwareResult]:
        return self.tier1.flush_pending()
