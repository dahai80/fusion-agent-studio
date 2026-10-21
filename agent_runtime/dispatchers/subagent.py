"""Sub-dispatcher: SubAgentDispatcher (PRD N4, issue #341, #421-#424, #434)."""

from __future__ import annotations

import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class SubAgentDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "subagent.run": self._handle_subagent_run,
            "subagent.register": self._handle_subagent_register,
            "subagent.list": self._handle_subagent_list,
            "subagent.spawn": self._handle_subagent_spawn,
            "subagent.wait": self._handle_subagent_wait,
            "subagent.cancel": self._handle_subagent_cancel,
            "subagent.tree": self._handle_subagent_tree,
            "subagent.transcript": self._handle_subagent_transcript,
            "subagent.resume": self._handle_subagent_resume,
            "subagent.definition_list": self._handle_definition_list,
            "subagent.definition_get": self._handle_definition_get,
            "subagent.definition_register": self._handle_definition_register,
            "subagent.definition_reload": self._handle_definition_reload,
            "subagent.toolset": self._handle_toolset,
        }

    def _get_runner(self):
        daemon = self._daemon
        rt = daemon._get_runtime() if hasattr(daemon, "_get_runtime") else None
        if rt is None:
            return None
        runner = getattr(rt, "subagent_runner", None)
        if runner is None:
            from ..subagent import SubAgentRunner

            runner = SubAgentRunner(rt)
            rt.subagent_runner = runner
        return runner

    def _get_registry(self):
        rt = self._daemon._get_runtime() if hasattr(self._daemon, "_get_runtime") else None
        return getattr(rt, "subagent_registry", None)

    def _definitions(self):
        from ..subagent_defs import get_definition_registry

        return get_definition_registry()

    async def _handle_subagent_run(self, params: dict) -> dict:
        runner = self._get_runner()
        if runner is None:
            return {"status": "error", "message": "runtime unavailable"}
        from ..subagent import SubAgentSpec

        try:
            spec = SubAgentSpec.from_dict(params.get("spec") or params)
        except (TypeError, ValueError) as e:
            return {"status": "error", "message": f"invalid spec: {e}"}
        report = await runner.run(spec)
        logger.info(
            "subagent.run name=%s ok=%s turns=%s tokens=%s",
            spec.name, report.ok, report.turns, report.tokens_used,
        )
        return {"status": "ok", "report": report.to_dict()}

    async def _handle_subagent_register(self, params: dict) -> dict:
        reg = self._get_registry()
        if reg is None:
            return {"status": "error", "message": "runtime unavailable"}
        from ..subagent import SubAgentSpec

        try:
            spec = SubAgentSpec.from_dict(params.get("spec") or params)
        except (TypeError, ValueError) as e:
            return {"status": "error", "message": f"invalid spec: {e}"}
        reg.register(spec)
        logger.info("subagent.register name=%s", spec.name)
        return {"status": "ok", "name": spec.name}

    async def _handle_subagent_list(self, params: dict) -> dict:
        reg = self._get_registry()
        if reg is None:
            return {"status": "error", "message": "runtime unavailable"}
        return {"status": "ok", "specs": reg.list()}

    async def _handle_subagent_spawn(self, params: dict) -> dict:
        runner = self._get_runner()
        if runner is None:
            return {"status": "error", "message": "runtime unavailable"}
        from ..subagent import SubAgentSpec

        raw = dict(params.get("spec") or params)
        definition_name = str(raw.pop("definition", "") or "")
        spec = None
        if definition_name:
            definition = self._definitions().get(definition_name)
            if definition is None:
                return {"status": "error", "message": f"definition not found: {definition_name}"}
            spec = definition.to_spec(task=str(raw.get("task", "") or ""))
            spec = SubAgentSpec.from_dict({**spec.to_dict(), **raw, "task": spec.task})
        try:
            spec = spec or SubAgentSpec.from_dict(raw)
        except (TypeError, ValueError) as e:
            return {"status": "error", "message": f"invalid spec: {e}"}
        out = runner.spawn(spec)
        logger.info("subagent.spawn name=%s agent_id=%s", spec.name, out.get("agent_id"))
        return {"status": "ok", **out}

    async def _handle_subagent_wait(self, params: dict) -> dict:
        runner = self._get_runner()
        if runner is None:
            return {"status": "error", "message": "runtime unavailable"}
        agent_id = str(params.get("agent_id", ""))
        if not agent_id:
            return {"status": "error", "message": "agent_id required"}
        report = await runner.task_manager().wait(agent_id, float(params.get("timeout", 0) or 0))
        if report is None:
            return {"status": "error", "message": f"no report for agent_id: {agent_id}"}
        logger.info("subagent.wait agent_id=%s ok=%s", agent_id, report.get("ok"))
        return {"status": "ok", "report": report}

    async def _handle_subagent_cancel(self, params: dict) -> dict:
        runner = self._get_runner()
        if runner is None:
            return {"status": "error", "message": "runtime unavailable"}
        agent_id = str(params.get("agent_id", ""))
        cancelled = runner.task_manager().cancel(agent_id)
        logger.info("subagent.cancel agent_id=%s cancelled=%s", agent_id, cancelled)
        return {"status": "ok", "cancelled": cancelled}

    async def _handle_subagent_tree(self, params: dict) -> dict:
        runner = self._get_runner()
        if runner is None:
            return {"status": "error", "message": "runtime unavailable"}
        tree = runner.task_manager().tree(
            parent_session=str(params.get("parent_session", "")),
            limit=int(params.get("limit", 100) or 100),
        )
        return {"status": "ok", **tree}

    async def _handle_subagent_transcript(self, params: dict) -> dict:
        runner = self._get_runner()
        if runner is None:
            return {"status": "error", "message": "runtime unavailable"}
        agent_id = str(params.get("agent_id", ""))
        record = runner.transcripts().get(agent_id)
        if record is None:
            return {"status": "error", "message": f"transcript not found: {agent_id}"}
        if not params.get("include_messages", True):
            record.pop("messages", None)
        return {"status": "ok", "transcript": record}

    async def _handle_subagent_resume(self, params: dict) -> dict:
        runner = self._get_runner()
        if runner is None:
            return {"status": "error", "message": "runtime unavailable"}
        agent_id = str(params.get("agent_id", ""))
        report = await runner.resume(
            agent_id,
            task=str(params.get("task", "")),
            max_turns=int(params.get("max_turns", 0) or 0),
        )
        if report is None:
            return {"status": "error", "message": f"cannot resume: {agent_id}"}
        logger.info("subagent.resume agent_id=%s ok=%s partial=%s", agent_id, report.ok, report.partial)
        return {"status": "ok", "report": report.to_dict()}

    async def _handle_definition_list(self, params: dict) -> dict:
        registry = self._definitions()
        return {"status": "ok", "definitions": registry.list()}

    async def _handle_definition_get(self, params: dict) -> dict:
        name = str(params.get("name", ""))
        definition = self._definitions().get(name)
        if definition is None:
            return {"status": "error", "message": f"definition not found: {name}"}
        return {"status": "ok", "definition": definition.to_dict()}

    async def _handle_definition_register(self, params: dict) -> dict:
        from ..subagent_defs import AgentDefinition

        payload = dict(params.get("definition") or params)
        payload.pop("status", None)
        try:
            definition = AgentDefinition.from_dict(payload)
            self._definitions().register(definition)
        except (TypeError, ValueError) as e:
            return {"status": "error", "message": f"invalid definition: {e}"}
        logger.info("subagent.definition_register name=%s", definition.name)
        return {"status": "ok", "name": definition.name}

    async def _handle_definition_reload(self, params: dict) -> dict:
        registry = self._definitions()
        registry.clear()
        count = registry.discover(project_dir=str(params.get("project_dir", "")))
        logger.info("subagent.definition_reload count=%d", count)
        return {"status": "ok", "count": count, "definitions": registry.list()}

    async def _handle_toolset(self, params: dict) -> dict:
        runner = self._get_runner()
        if runner is None:
            return {"status": "error", "message": "runtime unavailable"}
        parent_tools = getattr(runner.parent_runtime, "tools", None)
        available = [t["name"] for t in parent_tools.list_tools()] if parent_tools else []
        allowed = [str(t) for t in params.get("tools") or []]
        denied = [str(t) for t in params.get("disallowed_tools") or []]
        mcp_servers = [str(s) for s in params.get("mcp_servers") or []]
        effective: list[str] = []
        for name in allowed or available:
            if not parent_tools or not parent_tools.has(name):
                continue
            tool = parent_tools.get(name)
            if runner._denied_by_patterns(name, tool, denied):
                continue
            if mcp_servers and not runner._server_allowed(tool, mcp_servers):
                continue
            effective.append(name)
        logger.info(
            "subagent.toolset allowed=%d denied=%d effective=%d",
            len(allowed),
            len(denied),
            len(effective),
        )
        return {"status": "ok", "tools": effective, "available": available}
