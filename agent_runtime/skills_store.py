from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_SKILLS_DIR = str(Path.home() / ".fusion-agent-studio" / "skills")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class SkillMeta:
    name: str
    description: str = ""
    path: str = ""
    version: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "version": self.version,
            "allowed_tools": list(self.allowed_tools),
            "extra": self.extra,
        }


def parse_skill_md(content: str, fallback_name: str = "") -> tuple[SkillMeta, str]:
    # Anthropic SKILL.md format: optional YAML frontmatter + markdown body.
    meta = SkillMeta(name=fallback_name)
    body = content
    m = _FRONTMATTER_RE.match(content)
    if m:
        body = content[m.end():]
        try:
            data = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            logger.warning("skill frontmatter parse failed: %s", e)
            data = {}
        if isinstance(data, dict):
            meta.name = str(data.get("name", fallback_name))
            meta.description = str(data.get("description", ""))
            meta.version = str(data.get("version", ""))
            at = data.get("allowed-tools") or data.get("allowed_tools") or []
            if isinstance(at, str):
                at = [t.strip() for t in at.split(",") if t.strip()]
            meta.allowed_tools = [str(t) for t in at]
            known = {"name", "description", "version", "allowed-tools", "allowed_tools"}
            meta.extra = {k: v for k, v in data.items() if k not in known}
    if not meta.name:
        meta.name = fallback_name
    return meta, body.strip()


class SkillsStore:
    def __init__(self, skills_dir: str = DEFAULT_SKILLS_DIR):
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        logger.info("SkillsStore init dir=%s", self.skills_dir)

    def _skill_path(self, name: str) -> Path:
        if not re.match(r"^[A-Za-z0-9_-]+$", name):
            raise ValueError(f"invalid skill name: {name!r}")
        return self.skills_dir / name / "SKILL.md"

    # Progressive disclosure: list returns metadata only, never bodies.
    def list_skills(self) -> list[dict]:
        out = []
        if not self.skills_dir.exists():
            return out
        for d in sorted(self.skills_dir.iterdir()):
            if not d.is_dir():
                continue
            skill_md = d / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                meta, _ = parse_skill_md(skill_md.read_text(encoding="utf-8"), d.name)
            except (OSError, UnicodeDecodeError) as e:
                logger.warning("skill read failed %s: %s", d.name, e)
                continue
            meta.path = str(skill_md)
            if meta.name == d.name or not meta.name:
                meta.name = d.name
            out.append(meta.to_dict())
        return out

    def has_skill(self, name: str) -> bool:
        try:
            return self._skill_path(name).exists()
        except ValueError:
            return False

    def load_skill(self, name: str) -> dict | None:
        # On-demand body load (progressive disclosure step 2).
        try:
            p = self._skill_path(name)
        except ValueError as e:
            logger.warning("load_skill rejected: %s", e)
            return None
        if not p.exists():
            return None
        try:
            meta, body = parse_skill_md(p.read_text(encoding="utf-8"), name)
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("skill load failed %s: %s", name, e)
            return None
        meta.path = str(p)
        return {"meta": meta.to_dict(), "body": body}

    def install_skill(self, name: str, content: str) -> dict:
        # Validate caller-supplied name first — frontmatter name never
        # overrides the directory (path-traversal safety).
        p = self._skill_path(name)
        meta, body = parse_skill_md(content, name)
        if meta.name and meta.name != name:
            logger.info(
                "install_skill frontmatter name %r ignored, dir name %r wins",
                meta.name, name,
            )
            meta.name = name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        logger.info("skill installed name=%s path=%s", name, p)
        return {"name": name, "path": str(p)}

    def uninstall_skill(self, name: str) -> bool:
        try:
            d = self._skill_path(name).parent
        except ValueError as e:
            logger.warning("uninstall_skill rejected: %s", e)
            return False
        if not d.exists():
            return False
        shutil.rmtree(d)
        logger.info("skill uninstalled name=%s", name)
        return True
