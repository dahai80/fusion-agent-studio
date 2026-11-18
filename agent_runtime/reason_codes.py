# Issue #747: stable dispatch reason codes for team/task RPC failures.
# Borrowed from multica's internal/dispatch/reason.go contract (plan:
# fusion-research/docs/multica-borrow-plan-1118.zh-CN.md F1). One place maps
# harness exceptions to codes; `message` keeps internal detail, `hint` is
# operator-facing; failures that would leak machine detail collapse to
# invocation_not_allowed (detail stays in server logs only).
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

REASON_CODES = (
    "queued",
    "coalesced",
    "team_not_registered",
    "member_unknown",
    "permission_mode_unknown",
    "invocation_not_allowed",
    "runtime_offline",
    "lead_not_running",
    "budget_exhausted",
    "depth_overflow",
    "task_not_found",
    "spec_unavailable",
    "supplement_cap_reached",
)

HINTS = {
    "queued": "request accepted and queued; poll teams.status for progress",
    "coalesced": "duplicate request coalesced into the in-flight one; poll for its result",
    "team_not_registered": "register the team via teams.register first",
    "member_unknown": "check the member name against teams.status",
    "permission_mode_unknown": "use one of the documented permission modes",
    "invocation_not_allowed": "operation not allowed from your role or in the current team state",
    "runtime_offline": "the agent runtime (kernel runner) is unavailable; start it and retry",
    "lead_not_running": "start the lead loop (teams.lead_start) and retry",
    "budget_exhausted": "raise the token budget or resume the run later",
    "depth_overflow": "reduce orchestration nesting depth",
    "task_not_found": "check the task id via teams.status",
    "spec_unavailable": "seed the team SPEC.md via teams.register or teams.spec_write",
    "supplement_cap_reached": "steering is capped at 3 per run; wait for the run to finish or send an inbox message",
}

_TEAM_STATE_PATTERNS = (
    "illegal lifecycle",
    "not ACTIVE",
    "illegal task transition",
    "duplicate task id",
    "blocked requires",
    "task title required",
    "unknown task status",
)

_MACHINE_PATH_RE = re.compile(r"(?i)/(users|tmp|private|var|opt|home|etc)/")


def _message_patterns(text: str) -> str:
    t = text.lower()
    if "team not registered" in t or "not registered" in t:
        return "team_not_registered"
    if "no lead for team" in t or "lead not running" in t:
        return "lead_not_running"
    if "lead already running" in t:
        return "invocation_not_allowed"
    if "unknown task" in t or "playbook entry not found" in t or "no run attempt" in t:
        return "task_not_found"
    if "unknown member" in t or "unknown permission mode" in t:
        return "member_unknown" if "member" in t else "permission_mode_unknown"
    if "exceeds member cap" in t or "budget exhausted" in t:
        return "budget_exhausted"
    if "supplement cap" in t:
        return "supplement_cap_reached"
    if "depth overflow" in t:
        return "depth_overflow"
    if "spec.md" in t or "spec unavailable" in t:
        return "spec_unavailable"
    if "playbook disabled" in t or "playbook" in t:
        return "invocation_not_allowed"
    if any(p.lower() in t for p in _TEAM_STATE_PATTERNS):
        return "invocation_not_allowed"
    if "runner" in t or "kernel" in t or "agent_fn" in t or "connect" in t and "sock" in t:
        return "runtime_offline"
    return ""


def classify_error(e: Exception) -> str:
    # One-place switch (plan F1): harness exceptions -> reason codes. Unknown
    # failures default to invocation_not_allowed so nothing leaks by accident.
    text = str(e or "")
    pattern = _message_patterns(text)
    if pattern:
        return pattern
    name = type(e).__name__
    if name in ("TeamDefError",):
        return "member_unknown" if "member" in text.lower() else "invocation_not_allowed"
    if name in ("TeamStateError", "SpawnBriefError"):
        return "invocation_not_allowed"
    if name == "LeadBudgetError":
        return "budget_exhausted"
    if name == "LeadError":
        return "lead_not_running"
    if isinstance(e, KeyError):
        return "team_not_registered"
    if isinstance(e, FileNotFoundError):
        return "spec_unavailable" if "spec" in text.lower() else "runtime_offline"
    return "invocation_not_allowed"


def error_payload(code: str, message: str) -> dict:
    if code not in REASON_CODES:
        code = "invocation_not_allowed"
    return {
        "status": "error",
        "reason_code": code,
        "message": str(message or ""),
        "hint": HINTS[code],
    }


def leaks_machine_detail(text: str) -> bool:
    # Non-leaking rule (plan F1): filesystem/socket/machine paths never leave
    # the server; those failures collapse to invocation_not_allowed upstream.
    return bool(_MACHINE_PATH_RE.search(str(text or "")))
