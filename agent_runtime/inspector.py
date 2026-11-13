"""E4: Inspector data-plane aggregations over run event streams.

Pure functions over event dicts (AgentEvent.to_dict() shape) so they work
on managed-run buffers, run-history events, and replayed trajectories:

- runs.tool_stats: per-tool call count / failure rate / median duration
  (TOOL_CALL paired with the next TOOL_RESULT of the same name+node).
- runs.thread_lanes: per-lane timeline from the #497 event tree
  (event_id/parent_id — the parent_tool_use_id equivalent), one lane per
  top-level event chain, subagent spans nest as children.
"""

from __future__ import annotations

import logging
import statistics

logger = logging.getLogger(__name__)

_FAILURE_PREFIXES = ("Error:", "Blocked", "SafetyGateway blocked")
_TOOL_CALL = "tool_call"
_TOOL_RESULT = "tool_result"


def _is_failure(result_event: dict) -> bool:
    meta = result_event.get("metadata") or {}
    if meta.get("error"):
        return True
    content = str(result_event.get("content", ""))
    return content.startswith(_FAILURE_PREFIXES)


def tool_stats(events: list[dict]) -> list[dict]:
    # Pair each TOOL_CALL with the next TOOL_RESULT sharing (name, node_id);
    # unpaired calls count as still-running (no duration, not a failure).
    pending: dict[tuple[str, str], dict] = {}
    rows: dict[str, dict] = {}

    def _row(name: str) -> dict:
        if name not in rows:
            rows[name] = {
                "tool": name,
                "calls": 0,
                "failures": 0,
                "durations_ms": [],
            }
        return rows[name]

    for ev in events:
        etype = ev.get("type", "")
        name = str(ev.get("name", ""))
        if etype == _TOOL_CALL and name:
            key = (name, str(ev.get("node_id", "")))
            _row(name)["calls"] += 1
            pending[key] = ev
        elif etype == _TOOL_RESULT and name:
            key = (name, str(ev.get("node_id", "")))
            call = pending.pop(key, None)
            row = _row(name)
            if _is_failure(ev):
                row["failures"] += 1
            if call is not None:
                ts0 = float(call.get("timestamp", 0.0) or 0.0)
                ts1 = float(ev.get("timestamp", 0.0) or 0.0)
                if ts1 > ts0:
                    row["durations_ms"].append((ts1 - ts0) * 1000.0)

    stats = []
    for name in sorted(rows):
        row = rows[name]
        durs = sorted(row.pop("durations_ms"))
        if durs:
            median_ms = round(statistics.median(durs), 1)
            p95_ms = round(durs[min(len(durs) - 1, int(len(durs) * 0.95))], 1)
        else:
            median_ms = 0.0
            p95_ms = 0.0
        stats.append(
            {
                "tool": row["tool"],
                "calls": row["calls"],
                "failures": row["failures"],
                "failure_rate": round(row["failures"] / row["calls"], 4) if row["calls"] else 0.0,
                "median_ms": median_ms,
                "p95_ms": p95_ms,
            }
        )
    logger.info("tool_stats computed tools=%d events=%d", len(stats), len(events))
    return stats


def thread_lanes(events: list[dict]) -> dict:
    # Build the #497 event tree; one lane per root (parent_id == ""), children
    # nest under their parent. Lane min/max timestamps give the minimap span.
    by_id: dict[str, dict] = {}
    children: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for ev in events:
        eid = str(ev.get("event_id", "") or "")
        if eid:
            by_id[eid] = ev
        pid = str(ev.get("parent_id", "") or "")
        if pid:
            children.setdefault(pid, []).append(ev)
        else:
            roots.append(ev)

    lanes = []
    for idx, root in enumerate(roots):
        lane_events: list[dict] = []
        stack = [root]
        max_depth = 0
        while stack:
            cur = stack.pop()
            depth = cur.pop("_depth", 0) if isinstance(cur, dict) and "_depth" in cur else 0
            max_depth = max(max_depth, depth)
            lane_events.append(
                {
                    "event_id": cur.get("event_id", ""),
                    "parent_id": cur.get("parent_id", ""),
                    "type": cur.get("type", ""),
                    "name": cur.get("name", ""),
                    "node_id": cur.get("node_id", ""),
                    "timestamp": cur.get("timestamp", 0.0),
                    "depth": depth,
                }
            )
            for ch in sorted(
                children.get(cur.get("event_id", ""), []),
                key=lambda e: float(e.get("timestamp", 0.0) or 0.0),
            ):
                ch = dict(ch)
                ch["_depth"] = depth + 1
                stack.append(ch)

        lane_events.sort(key=lambda e: (float(e.get("timestamp", 0.0) or 0.0), e.get("depth", 0)))
        ts_list = [float(e.get("timestamp", 0.0) or 0.0) for e in lane_events if e.get("timestamp")]
        lanes.append(
            {
                "lane_id": root.get("event_id", "") or f"lane-{idx}",
                "root_type": root.get("type", ""),
                "root_name": root.get("name", ""),
                "label": root.get("name", "") or root.get("type", ""),
                "events": lane_events,
                "event_count": len(lane_events),
                "max_depth": max_depth,
                "t_start": min(ts_list) if ts_list else 0.0,
                "t_end": max(ts_list) if ts_list else 0.0,
            }
        )
    lanes.sort(key=lambda l: l["t_start"])
    logger.info("thread_lanes computed lanes=%d events=%d", len(lanes), len(events))
    return {"lanes": lanes, "lane_count": len(lanes)}
