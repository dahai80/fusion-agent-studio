from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_DIR = os.path.join(
    os.path.expanduser("~"), ".fusion-agent-studio", "file_checkpoints"
)

DEFAULT_EXCLUDES = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".vite",
        ".next",
        ".codegraph",
        "file_checkpoints",
    }
)

MAX_FILES = 5000
MAX_BYTES = 50 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FileCheckpointStore:
    def __init__(self, snapshot_dir: str = ""):
        self.snapshot_dir = Path(
            snapshot_dir
            or os.environ.get("FUSION_FILE_CHECKPOINT_DIR")
            or DEFAULT_SNAPSHOT_DIR
        )
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        logger.info("file checkpoint store ready dir=%s", self.snapshot_dir)

    def _index_path(self) -> Path:
        return self.snapshot_dir / "index.json"

    def _read_index(self) -> list[dict]:
        path = self._index_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("file checkpoint index unreadable: %s", e)
            return []
        return data if isinstance(data, list) else []

    def _write_index(self, entries: list[dict]) -> None:
        self._index_path().write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _iter_files(self, root: Path, exclude: set[str]):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in exclude]
            for name in filenames:
                full = Path(dirpath) / name
                try:
                    if full.is_symlink() or not full.is_file():
                        continue
                except OSError:
                    continue
                yield full

    def snapshot(
        self,
        root: str = "",
        run_id: str = "",
        label: str = "",
        exclude: list[str] | None = None,
    ) -> dict:
        base = Path(root or os.getcwd()).resolve()
        snapshot_id = f"fc-{uuid.uuid4().hex[:12]}"
        target = self.snapshot_dir / snapshot_id
        blobs = target / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        excluded = set(DEFAULT_EXCLUDES) | set(exclude or [])
        manifest: dict[str, dict] = {}
        total_bytes = 0
        skipped = 0
        for path in self._iter_files(base, excluded):
            rel = str(path.relative_to(base))
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if len(manifest) >= MAX_FILES or total_bytes + size > MAX_BYTES:
                skipped += 1
                continue
            try:
                digest = _sha256(path)
                blob = blobs / digest
                if not blob.exists():
                    shutil.copy2(path, blob)
            except OSError as e:
                logger.warning("file checkpoint copy failed path=%s: %s", rel, e)
                skipped += 1
                continue
            manifest[rel] = {"sha256": digest, "size": size, "mode": path.stat().st_mode}
            total_bytes += size
        meta = {
            "snapshot_id": snapshot_id,
            "run_id": run_id,
            "label": label,
            "root": str(base),
            "files": manifest,
            "bytes": total_bytes,
            "skipped": skipped,
            "created_at": time.time(),
        }
        (target / "manifest.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        entries = self._read_index()
        entries.append({k: meta[k] for k in ("snapshot_id", "run_id", "label", "root", "bytes", "created_at")}
                       | {"file_count": len(manifest), "skipped": skipped})
        self._write_index(entries)
        logger.info(
            "file checkpoint snapshot id=%s root=%s files=%d bytes=%d skipped=%d",
            snapshot_id,
            base,
            len(manifest),
            total_bytes,
            skipped,
        )
        return {
            "snapshot_id": snapshot_id,
            "run_id": run_id,
            "root": str(base),
            "file_count": len(manifest),
            "bytes": total_bytes,
            "skipped": skipped,
            "created_at": meta["created_at"],
        }

    def _load(self, snapshot_id: str) -> tuple[dict | None, Path | None]:
        if not snapshot_id.startswith("fc-") or not snapshot_id.replace("fc-", "").isalnum():
            logger.warning("file checkpoint id rejected: %r", snapshot_id)
            return None, None
        target = self.snapshot_dir / snapshot_id
        manifest_path = target / "manifest.json"
        if not manifest_path.exists():
            logger.warning("file checkpoint not found id=%s", snapshot_id)
            return None, None
        try:
            meta = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("file checkpoint manifest unreadable id=%s: %s", snapshot_id, e)
            return None, None
        return meta, target

    def list(self, run_id: str = "", limit: int = 50) -> list[dict]:
        entries = self._read_index()
        if run_id:
            entries = [e for e in entries if e.get("run_id") == run_id]
        return entries[-int(limit):]

    def rollback(self, snapshot_id: str, root: str = "") -> dict:
        meta, target = self._load(snapshot_id)
        if meta is None or target is None:
            return {"ok": False, "error": f"snapshot not found: {snapshot_id}"}
        base = Path(root or meta.get("root") or os.getcwd()).resolve()
        manifest: dict[str, dict] = meta.get("files") or {}
        blobs = target / "blobs"
        restored = 0
        created_removed = 0
        errors: list[str] = []
        created_after = float(meta.get("created_at", 0))
        for rel, info in manifest.items():
            blob = blobs / str(info.get("sha256", ""))
            dest = base / rel
            if not blob.exists():
                errors.append(f"missing blob for {rel}")
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(blob, dest)
                try:
                    os.chmod(dest, int(info.get("mode", 0o644)))
                except OSError as e:
                    logger.warning("file checkpoint chmod failed path=%s: %s", rel, e)
                restored += 1
            except OSError as e:
                errors.append(f"{rel}: {e}")
        excluded = set(DEFAULT_EXCLUDES)
        for path in self._iter_files(base, excluded):
            rel = str(path.relative_to(base))
            if rel in manifest:
                continue
            try:
                if path.stat().st_mtime < created_after:
                    continue
                path.unlink()
                created_removed += 1
            except OSError as e:
                errors.append(f"{rel}: {e}")
        logger.info(
            "file checkpoint rollback id=%s root=%s restored=%d removed=%d errors=%d",
            snapshot_id,
            base,
            restored,
            created_removed,
            len(errors),
        )
        return {
            "ok": not errors,
            "snapshot_id": snapshot_id,
            "root": str(base),
            "restored": restored,
            "removed": created_removed,
            "errors": errors,
        }

    def delete(self, snapshot_id: str) -> bool:
        meta, target = self._load(snapshot_id)
        if meta is None or target is None:
            return False
        shutil.rmtree(target, ignore_errors=True)
        self._write_index([e for e in self._read_index() if e.get("snapshot_id") != snapshot_id])
        logger.info("file checkpoint deleted id=%s", snapshot_id)
        return True
