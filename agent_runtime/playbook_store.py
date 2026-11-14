# Issue #702 P5: operational playbook (fusion-harness 0.11 playbook module).
# Distilled lessons append through PlaybookOp (mandatory provenance via
# episodes, tombstone delete via op="delete") and persist as a JSONL op log
# under studio_home()/playbook — the log is the source of truth; the in-memory
# harness Playbook replays it on load.
import json
import logging
from pathlib import Path

from fusion_harness.playbook import Playbook, PlaybookOp

from .paths import studio_home

logger = logging.getLogger(__name__)


def playbook_enabled() -> bool:
    import os

    return os.environ.get("FUSION_PLAYBOOK", "1") != "0"


class PlaybookStore:
    def __init__(self, root: Path | str | None = None, name: str = "agent-studio"):
        self.dir = Path(root) if root else studio_home() / "playbook"
        self.ops_path = self.dir / "ops.jsonl"
        self.book = Playbook(name)
        self._entries: dict[str, dict] = {}
        self._replay()

    def _replay(self) -> None:
        if not self.ops_path.is_file():
            return
        count = 0
        try:
            with open(self.ops_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    op = PlaybookOp(
                        op=str(data.get("op", "add")),
                        section=str(data.get("section", "")),
                        content=str(data.get("content", "")),
                        episodes=list(data.get("episodes", []) or []),
                        target_id=data.get("target_id"),
                        op_id=str(data.get("op_id", "")),
                        ts=float(data.get("ts", 0.0) or 0.0),
                    )
                    entry = self.book.apply(op)
                    if op.op == "delete":
                        rec = self._entries.get(str(op.target_id or ""))
                        if rec is not None:
                            rec["deleted"] = True
                    else:
                        self._entries[entry.entry_id] = {
                            "entry_id": entry.entry_id,
                            "section": op.section,
                            "content": op.content,
                            "episodes": list(op.episodes),
                            "revision": entry.revision,
                            "deleted": False,
                        }
                    count += 1
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("playbook replay failed path=%s: %s", self.ops_path, e)
        if count:
            logger.info("playbook replayed ops=%d path=%s", count, self.ops_path)

    def append_lesson(
        self, section: str, content: str, episodes: list[str] | None = None
    ) -> dict:
        # Provenance is mandatory: episodes (trace/eval ids) must be present
        # for add ops so every lesson traces back to observed behavior.
        if not content.strip():
            return {"status": "error", "message": "lesson content must be non-empty"}
        eps = [e for e in (episodes or []) if e]
        if not eps:
            return {"status": "error", "message": "lesson requires episodes (provenance)"}
        op = PlaybookOp(op="add", section=section, content=content, episodes=eps)
        entry = self.book.apply(op)
        self._entries[entry.entry_id] = {
            "entry_id": entry.entry_id,
            "section": section,
            "content": content,
            "episodes": list(eps),
            "revision": entry.revision,
            "deleted": False,
        }
        self._persist(op)
        logger.info(
            "playbook lesson appended section=%s entry=%s episodes=%d",
            section, entry.entry_id, len(eps),
        )
        return {"status": "ok", "entry_id": entry.entry_id, "revision": entry.revision}

    def delete_lesson(self, target_id: str, episodes: list[str] | None = None) -> dict:
        # Harness PlaybookOp validation: delete ops still need a section and
        # at least one provenance episode; fall back to the target's section.
        rec = self._entries.get(target_id)
        section = (rec or {}).get("section") or "playbook"
        eps = [e for e in (episodes or []) if e] or ["delete:" + target_id]
        op = PlaybookOp(op="delete", section=section, content="", episodes=eps, target_id=target_id)
        self.book.apply(op)
        rec = self._entries.get(target_id)
        if rec is not None:
            rec["deleted"] = True
        self._persist(op)
        logger.info("playbook lesson tombstoned target=%s", target_id)
        return {"status": "ok", "target_id": target_id}

    def entries(self) -> list[dict]:
        return [dict(v) for v in self._entries.values() if not v.get("deleted")]

    def _persist(self, op: PlaybookOp) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.ops_path, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "op": op.op,
                            "section": op.section,
                            "content": op.content,
                            "episodes": list(op.episodes),
                            "target_id": op.target_id,
                            "op_id": op.op_id,
                            "ts": op.ts,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError as e:
            logger.warning("playbook persist failed path=%s: %s", self.ops_path, e)
