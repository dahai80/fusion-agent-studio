"""E5: offline reviewer — out-of-band job that replays a run's event stream
and flags permission-rule overreach (LLM-judge assist, deterministic rules
first, both layers fail toward "flag" not silence). Reports land in
AuditLogger (S4: regulator-examinable format) and are returned to callers.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

logger = logging.getLogger(__name__)

_DEFAULT_RULES = [
    {
        "id": "destructive-shell",
        "tool": "terminal",
        "pattern": r"rm\s+-rf|sudo\s|mkfs|dd\s+if=|shutdown|reboot",
        "severity": "high",
    },
    {
        "id": "workspace-escape",
        "tool": "file_write",
        "pattern": r"(\.\./){2,}|/etc/|/System/|~/\.ssh",
        "severity": "high",
    },
    {
        "id": "force-push",
        "tool": "git_push",
        "pattern": r"--force|-f\b",
        "severity": "medium",
    },
]


class OfflineReviewer:
    def __init__(
        self,
        rules: list[dict] | None = None,
        mlx_client=None,
        audit_logger=None,
    ):
        self.rules = rules if rules is not None else _DEFAULT_RULES
        self.mlx_client = mlx_client
        self.audit_logger = audit_logger

    def _deterministic_findings(self, events: list[dict]) -> list[dict]:
        findings = []
        for ev in events:
            if ev.get("type") != "tool_call":
                continue
            tool = str(ev.get("name", ""))
            args = ev.get("args") or {}
            args_text = json.dumps(args, ensure_ascii=False) if args else ""
            for rule in self.rules:
                if rule.get("tool") and rule["tool"] != tool:
                    continue
                pat = rule.get("pattern")
                if not pat:
                    continue
                try:
                    if re.search(pat, args_text) or re.search(pat, str(ev.get("content", ""))):
                        findings.append(
                            {
                                "finding_id": uuid.uuid4().hex[:12],
                                "event_id": ev.get("event_id", ""),
                                "tool": tool,
                                "rule_id": rule.get("id", ""),
                                "severity": rule.get("severity", "medium"),
                                "verdict": "overreach",
                                "layer": "rules",
                                "reason": f"matched rule {rule.get('id', '?')}",
                                "args_preview": args_text[:200],
                            }
                        )
                        break
                except re.error as e:
                    logger.warning("offline reviewer bad rule pattern %s: %s", rule.get("id"), e)
        return findings

    async def _llm_findings(self, events: list[dict], known_event_ids: set[str]) -> list[dict]:
        # Second layer: only review tool calls the rules did not flag. Judge
        # failure marks the call "unreviewed" (fail-visible), never silently ok.
        if self.mlx_client is None:
            return []
        pending = [
            ev
            for ev in events
            if ev.get("type") == "tool_call" and ev.get("event_id") not in known_event_ids
        ]
        if not pending:
            return []
        rules_text = json.dumps(
            [{"id": r.get("id"), "tool": r.get("tool"), "pattern": r.get("pattern")} for r in self.rules],
            ensure_ascii=False,
        )
        findings = []
        for ev in pending:
            prompt = (
                "You are a permission auditor. Rules:\n" + rules_text + "\n"
                "Tool call:\n" + json.dumps({"tool": ev.get("name", ""), "args": ev.get("args") or {}}, ensure_ascii=False) + "\n"
                'Reply with JSON only: {"verdict": "ok"|"overreach", "reason": "..."}'
            )
            try:
                resp = await self.mlx_client.chat(
                    model=getattr(self.mlx_client, "model", "") or "",
                    messages=[{"role": "user", "content": prompt}],
                )
                text = resp.content if hasattr(resp, "content") else str(resp)
                start = text.find("{")
                end = text.rfind("}")
                if start < 0 or end <= start:
                    raise ValueError("no JSON in judge reply")
                parsed = json.loads(text[start : end + 1])
                verdict = str(parsed.get("verdict", "")).lower()
                if verdict not in ("ok", "overreach"):
                    raise ValueError(f"bad verdict {verdict!r}")
                if verdict == "overreach":
                    findings.append(
                        {
                            "finding_id": uuid.uuid4().hex[:12],
                            "event_id": ev.get("event_id", ""),
                            "tool": ev.get("name", ""),
                            "rule_id": "llm_judge",
                            "severity": "medium",
                            "verdict": "overreach",
                            "layer": "llm",
                            "reason": str(parsed.get("reason", ""))[:300],
                            "args_preview": json.dumps(ev.get("args") or {}, ensure_ascii=False)[:200],
                        }
                    )
            except Exception as e:
                logger.warning("llm judge failed event=%s: %s", ev.get("event_id"), e)
                findings.append(
                    {
                        "finding_id": uuid.uuid4().hex[:12],
                        "event_id": ev.get("event_id", ""),
                        "tool": ev.get("name", ""),
                        "rule_id": "llm_judge",
                        "severity": "info",
                        "verdict": "unreviewed",
                        "layer": "llm",
                        "reason": f"judge failed: {e}",
                        "args_preview": "",
                    }
                )
        return findings

    async def review_run(self, run_id: str, events: list[dict]) -> dict:
        started = time.time()
        findings = self._deterministic_findings(events)
        known = {f["event_id"] for f in findings if f.get("event_id")}
        findings.extend(await self._llm_findings(events, known))
        overreach = [f for f in findings if f["verdict"] == "overreach"]
        report = {
            "report_id": uuid.uuid4().hex[:16],
            "run_id": run_id,
            "reviewed_at": time.time(),
            "reviewed_events": len(events),
            "tool_calls": sum(1 for e in events if e.get("type") == "tool_call"),
            "flagged": len(overreach),
            "unreviewed": sum(1 for f in findings if f["verdict"] == "unreviewed"),
            "findings": findings,
            "duration_ms": int((time.time() - started) * 1000),
        }
        logger.info(
            "offline review run=%s flagged=%d findings=%d",
            run_id,
            len(overreach),
            len(findings),
        )
        self._write_audit(report)
        return report

    def _write_audit(self, report: dict) -> None:
        if self.audit_logger is None:
            return
        try:
            for f in report["findings"]:
                self.audit_logger.log_action(
                    actor_id="offline-reviewer",
                    action="offline_review.finding",
                    resource_type="run",
                    resource_id=report["run_id"],
                    details={
                        "report_id": report["report_id"],
                        "verdict": f["verdict"],
                        "rule_id": f["rule_id"],
                        "severity": f["severity"],
                        "tool": f["tool"],
                        "reason": f["reason"],
                    },
                    result="flagged" if f["verdict"] == "overreach" else f["verdict"],
                )
        except Exception as e:
            logger.warning("offline reviewer audit write failed: %s", e)


def default_rules() -> list[dict]:
    return [dict(r) for r in _DEFAULT_RULES]
