"""Sub-dispatcher: SkillsDispatcher (PRD N5, issue #342)."""

from __future__ import annotations

import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class SkillsDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "skills.list": self._handle_list,
            "skills.load": self._handle_load,
            "skills.install": self._handle_install,
            "skills.delete": self._handle_delete,
            "skills.commands": self._handle_commands,
            "skills.invoke": self._handle_invoke,
        }

    def _store(self):
        from ..skills_store import SkillsStore

        cached = getattr(self._daemon, "_skills_store", None)
        if cached is None:
            cached = SkillsStore.with_project_dirs()
            self._daemon._skills_store = cached
        return cached

    async def _handle_commands(self, params: dict) -> dict:
        commands = self._store().commands()
        logger.info("skills.commands count=%d", len(commands))
        return {"status": "ok", "commands": commands}

    async def _handle_invoke(self, params: dict) -> dict:
        name = str(params.get("name", "") or "")
        if not name:
            return {"status": "error", "message": "name required"}
        values = params.get("params") or params.get("arguments") or {}
        if not isinstance(values, dict):
            return {"status": "error", "message": "params must be an object"}
        result = self._store().invoke_skill(name, values)
        if not result.get("ok"):
            return {"status": "error", **{k: v for k, v in result.items() if k != "ok"}}
        logger.info("skills.invoke name=%s prompt_len=%d", name, len(result.get("prompt", "")))
        return {"status": "ok", **{k: v for k, v in result.items() if k != "ok"}}

    def _plane(self):
        getter = getattr(self._daemon, "_get_protocol_plane", None)
        if getter is not None:
            return getter()
        from ..protocol_plane import ProtocolPlane

        plane = getattr(self._daemon, "_protocol_plane", None)
        if plane is None:
            plane = ProtocolPlane()
            self._daemon._protocol_plane = plane
        return plane

    async def _handle_list(self, params: dict) -> dict:
        skills = self._store().list_skills()
        # N17 #390: discovery syncs the skill layer onto the protocol plane.
        for skill in skills:
            self._plane().ensure("skill", skill["name"], origin="skills-store")
        logger.info("skills.list count=%d", len(skills))
        return {"status": "ok", "skills": skills}

    async def _handle_load(self, params: dict) -> dict:
        name = str(params.get("name", ""))
        if not name:
            return {"status": "error", "message": "name required"}
        skill = self._store().load_skill(name)
        if skill is None:
            return {"status": "error", "message": f"skill not found: {name}"}
        logger.info("skills.load name=%s body_len=%d", name, len(skill["body"]))
        return {"status": "ok", "skill": skill}

    async def _handle_install(self, params: dict) -> dict:
        name = str(params.get("name", ""))
        content = params.get("content", "")
        if not name or not content:
            return {"status": "error", "message": "name and content required"}
        try:
            out = self._store().install_skill(name, str(content))
        except (ValueError, OSError) as e:
            return {"status": "error", "message": str(e)}
        self._plane().ensure("skill", out["name"], origin="skills-store")
        logger.info("skills.install name=%s", out["name"])
        return {"status": "ok", **out}

    async def _handle_delete(self, params: dict) -> dict:
        name = str(params.get("name", ""))
        if not name:
            return {"status": "error", "message": "name required"}
        ok = self._store().uninstall_skill(name)
        if not ok:
            return {"status": "error", "message": f"skill not found: {name}"}
        self._plane().unregister("skill", name)
        logger.info("skills.delete name=%s", name)
        return {"status": "ok", "name": name}
