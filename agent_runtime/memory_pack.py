"""N15 memory pack (#354): Anthropic-format import/export.

Single fact per file + YAML frontmatter (name/description/type:
user|feedback|project|reference) + MEMORY.md index + [[name]] links —
agent-studio reads and writes the Anthropic memory pack format directly.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_VALID_TYPES = ("user", "feedback", "project", "reference")


def export_pack(engine, out_dir: str, scope: str = "", limit: int = 500) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    entries = engine.list_recent(scope=scope, limit=limit)
    index_lines = ["# Memory Index"]
    written = 0
    for e in entries:
        slug = re.sub(r"[^A-Za-z0-9_-]", "-", (e.id or f"mem-{written}"))[:64]
        fname = f"{slug}.md"
        mtype = getattr(e, "memory_type", "") or "project"
        if mtype not in _VALID_TYPES:
            mtype = "project"
        desc = (e.content or "")[:80].replace("\n", " ")
        front = yaml.safe_dump(
            {"name": slug, "description": desc, "type": mtype},
            allow_unicode=True,
        ).strip()
        body = f"---\n{front}\n---\n\n{e.content}\n"
        (out / fname).write_text(body, encoding="utf-8")
        index_lines.append(f"- [{slug}]({fname}) — {desc}")
        written += 1
    (out / "MEMORY.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    logger.info("memory pack exported dir=%s count=%d scope=%s", out, written, scope)
    return {"dir": str(out), "count": written, "index": str(out / "MEMORY.md")}


def import_pack(engine, in_dir: str, scope: str = "") -> dict:
    d = Path(in_dir)
    if not d.is_dir():
        return {"count": 0, "skipped": 0, "error": f"not a directory: {in_dir}"}
    count = 0
    skipped = 0
    for f in sorted(d.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("pack read failed %s: %s", f.name, e)
            skipped += 1
            continue
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
        if not m:
            skipped += 1
            continue
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        body = text[m.end():].strip()
        if not body:
            skipped += 1
            continue
        mtype = str(meta.get("type", "project"))
        if mtype not in _VALID_TYPES:
            mtype = "project"
        name = str(meta.get("name", "") or f.stem)
        if not _NAME_RE.match(name):
            name = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:64] or f.stem[:64]
        engine.store(
            content=body,
            scope=scope or "imported",
            metadata={"pack": d.name, "pack_name": name},
            memory_type=mtype,
        )
        count += 1
    logger.info("memory pack imported dir=%s count=%d skipped=%d", d, count, skipped)
    return {"count": count, "skipped": skipped}
