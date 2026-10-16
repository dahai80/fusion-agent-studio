"""Sub-dispatcher: SubAgentDispatcher (PRD N4, issue #341)."""

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
