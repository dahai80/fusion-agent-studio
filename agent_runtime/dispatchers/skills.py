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

    async def _admit_skill_invoke(self, name: str, values: dict) -> dict | None:
        # #476: skills.invoke runs arbitrary skill prompt content — must pass
        # the ToolFunnel admission chain (hooks -> permission -> budget),
        # same contract as MCP RPCs (N6 pattern in dispatchers/mcp.py).
        daemon = self._daemon
        get_runtime = getattr(daemon, "_get_runtime", None)
        if not callable(get_runtime):
            logger.warning("skills.invoke %s: daemon has no runtime, funnel skipped", name)
            return None
        runtime = get_runtime()
        funnel = getattr(runtime, "tool_funnel", None)
        if funnel is None:
            logger.warning("skills.invoke %s: runtime has no tool_funnel, funnel skipped", name)
            return None
        from ..context import AgentContext

        outcome = await funnel.admit(
            ctx=AgentContext(),
            tool_name="skill.invoke",
            args={"name": name, "params": values},
            node_label=f"skill:{name}",
        )
        if outcome.needs_approval:
            logger.warning("skill.invoke %s requires approval, RPC path has no approver", name)
            return {
                "status": "error",
                "message": f"skill.invoke requires approval ({outcome.reason})",
            }
        if not outcome.allowed:
            logger.warning(
                "skill.invoke %s blocked gate=%s reason=%s",
                name,
                outcome.blocked_by,
                outcome.reason,
            )
            return {
                "status": "error",
                "message": f"Blocked by {outcome.blocked_by}: {outcome.reason}",
            }
        return None

    def _persona_skill_check(self, params: dict, name: str) -> dict | None:
        # #482: 调用方带 agent_id 时, persona skills 声明即白名单 —
        # 声明了 skills 列表的 agent 只能 invoke 列表内技能.
        agent_id = str(params.get("agent_id", "") or "")
        if not agent_id:
            return None
        try:
            from ..persona import resolve_persona

            result = resolve_persona(
                agent_id=agent_id,
                tenant_id=str(params.get("tenant_id", "") or ""),
                team_id=str(params.get("team_id", "") or ""),
            )
            declared = (result.get("declarations") or {}).get("skills") or []
        except Exception as e:
            logger.warning("skills.invoke persona resolve failed agent=%s: %s", agent_id, e)
            return None
        if declared and name not in declared:
            logger.warning(
                "skills.invoke %s blocked by persona allowlist agent=%s allowed=%s",
                name,
                agent_id,
                declared,
            )
            return {
                "status": "error",
                "message": f"skill not in persona skills allowlist: {name}",
            }
        return None

    async def _handle_invoke(self, params: dict) -> dict:
        name = str(params.get("name", "") or "")
        if not name:
            return {"status": "error", "message": "name required"}
        values = params.get("params") or params.get("arguments") or {}
        if not isinstance(values, dict):
            return {"status": "error", "message": "params must be an object"}
        blocked = self._persona_skill_check(params, name)
        if blocked is not None:
            return blocked
        blocked = await self._admit_skill_invoke(name, values)
        if blocked is not None:
            return blocked
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
