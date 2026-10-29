"""#503: persona-pack marketplace distribution.

A persona pack is a directory holding one persona layer stack:
agent.md (optional) + persona/*.md shards + rules/*.md, plus a
manifest.json carrying per-file sha256 checksums and a pack signature
(sha256 over the canonical JSON of the file list — same hash family as
the v0.8.9 marketplace sha256 work). publish() signs, install()
re-verifies before anything touches the persona root, and a pack whose
rules would remove an existing deny rule is rejected (persona rules
tighten permissions; a pack must never loosen them).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

PERSONA_PACKS_DIRNAME = "persona_packs"
MANIFEST_NAME = "manifest.json"
_LAYER_RE = re.compile(r"^(global|tenant|team)$")
_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_name(name: str) -> str:
    return _SAFE_RE.sub("-", name)[:64]


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signature(files: list[dict]) -> str:
    payload = json.dumps(files, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_manifest(pack_dir: Path | str, layer: str, name: str,
                   version: str = "1.0.0", visibility: str = "public") -> dict:
    d = Path(pack_dir)
    if not _LAYER_RE.match(layer):
        raise ValueError(f"unknown persona layer: {layer}")
    if layer != "global" and not name:
        raise ValueError(f"{layer} pack requires name")
    if not d.is_dir():
        raise ValueError(f"pack dir not found: {d}")
    files: list[dict] = []
    agent_md = d / "agent.md"
    if agent_md.is_file():
        files.append({"path": "agent.md", "sha256": _file_sha256(agent_md)})
    for sub in ("persona", "rules"):
        sub_dir = d / sub
        if sub_dir.is_dir():
            for p in sorted(sub_dir.glob("*.md")):
                files.append({"path": f"{sub}/{p.name}", "sha256": _file_sha256(p)})
    if not files:
        raise ValueError("pack contains no persona content (agent.md/persona/rules)")
    manifest = {
        "name": name,
        "layer": layer,
        "version": version,
        "visibility": visibility,
        "files": files,
    }
    manifest["signature"] = _signature(files)
    (d / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("persona pack manifest built: %s layer=%s files=%d", d, layer, len(files))
    return manifest


def verify_manifest(pack_dir: Path | str) -> dict:
    d = Path(pack_dir)
    mf_path = d / MANIFEST_NAME
    if not mf_path.is_file():
        raise ValueError(f"pack manifest missing: {mf_path}")
    try:
        manifest = json.loads(mf_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"pack manifest unreadable: {e}") from None
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("pack manifest has no file list")
    if not _LAYER_RE.match(str(manifest.get("layer", ""))):
        raise ValueError(f"unknown persona layer: {manifest.get('layer')!r}")
    if manifest.get("signature") != _signature(files):
        raise ValueError("pack signature mismatch — manifest tampered")
    for f in files:
        p = d / str(f.get("path", ""))
        if not p.is_file():
            raise ValueError(f"pack file missing: {f.get('path')!r}")
        if _file_sha256(p) != f.get("sha256"):
            raise ValueError(f"pack file hash mismatch: {f.get('path')!r}")
    return manifest


def _rule_dir_rules(rules_dir: Path) -> list[str]:
    # same contract as persona.load_layer: one rule per file, first
    # non-empty line wins
    rules: list[str] = []
    if not rules_dir.is_dir():
        return rules
    for p in sorted(rules_dir.glob("*.md")):
        try:
            body = p.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.warning("persona rule file read failed path=%s: %s", p, e)
            continue
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        if first:
            rules.append(first)
    return rules


def denies_removed(existing_rules: list[str], pack_rules: list[str]) -> list[str]:
    deny_prefix = ("deny:", "deny ", "deny\t")
    existing_denies = [r for r in existing_rules if r.lower().startswith(deny_prefix)]
    pack_set = set(pack_rules)
    return [r for r in existing_denies if r not in pack_set]


class PersonaPackManager:
    def __init__(self, store_dir: Path | str | None = None,
                 marketplace=None, persona_root: Path | None = None):
        from .persona import PERSONA_ROOT

        self.store_dir = Path(store_dir) if store_dir else (
            Path.home() / ".fusion-agent-studio" / PERSONA_PACKS_DIRNAME
        )
        self.packs_dir = self.store_dir / PERSONA_PACKS_DIRNAME
        self.index_path = self.store_dir / "persona_packs_index.json"
        self.persona_root = Path(persona_root) if persona_root else PERSONA_ROOT
        self._marketplace = marketplace
        self._index: dict[str, dict] = {}
        self._load_index()

    def _load_index(self) -> None:
        try:
            if self.index_path.is_file():
                self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("persona pack index load failed: %s", e)
            self._index = {}

    def _save_index(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps(self._index, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _audit(self, event: str, pack_id: str, tenant: str, detail: str = "") -> None:
        if self._marketplace is None:
            return
        try:
            self._marketplace._audit(event, pack_id, tenant, detail)
        except Exception as e:
            logger.warning("persona pack audit failed: %s", e)

    def _visible(self, pack: dict, tenant: str) -> bool:
        if pack.get("visibility") != "private":
            return True
        return pack.get("tenant") == tenant

    def publish(self, pack_dir: Path | str, tenant: str = "default") -> str:
        manifest = verify_manifest(pack_dir)
        pack_id = uuid.uuid4().hex[:8]
        dest = self.packs_dir / pack_id
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(pack_dir, dest)
        # re-verify the copy so the stored artifact is exactly what was hashed
        verify_manifest(dest)
        now = time.time()
        self._index[pack_id] = {
            "id": pack_id,
            "name": manifest.get("name", ""),
            "layer": manifest.get("layer", ""),
            "version": manifest.get("version", "1.0.0"),
            "visibility": manifest.get("visibility", "public"),
            "tenant": tenant,
            "signature": manifest.get("signature", ""),
            "file_count": len(manifest.get("files", [])),
            "created_at": now,
        }
        self._save_index()
        self._audit("persona_pack_publish", pack_id, tenant,
                    f"layer={manifest.get('layer')} files={len(manifest.get('files', []))}")
        logger.info("persona pack published: %s (%s)", manifest.get("name"), pack_id)
        return pack_id

    def get(self, pack_id: str, tenant: str = "default") -> dict | None:
        pack = self._index.get(pack_id)
        if pack is None or not self._visible(pack, tenant):
            return None
        return pack

    def list(self, tenant: str = "default") -> list[dict]:
        return [p for p in self._index.values() if self._visible(p, tenant)]

    def _target_dir(self, layer: str, name: str) -> Path:
        if layer == "global":
            return self.persona_root
        sub = "tenants" if layer == "tenant" else "teams"
        return self.persona_root / sub / _safe_name(name)

    def install(self, pack_id: str, tenant: str = "default",
                layer_name: str = "") -> dict:
        pack = self._index.get(pack_id)
        if pack is None or not self._visible(pack, tenant):
            self._audit("persona_pack_install_refused", pack_id, tenant, "not visible")
            return {"status": "error", "message": f"pack not found (or not visible): {pack_id}"}
        pack_dir = self.packs_dir / pack_id
        try:
            manifest = verify_manifest(pack_dir)
        except ValueError as e:
            self._audit("persona_pack_install_refused", pack_id, tenant, str(e))
            return {"status": "error", "message": str(e)}
        layer = manifest["layer"]
        # #503: tenant packs install into the INSTALLING tenant's scope;
        # team packs target the team named in the manifest (or layer_name).
        if layer == "tenant":
            name = layer_name or tenant
        else:
            name = layer_name or manifest.get("name", "")
        if layer != "global" and not name:
            return {"status": "error", "message": f"{layer} pack install requires layer_name"}
        target = self._target_dir(layer, name)
        target.mkdir(parents=True, exist_ok=True)
        existing_rules = _rule_dir_rules(target / "rules")
        pack_rules = _rule_dir_rules(pack_dir / "rules")
        loosened = denies_removed(existing_rules, pack_rules)
        if loosened:
            msg = (
                f"install refused: pack removes existing deny rule(s): "
                f"{'; '.join(loosened)}"
            )
            self._audit("persona_pack_install_refused", pack_id, tenant,
                        f"deny loosening: {len(loosened)}")
            logger.error("persona pack %s install refused for %s: %s", pack_id, tenant, msg)
            return {"status": "error", "message": msg}
        for f in manifest["files"]:
            rel = str(f["path"])
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pack_dir / rel, dest)
        self._audit("persona_pack_install", pack_id, tenant,
                    f"layer={layer} name={name or '-'}")
        logger.info("persona pack %s installed into %s", pack_id, target)
        return {"status": "ok", "layer": layer, "name": name, "path": str(target)}
