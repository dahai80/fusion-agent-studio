# Issue #681: fusion-memory v1.8.0 SKILL.md export consumer.
#
# Pulls POST /v1/memory/export_skills (Bearer, producer-side redact gate
# already applied) and writes each doc into the SkillsStore so the harness
# skill loader picks it up. target=project maps to the project-scoped
# skills dir (.fusion-agent-studio/skills under project_root); the default
# user target lands in the user skills dir.
#
# Env:
#   FUSION_MEMORY_BASE_URL  default http://127.0.0.1:11435
#   FUSION_MEMORY_TOKEN     bearer token for the /v1 boundary
#   FUSION_SKILLS_DIR       override for the user skills dir

import logging
import os
import re
from pathlib import Path

import httpx

from .skills_store import DEFAULT_SKILLS_DIR, PROJECT_SKILLS_DIRS, SkillsStore

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11435"
EXPORT_TIMEOUT_S = 15.0
_MAX_LIMIT = 50
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _resolve_store(target: str, project_root: str) -> tuple[SkillsStore, str]:
    if target == "project":
        root = Path(project_root or os.getcwd())
        skills_dir = root / PROJECT_SKILLS_DIRS[0]
        return SkillsStore(skills_dir=str(skills_dir)), str(skills_dir)
    skills_dir = os.environ.get("FUSION_SKILLS_DIR", "") or DEFAULT_SKILLS_DIR
    return SkillsStore(skills_dir=skills_dir), skills_dir


async def sync_memory_skills(
    limit: int = 20,
    namespace: str = "",
    namespace_key: str = "",
    target: str = "user",
    project_root: str = "",
    base_url: str = "",
    token: str = "",
    transport: "httpx.AsyncBaseTransport | None" = None,
) -> dict:
    limit = max(1, min(int(limit or 20), _MAX_LIMIT))
    base = (base_url or os.environ.get("FUSION_MEMORY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    bearer = token or os.environ.get("FUSION_MEMORY_TOKEN", "")
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    payload: dict = {"limit": limit}
    if namespace:
        payload["namespace"] = namespace
    if namespace_key:
        payload["namespace_key"] = namespace_key
    try:
        async with httpx.AsyncClient(timeout=EXPORT_TIMEOUT_S, transport=transport) as client:
            resp = await client.post(
                f"{base}/v1/memory/export_skills", json=payload, headers=headers
            )
            resp.raise_for_status()
            skills = resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning("memory skill sync http %s: %s", e.response.status_code, e)
        return {"status": "error", "message": f"export_skills http {e.response.status_code}"}
    except Exception as e:
        logger.warning("memory skill sync failed: %s", e)
        return {"status": "error", "message": f"export_skills unreachable: {e}"}
    if not isinstance(skills, list):
        return {"status": "error", "message": "export_skills returned non-list payload"}

    store, skills_dir = _resolve_store(target, project_root)
    synced: list[dict] = []
    skipped: list[dict] = []
    for item in skills:
        if not isinstance(item, dict):
            skipped.append({"reason": "non-object entry"})
            continue
        name = str(item.get("name", "") or "")
        markdown = str(item.get("markdown", "") or "")
        if not name or not _NAME_RE.match(name):
            skipped.append({"id": item.get("id", ""), "reason": f"invalid skill name: {name!r}"})
            continue
        if not markdown.strip():
            skipped.append({"id": item.get("id", ""), "name": name, "reason": "empty markdown"})
            continue
        try:
            out = store.install_skill(name, markdown)
        except (ValueError, OSError) as e:
            logger.warning("memory skill sync write failed name=%s: %s", name, e)
            skipped.append({"id": item.get("id", ""), "name": name, "reason": str(e)})
            continue
        synced.append(
            {
                "name": name,
                "id": item.get("id", ""),
                "confidence": item.get("confidence"),
                "path": out["path"],
            }
        )
        logger.info("memory skill synced name=%s id=%s", name, item.get("id", ""))
    logger.info(
        "memory skill sync done base=%s target=%s synced=%d skipped=%d",
        base,
        target,
        len(synced),
        len(skipped),
    )
    return {
        "status": "ok",
        "count": len(synced),
        "synced": synced,
        "skipped": skipped,
        "skills_dir": skills_dir,
    }
