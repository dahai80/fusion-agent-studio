# Issue #799: evidence-based tool pruning telemetry (TokenEconomizer borrow).
#
# Telemetry-only phase: a per-session tool-usage ledger plus deterministic
# unused-streak proposals. Every loaded tool definition is sent with every
# request, so unused tools are a pure per-request tax; this module measures
# which loaded tools a workspace never uses so a human (or a later, explicit
# apply step) can decide what to exclude.
#
# Rules (deterministic, zero model calls):
#   - work-surface tools (file/shell/git-class) are never proposed
#   - any observed use resets a tool's unused streak
#   - a tool is proposed after staying unused for `unused_sessions`
#     consecutive sessions; rarely-needed classes get a 3x longer streak
#   - the ledger only proposes; it never mutates tool registration
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

PROTECTED_TOOLS = frozenset(
    {
        "register_tool",
        "unregister_tool",
        "tool_search",
        "subagent_spawn",
        "subagent_status",
    }
)

PROTECTED_TOOL_PREFIXES = (
    "file_",
    "read_",
    "write_",
    "edit_",
    "shell",
    "terminal",
    "git_",
    "code_",
    "artifact_",
)

RARE_TOOL_PREFIXES = (
    "mcp_",
    "computer_",
    "notebook",
)


def is_protected_tool(name: str) -> bool:
    n = str(name or "")
    if n in PROTECTED_TOOLS:
        return True
    return any(n.startswith(p) for p in PROTECTED_TOOL_PREFIXES)


def is_rare_class_tool(name: str) -> bool:
    n = str(name or "")
    return any(n.startswith(p) for p in RARE_TOOL_PREFIXES)


class ToolUsageLedger:
    # JSON-backed session -> tool-set map. Records accumulate in memory and
    # flush atomically (tmp + rename) on every record; tool-call frequency
    # is per LLM turn, so the write cost is negligible. Keeps the most
    # recent `max_sessions` sessions by first-seen timestamp.
    def __init__(self, path: str, max_sessions: int = 60):
        self.path = str(path)
        self.max_sessions = int(max_sessions)
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._recorded = 0
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
                self._sessions = {
                    str(k): v for k, v in data["sessions"].items() if isinstance(v, dict)
                }
        except FileNotFoundError:
            self._sessions = {}
        except Exception as e:
            logger.warning("tool usage ledger load failed path=%s err=%s", self.path, e)
            self._sessions = {}

    def _save(self):
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self.path + ".tmp"
            payload = {
                "updated_at": time.time(),
                "sessions": self._sessions,
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning("tool usage ledger save failed path=%s err=%s", self.path, e)

    def record(self, session_id: str, tool_name: str) -> None:
        sid = str(session_id or "")
        name = str(tool_name or "")
        if not sid or not name:
            return
        with self._lock:
            entry = self._sessions.get(sid)
            if entry is None:
                entry = {"ts": time.time(), "tools": []}
                self._sessions[sid] = entry
            if name not in entry["tools"]:
                entry["tools"].append(name)
            self._prune_locked()
            self._save()
            self._recorded += 1

    def _prune_locked(self):
        if len(self._sessions) <= self.max_sessions:
            return
        ordered = sorted(self._sessions.items(), key=lambda kv: kv[1].get("ts", 0.0))
        for sid, _ in ordered[: len(self._sessions) - self.max_sessions]:
            del self._sessions[sid]

    def ordered_sessions(self) -> list[dict]:
        # Oldest first: [{session_id, ts, tools: [...]}]
        with self._lock:
            rows = [
                {
                    "session_id": sid,
                    "ts": float(e.get("ts", 0.0)),
                    "tools": list(e.get("tools", [])),
                }
                for sid, e in self._sessions.items()
            ]
        rows.sort(key=lambda r: r["ts"])
        return rows

    def proposals(
        self,
        loaded_tools: list[str] | None = None,
        unused_sessions: int = 5,
        rare_multiplier: int = 3,
    ) -> list[dict]:
        # Newest-first walk per tool: streak = consecutive recent sessions
        # in which the tool was NOT used (streak stops at the first use).
        # A tool that was never observed at all gets the full session count.
        sessions = self.ordered_sessions()
        observed = int(unused_sessions)
        out = []
        for name in loaded_tools or []:
            if is_protected_tool(name):
                continue
            threshold = observed
            if is_rare_class_tool(name):
                threshold = observed * int(rare_multiplier)
            streak = 0
            last_used = 0.0
            for row in reversed(sessions):
                if name in row["tools"]:
                    last_used = row["ts"]
                    break
                streak += 1
            else:
                streak = len(sessions)
            if sessions and streak >= threshold:
                out.append(
                    {
                        "tool": name,
                        "unused_streak": streak,
                        "threshold": threshold,
                        "last_used_ts": last_used,
                        "rare_class": is_rare_class_tool(name),
                    }
                )
        out.sort(key=lambda p: (-p["unused_streak"], p["tool"]))
        return out

    def report(
        self,
        loaded_tools: list[str] | None = None,
        schema_tokens: dict[str, int] | None = None,
        unused_sessions: int = 5,
    ) -> dict:
        sessions = self.ordered_sessions()
        candidates = self.proposals(loaded_tools=loaded_tools, unused_sessions=unused_sessions)
        schema_tokens = schema_tokens or {}
        for c in candidates:
            c["est_tokens_per_request"] = int(schema_tokens.get(c["tool"], 0))
        saving = sum(int(c.get("est_tokens_per_request", 0)) for c in candidates)
        return {
            "mode": "telemetry_only",
            "sessions_observed": len(sessions),
            "loaded_tools": len(loaded_tools or []),
            "protected_count": sum(1 for t in loaded_tools or [] if is_protected_tool(t)),
            "candidates": candidates,
            "est_tokens_saved_per_request": saving,
            "note": (
                "proposals only; exclusion from session tool sets is a separate "
                "explicit apply step (not part of the telemetry phase)"
            ),
        }
