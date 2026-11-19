# Issue #771: single adoption point for fusion_harness.json_repair.
#
# Every LLM-output JSON parse goes through parse_model_json(): strict
# json.loads first (the common case, zero overhead), then the kernel
# repair ladder. Invocations/repaired/failed are counted per context so a
# run's parse-repair success rate is observable, and the repair trace
# (which ladder rung succeeded) is logged for debugging.
from __future__ import annotations

import json
import logging
import threading

from fusion_harness.json_repair import JSONRepairError
from fusion_harness.json_repair import parse_repair as _kernel_repair

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_stats: dict = {"invocations": 0, "strict": 0, "repaired": 0, "failed": 0}
_by_context: dict = {}


def _bump(context: str, outcome: str) -> None:
    with _lock:
        _stats[outcome] = _stats[outcome] + 1
        row = _by_context.setdefault(context, {"invocations": 0, "repaired": 0, "failed": 0})
        row["invocations"] += 1
        if outcome in ("repaired", "failed"):
            row[outcome] += 1


def parse_repair_stats() -> dict:
    """Counters since process start: totals plus per-context breakdown
    (invocations / repaired / failed). Exposed for RPC/telemetry consumption."""
    with _lock:
        return {"totals": dict(_stats), "by_context": {k: dict(v) for k, v in _by_context.items()}}


def _repair_rung(text: str) -> str:
    # Coarse repair trace: name the transformations that were needed to make
    # the text parse, mirroring the kernel ladder order. Kernel helpers are
    # private — a drift falls back to the generic "ladder" label instead of
    # breaking the call site.
    try:
        from fusion_harness.json_repair import (
            _close_truncated,
            _drop_incomplete_tail,
            _extract_balanced,
            _is_truncated,
            _single_to_double_quotes,
            _strip_fences,
            _strip_trailing_commas,
            _take_open,
        )
    except ImportError:
        return "ladder"
    fenced = _strip_fences(text)
    extracted = _extract_balanced(fenced)
    bases = [("fence_strip", fenced)] + (
        [("balanced_extract", extracted)] if extracted is not None else []
    )
    for base_name, base in bases:
        quoted = _single_to_double_quotes(base)
        for quote_name, variant in (("quote_fix", quoted), ("verbatim", base)):
            comma_name = "trailing_commas"
            candidates = [
                (comma_name, _strip_trailing_commas(variant)),
                ("verbatim", variant),
            ]
            if _is_truncated(variant):
                candidates += [
                    ("close_truncated", _close_truncated(_take_open(variant))),
                    ("drop_truncated_tail", _close_truncated(_drop_incomplete_tail(variant))),
                ]
            for rung, candidate in candidates:
                try:
                    json.loads(candidate)
                    flags = [base_name, quote_name, rung]
                    return (
                        "+".join(dict.fromkeys(f for f in flags if f != "verbatim")) or "verbatim"
                    )
                except ValueError:
                    continue
    return "ladder"


def parse_model_json(content: object, context: str = "") -> object:
    """Parse LLM output JSON through the kernel repair ladder.

    Raises JSONRepairError when every rung fails (callers keep their
    existing typed-error fallbacks). Strict parses short-circuit with zero
    ladder overhead; repairs are counted and logged with the rung that
    succeeded."""
    text = str(content)
    _bump(context, "invocations")
    try:
        data = json.loads(text)
        _bump(context, "strict")
        return data
    except ValueError:
        pass
    try:
        data = _kernel_repair(text)
    except JSONRepairError:
        _bump(context, "failed")
        logger.warning("parse_repair exhausted ladder context=%s input=%r", context, text[:120])
        raise
    rung = _repair_rung(text)
    _bump(context, "repaired")
    logger.info("LLM JSON repaired context=%s rung=%s len=%d", context, rung, len(text))
    return data
