from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_SKILLS_DIR = str(Path.home() / ".fusion-agent-studio" / "skills")
PROJECT_SKILLS_DIRS = (".fusion-agent-studio/skills", ".claude/skills")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\s*\}\}|\{\s*([A-Za-z0-9_-]+)\s*\}")


@dataclass
class SkillMeta:
    name: str
    description: str = ""
    path: str = ""
    version: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    command: str = ""
    output_template: str = ""
    parameters: dict = field(default_factory=dict)
    source: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "version": self.version,
            "allowed_tools": list(self.allowed_tools),
            "command": self.command or f"/{self.name}",
            "output_template": self.output_template,
            "parameters": self.parameters,
            "source": self.source,
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
            at = data.get("allowed-tools") or data.get("allowed_tools") or data.get("tools") or []
            if isinstance(at, str):
                at = [t.strip() for t in at.split(",") if t.strip()]
            meta.allowed_tools = [str(t) for t in at]
            command = data.get("command") or data.get("slash") or ""
            meta.command = str(command) if command else ""
            if meta.command and not meta.command.startswith("/"):
                meta.command = f"/{meta.command}"
            template = data.get("output-template") or data.get("output_template") or ""
            meta.output_template = str(template)
            params = data.get("parameters") or data.get("arguments") or {}
            meta.parameters = params if isinstance(params, dict) else {}
            known = {
                "name",
                "description",
                "version",
                "allowed-tools",
                "allowed_tools",
                "tools",
                "command",
                "slash",
                "output-template",
                "output_template",
                "parameters",
                "arguments",
            }
            meta.extra = {k: v for k, v in data.items() if k not in known}
    if not meta.name:
        meta.name = fallback_name
    return meta, body.strip()


def _schema_properties(meta: SkillMeta) -> dict:
    props = (meta.parameters or {}).get("properties")
    return props if isinstance(props, dict) else {}


def _schema_required(meta: SkillMeta) -> list[str]:
    schema = meta.parameters or {}
    required = schema.get("required") or []
    if isinstance(required, str):
        required = [required]
    out = [str(r) for r in required]
    for key, spec in _schema_properties(meta).items():
        if isinstance(spec, dict) and spec.get("required") and key not in out:
            out.append(key)
    return out


def validate_skill_params(meta: SkillMeta, params: dict) -> tuple[dict, list[str]]:
    props = _schema_properties(meta)
    cleaned: dict = {}
    for key, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        default = spec.get("default")
        value = params.get(key, default)
        if value is None or value == "":
            continue
        kind = str(spec.get("type", "") or "")
        if kind == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return cleaned, [f"{key} must be an integer"]
        elif kind == "number":
            try:
                value = float(value)
            except (TypeError, ValueError):
                return cleaned, [f"{key} must be a number"]
        elif kind == "boolean":
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            else:
                value = bool(value)
        elif kind == "array" and isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        enum = spec.get("enum")
        if isinstance(enum, list) and enum and value not in enum:
            return cleaned, [f"{key} must be one of: {', '.join(str(e) for e in enum)}"]
        cleaned[key] = value
    errors = [f"{key} is required" for key in _schema_required(meta) if not cleaned.get(key)]
    return cleaned, errors


def render_skill_prompt(body: str, params: dict, output_template: str = "") -> str:
    def _sub(match: re.Match) -> str:
        key = match.group(1) or match.group(2)
        if key not in params:
            return match.group(0)
        return str(params[key])

    rendered = _PLACEHOLDER_RE.sub(_sub, body)
    if "$ARGUMENTS" in rendered:
        rendered = rendered.replace("$ARGUMENTS", " ".join(str(v) for v in params.values()))
    if output_template.strip():
        rendered = f"{rendered.rstrip()}\n\n## Output format\n\n{output_template.strip()}"
    return rendered


class SkillsStore:
    def __init__(self, skills_dir: str = DEFAULT_SKILLS_DIR, project_dir: str = ""):
        self.skills_dir = Path(skills_dir)
        self.project_dir = Path(project_dir) if project_dir else None
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "SkillsStore init user_dir=%s project_dir=%s",
            self.skills_dir,
            self.project_dir or "-",
        )

    @classmethod
    def with_project_dirs(cls, skills_dir: str = DEFAULT_SKILLS_DIR, project_root: str = "") -> SkillsStore:
        root = Path(project_root or Path.cwd())
        project_dir = next(
            (str(root / rel) for rel in PROJECT_SKILLS_DIRS if (root / rel).is_dir()),
            "",
        )
        return cls(skills_dir=skills_dir, project_dir=project_dir)

    def search_dirs(self) -> list[Path]:
        dirs = [self.skills_dir]
        if self.project_dir is not None:
            dirs.append(self.project_dir)
        return dirs

    def _skill_path(self, name: str) -> Path:
        if not re.match(r"^[A-Za-z0-9_-]+$", name):
            raise ValueError(f"invalid skill name: {name!r}")
        return self.skills_dir / name / "SKILL.md"

    def _resolve(self, name: str) -> Path | None:
        if not re.match(r"^[A-Za-z0-9_-]+$", name):
            return None
        for base in reversed(self.search_dirs()):
            candidate = base / name / "SKILL.md"
            if candidate.exists():
                return candidate
        return None

    # Progressive disclosure: list returns metadata only, never bodies.
    def list_skills(self) -> list[dict]:
        merged: dict[str, dict] = {}
        for base in self.search_dirs():
            if not base.exists():
                continue
            for d in sorted(base.iterdir()):
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
                meta.source = base.name if base != self.skills_dir else "user"
                merged[meta.name] = meta.to_dict()
        return [merged[k] for k in sorted(merged)]

    def has_skill(self, name: str) -> bool:
        return self._resolve(name) is not None

    def load_skill(self, name: str) -> dict | None:
        # On-demand body load (progressive disclosure step 2).
        p = self._resolve(name)
        if p is None:
            return None
        try:
            meta, body = parse_skill_md(p.read_text(encoding="utf-8"), name)
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("skill load failed %s: %s", name, e)
            return None
        meta.path = str(p)
        meta.source = "user" if p.parent.parent == self.skills_dir else p.parent.parent.name
        return {"meta": meta.to_dict(), "body": body}

    def commands(self) -> list[dict]:
        out = []
        for row in self.list_skills():
            out.append(
                {
                    "command": row.get("command") or f"/{row['name']}",
                    "name": row["name"],
                    "description": row.get("description", ""),
                    "parameters": row.get("parameters") or {},
                    "tools": row.get("allowed_tools") or [],
                    "source": row.get("source", ""),
                }
            )
        return out

    def invoke_skill(self, name: str, params: dict | None = None) -> dict:
        loaded = self.load_skill(name)
        if loaded is None:
            return {"ok": False, "error": f"skill not found: {name}", "errors": []}
        meta_dict = loaded["meta"]
        meta = SkillMeta(
            name=meta_dict["name"],
            parameters=meta_dict.get("parameters") or {},
            output_template=meta_dict.get("output_template", ""),
            allowed_tools=list(meta_dict.get("allowed_tools") or []),
        )
        cleaned, errors = validate_skill_params(meta, params or {})
        if errors:
            logger.warning("skill invoke validation failed name=%s errors=%s", name, errors)
            return {"ok": False, "error": "invalid parameters", "errors": errors, "params": cleaned}
        prompt = render_skill_prompt(loaded["body"], cleaned, meta.output_template)
        logger.info(
            "skill invoked name=%s params=%s prompt_len=%d",
            name,
            sorted(cleaned),
            len(prompt),
        )
        return {
            "ok": True,
            "name": name,
            "params": cleaned,
            "tools": list(meta.allowed_tools),
            "prompt": prompt,
            "errors": [],
        }

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
