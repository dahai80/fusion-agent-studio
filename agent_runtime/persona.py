"""N16 persona agent.md stack (#355): layered persona merge with conflict report.

Layer precedence (low -> high): global < tenant < team < agent < run.
Each layer is an agent.md-style markdown file; merge is section-level —
a higher layer's "## Section" body overrides the lower layer's same-named
section, and every override is reported as an explicit conflict record.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

PERSONA_ROOT = Path.home() / ".fusion-agent-studio" / "persona"

_LAYER_RE = re.compile(r"^(global|tenant|team)$")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


def _parse_meta_block(block: str) -> dict:
    # N16 #390: minimal YAML-ish frontmatter reader. Supports `key: value`,
    # `key: [a, b]`, quoted scalars and block lists (`key:` + `- item`).
    meta: dict = {}
    current_list: str = ""
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and current_list:
            meta.setdefault(current_list, []).append(stripped[2:].strip().strip("'\""))
            continue
        current_list = ""
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if not value:
            current_list = key
            meta[key] = []
        elif value.startswith("[") and value.endswith("]"):
            meta[key] = [
                v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()
            ]
        else:
            meta[key] = value.strip("'\"")
    return meta


def parse_frontmatter(md: str) -> tuple[dict, str]:
    if not md or not md.startswith("---"):
        return {}, md
    m = _FRONTMATTER_RE.match(md)
    if not m:
        return {}, md
    return _parse_meta_block(m.group(1)), md[m.end():]


_ALL_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL | re.MULTILINE
)


def _strip_frontmatter_blocks(md: str) -> str:
    # #482: 移除全部 frontmatter 块 (shard 拼接后非首块也要剥掉).
    # 块内解析不出任何 key 才保留原文 (避免误吃正文里的 --- 分隔线).
    def _repl(m):
        return "" if _parse_meta_block(m.group(1)) else m.group(0)

    return _ALL_FRONTMATTER_RE.sub(_repl, md)


def parse_declarations(md: str) -> dict:
    # N16 #390: rules fragments / memory_scope / skills declarations carried
    # in persona frontmatter and surfaced to the runtime.
    # #482: shard layout — load_layer 拼接 agent.md + persona/*.md 后,
    # 声明可能分布在多个 frontmatter 块, 全部扫描合并.
    rules: list[str] = []
    skills: list[str] = []
    memory_scope = ""
    for m in _ALL_FRONTMATTER_RE.finditer(md or ""):
        meta = _parse_meta_block(m.group(1))
        if not meta:
            continue
        raw_rules = meta.get("rules", [])
        if isinstance(raw_rules, str):
            raw_rules = [raw_rules]
        for r in raw_rules:
            if str(r).strip() and str(r).strip() not in rules:
                rules.append(str(r).strip())
        raw_skills = meta.get("skills", [])
        if isinstance(raw_skills, str):
            raw_skills = [raw_skills]
        for s in raw_skills:
            if str(s).strip() and str(s).strip() not in skills:
                skills.append(str(s).strip())
        if meta.get("memory_scope"):
            memory_scope = str(meta["memory_scope"])
    return {
        "rules": rules,
        "memory_scope": memory_scope,
        "skills": skills,
    }


def _layer_path(layer: str, name: str = "") -> Path:
    if layer == "global":
        return PERSONA_ROOT / "agent.md"
    if layer == "tenant":
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:64]
        return PERSONA_ROOT / "tenants" / safe / "agent.md"
    if layer == "team":
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:64]
        return PERSONA_ROOT / "teams" / safe / "agent.md"
    raise ValueError(f"unknown persona layer: {layer}")


def _layer_dir(layer: str, name: str = "") -> Path:
    if layer == "global":
        return PERSONA_ROOT
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:64]
    return PERSONA_ROOT / ("tenants" if layer == "tenant" else "teams") / safe


def load_layer(layer: str, name: str = "") -> str:
    # #482: shard layout — agent.md 入口 + persona/*.md 碎片 (sorted 确定性
    # 顺序) + rules/*.md (一文件一条 rule, 取首个非空行). 单 agent.md 布局
    # 向后兼容 (无 shard/rule 目录时行为与旧版一致).
    if not _LAYER_RE.match(layer):
        raise ValueError(f"unknown persona layer: {layer}")
    d = _layer_dir(layer, name)
    parts: list[str] = []
    try:
        agent_md = d / "agent.md"
        if agent_md.is_file():
            parts.append(agent_md.read_text(encoding="utf-8"))
        shard_dir = d / "persona"
        if shard_dir.is_dir():
            for p in sorted(shard_dir.glob("*.md")):
                try:
                    parts.append(p.read_text(encoding="utf-8"))
                except OSError as e:
                    logger.warning("persona shard read failed path=%s: %s", p, e)
        rule_dir = d / "rules"
        if rule_dir.is_dir():
            for p in sorted(rule_dir.glob("*.md")):
                try:
                    body = p.read_text(encoding="utf-8").strip()
                except OSError as e:
                    logger.warning("persona rule file read failed path=%s: %s", p, e)
                    continue
                first = next(
                    (ln.strip() for ln in body.splitlines() if ln.strip()), ""
                )
                if first:
                    parts.append(f"---\nrules:\n  - {first}\n---")
    except OSError as e:
        logger.warning("persona layer read failed layer=%s: %s", layer, e)
        return ""
    if not parts:
        return ""
    return "\n\n".join(parts)


def save_layer(layer: str, content: str, name: str = "") -> Path:
    if not _LAYER_RE.match(layer):
        raise ValueError(f"unknown persona layer: {layer}")
    if layer != "global" and not name:
        raise ValueError(f"{layer} layer requires name")
    p = _layer_path(layer, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    logger.info("persona layer saved layer=%s name=%s path=%s", layer, name, p)
    return p


_FILE_PATH_RE = re.compile(r"^(agent\.md|(?:persona|rules)/[A-Za-z0-9_-]+\.md)$")


def _layer_file_path(layer: str, name: str, path: str) -> Path:
    # #504: shard-level file access for the GUI — path allowlist blocks
    # traversal and arbitrary writes under the persona root.
    if not _LAYER_RE.match(layer):
        raise ValueError(f"unknown persona layer: {layer}")
    if layer != "global" and not name:
        raise ValueError(f"{layer} layer requires name")
    if not _FILE_PATH_RE.match(path or ""):
        raise ValueError(f"persona file path not allowed: {path!r}")
    return _layer_dir(layer, name) / path


def list_layer_files(layer: str, name: str = "") -> list[str]:
    d = _layer_dir(layer, name) if _LAYER_RE.match(layer or "") else None
    if d is None or not d.is_dir():
        return []
    files: list[str] = []
    if (d / "agent.md").is_file():
        files.append("agent.md")
    for sub in ("persona", "rules"):
        sub_dir = d / sub
        if sub_dir.is_dir():
            files.extend(f"{sub}/{p.name}" for p in sorted(sub_dir.glob("*.md")))
    return files


def read_layer_file(layer: str, name: str, path: str) -> str:
    p = _layer_file_path(layer, name, path)
    if not p.is_file():
        raise ValueError(f"persona file not found: {path}")
    return p.read_text(encoding="utf-8")


def write_layer_file(layer: str, name: str, path: str, content: str) -> Path:
    p = _layer_file_path(layer, name, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    logger.info("persona file saved layer=%s name=%s path=%s", layer, name, p)
    return p


def parse_sections(md: str) -> dict[str, str]:
    if not md:
        return {}
    # #482: shard 拼接后正文里可能残留非首 frontmatter 块, 全部剥掉再切节.
    md = _strip_frontmatter_blocks(md)
    out: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(md))
    if not matches:
        return {"": md.strip()}
    preamble = md[: matches[0].start()].strip()
    if preamble:
        out[""] = preamble
    for i, m in enumerate(matches):
        header = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        out[header] = md[start:end].strip()
    return out


def merge_persona_layers(layers: list[tuple[str, str]]) -> dict:
    merged: dict[str, str] = {}
    conflicts: list[dict] = []
    applied: list[str] = []
    order: list[str] = []
    rules: list[str] = []
    skills: list[str] = []
    memory_scope = ""
    for layer_name, text in layers:
        if not text or not text.strip():
            continue
        applied.append(layer_name)
        declared = parse_declarations(text)
        for rule in declared["rules"]:
            if rule not in rules:
                rules.append(rule)
        for skill in declared["skills"]:
            if skill not in skills:
                skills.append(skill)
        if declared["memory_scope"]:
            memory_scope = declared["memory_scope"]
        sections = parse_sections(text)
        for header, body in sections.items():
            if header in merged and merged[header].strip() != body.strip():
                conflicts.append(
                    {"section": header or "(preamble)", "winner": layer_name,
                     "loser": "lower layer"}
                )
            elif header in merged:
                conflicts.append(
                    {"section": header or "(preamble)", "winner": layer_name,
                     "loser": "lower layer (identical)"}
                )
            if header not in order:
                order.append(header)
            merged[header] = body
    parts = []
    for header in order:
        if header == "":
            parts.append(merged[header])
        else:
            parts.append(f"## {header}\n\n{merged[header]}")
    persona = "\n\n".join(parts).strip()
    return {
        "persona": persona,
        "layers_applied": applied,
        "conflicts": conflicts,
        "declarations": {
            "rules": rules,
            "memory_scope": memory_scope,
            "skills": skills,
        },
    }


def resolve_persona(
    agent_id: str = "",
    tenant_id: str = "",
    team_id: str = "",
    run_override: str = "",
    fallback: str = "",
    agents_root: Path | None = None,
) -> dict:
    stack: list[tuple[str, str]] = [("global", load_layer("global"))]
    if tenant_id:
        stack.append((f"tenant:{tenant_id}", load_layer("tenant", tenant_id)))
    if team_id:
        stack.append((f"team:{team_id}", load_layer("team", team_id)))
    agent_md = _load_agent_soul(agent_id, agents_root=agents_root)
    if agent_md:
        stack.append((f"agent:{agent_id}", agent_md))
    if run_override:
        stack.append(("run", run_override))
    result = merge_persona_layers(stack)
    if not result["persona"]:
        result["persona"] = fallback
    return result


class PersonaWatcher:
    # N16 #390: mtime-polling hot reload. No third-party dependency; a single
    # daemon thread re-reads the persona tree and reports changed paths.
    def __init__(self, root: Path | None = None, interval: float = 2.0,
                 on_change=None):
        self.root = Path(root) if root is not None else PERSONA_ROOT
        self.interval = max(0.2, float(interval))
        self.on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, tuple[float, int]] = {}

    def _scan(self) -> dict[str, tuple[float, int]]:
        out: dict[str, tuple[float, int]] = {}
        try:
            for path in self.root.rglob("*.md"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                out[str(path)] = (stat.st_mtime, stat.st_size)
        except OSError as e:
            logger.warning("persona watcher scan failed root=%s: %s", self.root, e)
        return out

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._snapshot = self._scan()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="persona-watcher", daemon=True
        )
        self._thread.start()
        logger.info(
            "persona watcher started root=%s interval=%.1fs files=%d",
            self.root, self.interval, len(self._snapshot),
        )

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            current = self._scan()
            changed = [
                path for path, sig in current.items()
                if self._snapshot.get(path) != sig
            ]
            changed += [path for path in self._snapshot if path not in current]
            self._snapshot = current
            if not changed:
                continue
            logger.info("persona hot reload changed=%s", ",".join(changed))
            if self.on_change is not None:
                try:
                    self.on_change(changed)
                except Exception as e:
                    logger.warning("persona watcher callback failed: %s", e)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("persona watcher stopped root=%s", self.root)


def import_agents_md(src_dir: str | Path, agents_root: Path | None = None) -> dict:
    # N16 #390: Anthropic agents/*.md -> .fusion-agent package importer.
    from .agent_package import AGENTS_ROOT, AgentManifest, AgentPackage

    src = Path(src_dir)
    root = Path(agents_root) if agents_root is not None else AGENTS_ROOT
    imported: list[dict] = []
    errors: list[dict] = []
    if not src.is_dir():
        return {"imported": imported, "errors": [{"path": str(src), "error": "not a directory"}]}
    for md_path in sorted(src.glob("*.md")):
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError as e:
            errors.append({"path": str(md_path), "error": str(e)})
            continue
        meta, body = parse_frontmatter(text)
        name = str(meta.get("name") or md_path.stem).strip()
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:64] or md_path.stem
        manifest = AgentManifest(
            name=name,
            description=str(meta.get("description", "") or ""),
            model=str(meta.get("model", "") or ""),
            tools=_as_list(meta.get("tools")),
            tags=["imported", "agents-md"],
        )
        pkg = AgentPackage(root / safe)
        pkg.init(manifest=manifest, soul=body.strip())
        declared = parse_declarations(text)
        imported.append({
            "agent_id": safe,
            "name": name,
            "path": str(pkg.base_path),
            "declarations": declared,
        })
        logger.info("agents.md import name=%s -> agent_id=%s", name, safe)
    return {"imported": imported, "errors": errors}


def export_agents_md(agent_id: str, dest_dir: str | Path,
                     agents_root: Path | None = None) -> dict:
    # N16 #390: .fusion-agent package -> Anthropic agents/*.md (round trip).
    from .agent_package import AGENTS_ROOT, AgentPackage

    root = Path(agents_root) if agents_root is not None else AGENTS_ROOT
    pkg = AgentPackage(root / agent_id)
    if not pkg.exists:
        return {"status": "error", "message": f"agent not found: {agent_id}"}
    manifest = pkg.load_manifest()
    soul = pkg.load_soul() or manifest.system_prompt or ""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{agent_id}.md"
    lines = ["---", f"name: {manifest.name or agent_id}"]
    if manifest.description:
        lines.append(f"description: {manifest.description}")
    if manifest.model:
        lines.append(f"model: {manifest.model}")
    if manifest.tools:
        lines.append(f"tools: [{', '.join(manifest.tools)}]")
    lines.extend(["---", "", soul.strip(), ""])
    out.write_text("\n".join(lines), encoding="utf-8")
    logger.info("agents.md export agent_id=%s -> %s", agent_id, out)
    return {"status": "ok", "path": str(out), "agent_id": agent_id}


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")]
    return [str(v).strip() for v in value if str(v).strip()]


def _load_agent_soul(agent_id: str, agents_root: Path) -> str:
    if not agent_id:
        return ""
    try:
        from .agent_package import AGENTS_ROOT, AgentPackage

        base = agents_root or AGENTS_ROOT
        pkg = AgentPackage(base / agent_id)
        if pkg.exists:
            return pkg.load_soul() or ""
    except Exception as e:
        logger.warning("persona agent soul load failed agent=%s: %s", agent_id, e)
    return ""
