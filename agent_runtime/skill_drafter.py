# Issue #696 P6: auto-draft SKILL.md candidates from eval gaps and real
# trajectories.
#
# Pipeline (deterministic mining, no model in the loop — Rule 5):
#   1. mining pass over recorded trajectories (trajectory_writer output):
#      failure traces (status != completed / error text) cluster by error
#      signature or tool-call signature; recurring clusters (>= min_count)
#      become draft candidates.
#   2. drafter renders an agentskills.io SKILL.md (frontmatter name +
#      description, body, provenance section citing source traces) — drafts
#      default to untrusted until reviewed (third-party/malicious skills
#      are an exfiltration vector).
#   3. drafts land in a review queue (studio_home()/skill_drafts);
#     skills.review_draft approve validates against the agentskills.io
#     structure and installs into the shared SkillsStore; reject archives.
import hashlib
import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DRAFTS_DIR = "skill_drafts"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def skill_drafts_dir() -> Path:
    from .paths import studio_home

    return studio_home() / _DRAFTS_DIR


class TrajectoryMiner:
    def __init__(self, writer=None, min_count: int = 2, limit: int = 200):
        if writer is None:
            from .trajectory_writer import get_trajectory_writer

            writer = get_trajectory_writer()
        self.writer = writer
        self.min_count = min_count
        self.limit = limit

    def mine(self) -> list[dict]:
        traces = self._load_failure_traces()
        clusters: dict[str, dict] = {}
        for data in traces:
            key = self._signature(data)
            c = clusters.setdefault(
                key,
                {"pattern": key, "count": 0, "trace_ids": [], "samples": []},
            )
            c["count"] += 1
            c["trace_ids"].append(data.get("trace_id", ""))
            err = str(data.get("error", ""))[:200]
            if err:
                c["samples"].append(err)
        candidates = [c for c in clusters.values() if c["count"] >= self.min_count]
        candidates.sort(key=lambda c: -c["count"])
        for c in candidates:
            c["draft_name"] = self._draft_name(c["pattern"])
        logger.info(
            "trajectory mining: traces=%d clusters=%d candidates=%d",
            len(traces),
            len(clusters),
            len(candidates),
        )
        return candidates

    def _load_failure_traces(self) -> list[dict]:
        out = []
        for meta in self.writer.list_trajectories(limit=self.limit):
            if meta.get("status") in ("completed", ""):
                continue
            path = self.writer.output_dir / meta["file"]
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, json.JSONDecodeError, KeyError) as e:
                logger.debug("trajectory read failed: %s", e)
        return out

    @staticmethod
    def _signature(data: dict) -> str:
        # Deterministic cluster key: error head wins; else tool-call
        # sequence shape (names only, args ignored).
        err = str(data.get("error", "")).strip()
        if err:
            return "error:" + re.sub(r"\s+", " ", err[:80])
        names = [
            str(t.get("name", "") or t if isinstance(t, dict) else t)
            for t in (data.get("tool_calls") or [])
        ]
        return "tools:" + ",".join(n for n in names if n) or "no-tools"

    @staticmethod
    def _draft_name(pattern: str) -> str:
        h = hashlib.sha256(pattern.encode("utf-8")).hexdigest()[:8]
        return f"draft-{h}"


def draft_skill_md(candidate: dict) -> str:
    # Render an agentskills.io SKILL.md draft citing provenance.
    name = candidate.get("draft_name", "draft-unknown")
    if not _NAME_RE.match(name):
        name = "draft-" + hashlib.sha256(name.encode()).hexdigest()[:8]
    count = candidate.get("count", 0)
    trace_ids = candidate.get("trace_ids", [])[:10]
    samples = candidate.get("samples", [])[:3]
    pattern = str(candidate.get("pattern", "")).replace("\n", " ")[:160]
    lines = [
        "---",
        f"name: {name}",
        f"description: Auto-drafted from {count} failing trajectories sharing one failure pattern. UNTRUSTED until reviewed.",
        "---",
        "",
        f"# Draft skill {name}",
        "",
        "## Observed failure pattern",
        "",
        f"Recurring pattern across {count} trace(s): {pattern}",
        "",
    ]
    if samples:
        lines += ["## Sample errors", ""]
        for s in samples:
            lines.append(f"- {s}")
        lines.append("")
    lines += [
        "## Guidance (draft — human review required)",
        "",
        "When this failure pattern appears, follow the reviewed practice",
        "recorded here after a maintainer edits this section.",
        "",
        "## Provenance",
        "",
        "- status: draft (untrusted)",
        f"- mined_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"- source traces: {', '.join(t for t in trace_ids if t)}",
    ]
    return "\n".join(lines) + "\n"


class SkillDraftStore:
    def __init__(self, drafts_dir: Path | str | None = None, store=None):
        self.dir = Path(drafts_dir) if drafts_dir else skill_drafts_dir()
        self._store = store

    def _skills_store(self):
        if self._store is None:
            from .skills_store import SkillsStore

            self._store = SkillsStore()
        return self._store

    def save(self, candidate: dict, content: str) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        name = candidate.get("draft_name", "draft-unknown")
        if not _NAME_RE.match(name):
            name = "draft-" + hashlib.sha256(name.encode()).hexdigest()[:8]
        record = {
            "name": name,
            "status": "draft",
            "untrusted": True,
            "created_at": time.time(),
            "provenance": {
                "trace_ids": candidate.get("trace_ids", []),
                "count": candidate.get("count", 0),
                "pattern": candidate.get("pattern", ""),
            },
            "content": content,
        }
        path = self.dir / f"{name}.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, ensure_ascii=False)
        logger.info("skill draft saved name=%s path=%s", name, path)
        return record

    def list_drafts(self, status: str = "") -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("draft-*.json")):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("draft read failed %s: %s", p, e)
                continue
            if status and rec.get("status") != status:
                continue
            out.append(
                {
                    "name": rec.get("name", ""),
                    "status": rec.get("status", ""),
                    "untrusted": rec.get("untrusted", True),
                    "count": (rec.get("provenance") or {}).get("count", 0),
                    "trace_ids": (rec.get("provenance") or {}).get("trace_ids", []),
                    "created_at": rec.get("created_at", 0),
                }
            )
        return out

    def get(self, name: str) -> dict | None:
        if not _NAME_RE.match(name):
            return None
        p = self.dir / f"{name}.json"
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def review(self, name: str, action: str, reviewer: str = "") -> dict:
        rec = self.get(name)
        if rec is None:
            return {"status": "not_found", "name": name}
        if action == "reject":
            rec["status"] = "rejected"
            rec["reviewed_by"] = reviewer
            rec["untrusted"] = True
            self._write(rec)
            logger.info("skill draft rejected name=%s reviewer=%s", name, reviewer)
            return {"status": "rejected", "name": name}
        if action != "approve":
            return {"status": "error", "message": "action must be approve or reject"}
        # Validate against the agentskills.io structure before install.
        from .skills_store import parse_skill_md

        try:
            meta, body = parse_skill_md(rec.get("content", ""), name)
        except (ValueError, OSError) as e:
            rec["status"] = "invalid"
            self._write(rec)
            return {"status": "error", "message": f"draft failed structure validation: {e}"}
        if not meta.name:
            return {"status": "error", "message": "draft missing skill name"}
        installed = self._skills_store().install_skill(name, rec.get("content", ""))
        rec["status"] = "published"
        rec["reviewed_by"] = reviewer
        rec["untrusted"] = False
        rec["installed_path"] = installed.get("path", "")
        self._write(rec)
        logger.info(
            "skill draft published name=%s reviewer=%s path=%s",
            name,
            reviewer,
            installed.get("path", ""),
        )
        return {"status": "published", "name": name, "path": installed.get("path", "")}

    def _write(self, rec: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / f"{rec['name']}.json"
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2, ensure_ascii=False)


def mine_and_draft(min_count: int = 2, limit: int = 200) -> list[dict]:
    store = SkillDraftStore()
    miner = TrajectoryMiner(min_count=min_count, limit=limit)
    saved = []
    for candidate in miner.mine():
        content = draft_skill_md(candidate)
        saved.append(store.save(candidate, content))
    logger.info("mine_and_draft: %d draft(s) queued for review", len(saved))
    return saved
