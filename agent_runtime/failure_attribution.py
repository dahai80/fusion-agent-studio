# Issue #791: MAST failure attribution bridge over fusion-harness
# failure_attribution (harness main). Converts archived run event dicts
# (AgentEvent.to_dict shape) into the record objects FailureAnalyzer._coarse
# consumes, runs the analyzer, and returns the top verdict for the run.
# Gated by FUSION_MAST_ATTRIBUTION=1 — default off, run flows unchanged.
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ENV_FLAG = "FUSION_MAST_ATTRIBUTION"


def attribution_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "0").strip() == "1"


@dataclass
class _EventRecord:
    # Shape FailureAnalyzer._coarse reads: event ("error"/"tool_call"/...),
    # node_id, ts, payload dict (error/status/message/tool/args keys).
    run_id: str = ""
    event: str = ""
    node_id: str = ""
    ts: float = 0.0
    payload: dict = field(default_factory=dict)


def records_from_events(run_id: str, events: list[dict]) -> list[_EventRecord]:
    records = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        etype = str(e.get("type", "") or e.get("event", "") or "")
        content = str(e.get("content", "") or "")
        metadata = e.get("metadata") if isinstance(e.get("metadata"), dict) else {}
        records.append(
            _EventRecord(
                run_id=run_id,
                event=etype,
                node_id=str(e.get("node_id", "") or ""),
                ts=float(e.get("timestamp", 0.0) or 0.0),
                payload={
                    "error": content,
                    "message": content,
                    "status": str(metadata.get("status", "") or ""),
                    "tool": str(e.get("name", "") or ""),
                    "args": e.get("args", {}) or {},
                },
            )
        )
    return records


def attribute_run(run_id: str, events: list[dict]) -> dict | None:
    # None = no failure candidates (clean run or empty stream) — callers
    # treat that as "no attribution", not an error.
    from fusion_harness.failure_attribution import FailureAnalyzer

    records = records_from_events(run_id, events)
    if not records:
        logger.info("mast attribution run=%s no records", run_id)
        return None
    report = FailureAnalyzer().analyze(records)
    run = report.get("runs", {}).get(run_id) or {}
    verdicts = run.get("verdicts") or []
    if not verdicts:
        logger.info("mast attribution run=%s no candidates", run_id)
        return None
    top = verdicts[0]
    return {
        "run_id": run_id,
        "mode": top.get("mode", ""),
        "cluster": top.get("cluster", ""),
        "confidence": top.get("confidence", 0.0),
        "responsible_agent": run.get("responsible_agent", ""),
        "decisive_moment": run.get("decisive_moment"),
        "handoff_candidates": run.get("handoff_candidates"),
        "verdicts": verdicts,
        "mode_counts": report.get("mode_counts", {}),
        "cluster_counts": report.get("cluster_counts", {}),
    }
