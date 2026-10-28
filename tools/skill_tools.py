"""#498: LLM-facing skill surface — match + invoke SKILL.md skills."""

from __future__ import annotations

import json
import logging
from typing import Callable

from .base import BaseTool

logger = logging.getLogger(__name__)


class SkillSearchTool(BaseTool):
    def __init__(self, store_provider: Callable, engine_provider: Callable | None = None):
        self.name = "skill_search"
        self.description = (
            "Search installed SKILL.md skills by trigger keywords (user text) and/or a "
            "path glob context. Returns matched skill metadata (name, description, "
            "triggers, allowed-tools, command) for the LLM to pick from."
        )
        self.parameters = {
            "query": {
                "type": "string",
                "description": "user text / task description to match against skill trigger keywords",
            },
            "path": {
                "type": "string",
                "description": "working file path to match against skill path globs (optional)",
            },
        }
        self.readonly = True
        self._store_provider = store_provider
        self._engine_provider = engine_provider

    async def execute(self, **kwargs) -> str:
        store = self._store_provider()
        if store is None:
            return json.dumps({"error": "skills store unavailable"})
        query = str(kwargs.get("query", "") or "")
        path = str(kwargs.get("path", "") or "")
        engine = self._engine_provider() if self._engine_provider else None
        from agent_runtime.skill_permissions import register_matched_skill_rules

        matched = register_matched_skill_rules(engine, store, text=query, path=path)
        payload = [
            {
                "name": m.get("name", ""),
                "description": m.get("description", ""),
                "triggers": m.get("triggers") or {},
                "allowed_tools": m.get("allowed_tools") or [],
                "command": m.get("command", ""),
            }
            for m in matched
        ]
        logger.info(
            "skill_search query_len=%d path=%s matched=%s",
            len(query),
            path or "-",
            [p["name"] for p in payload],
        )
        return json.dumps({"matched": payload, "count": len(payload)}, ensure_ascii=False)


class SkillInvokeTool(BaseTool):
    def __init__(self, store_provider: Callable, engine_provider: Callable | None = None):
        self.name = "skill_invoke"
        self.description = (
            "Invoke an installed SKILL.md skill by name with parameters. Returns the "
            "rendered skill prompt (run it as the next LLM instruction). The skill's "
            "allowed-tools restriction is enforced by the permission engine."
        )
        self.parameters = {
            "name": {"type": "string", "description": "skill name (from skill_search)"},
            "params": {
                "type": "object",
                "description": "skill parameters as declared in its frontmatter schema",
                "default": {},
            },
        }
        self.readonly = False
        self._store_provider = store_provider
        self._engine_provider = engine_provider

    async def execute(self, **kwargs) -> str:
        store = self._store_provider()
        if store is None:
            return json.dumps({"error": "skills store unavailable"})
        name = str(kwargs.get("name", "") or "").strip()
        if not name:
            return json.dumps({"error": "name required"})
        values = kwargs.get("params") or {}
        if not isinstance(values, dict):
            return json.dumps({"error": "params must be an object"})
        engine = self._engine_provider() if self._engine_provider else None
        if engine is not None:
            from agent_runtime.skill_permissions import register_skill_permission_rules

            loaded_meta = store.load_skill(name) or {}
            meta = loaded_meta.get("meta") or {}
            # rules key on the skill's declared (frontmatter) name — that is
            # the role a role-scoped execution will carry.
            register_skill_permission_rules(
                engine,
                {
                    "name": meta.get("name") or name,
                    "allowed_tools": meta.get("allowed_tools") or [],
                },
            )
        result = store.invoke_skill(name, values)
        logger.info(
            "skill_invoke name=%s ok=%s prompt_len=%d",
            name,
            bool(result.get("ok")),
            len(result.get("prompt", "")),
        )
        return json.dumps(result, ensure_ascii=False)
