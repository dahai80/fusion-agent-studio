"""Subagent spawn tool (issue #341) — lets a parent LLM delegate subtasks.

Wired via a runner_provider callable to avoid the agent_runtime <-> tools
import cycle. Optionally a spec_provider resolves named SubAgentSpec
templates registered on the runtime's SubAgentRegistry.
"""

from __future__ import annotations

import json
import logging

from .base import BaseTool

logger = logging.getLogger(__name__)


class SubAgentTool(BaseTool):
    name = "subagent_spawn"
    description = (
        "Spawn an isolated subagent to complete a subtask and return its final "
        "report text. 支持通过已注册模板复用命名专家 agent。"
    )
    readonly = False
    parameters = {
        "name": {
            "type": "string",
            "description": "Subagent name (or a registered template name)",
        },
        "task": {
            "type": "string",
            "description": "Task instruction for the subagent",
        },
        "model": {
            "type": "string",
            "description": "Optional model name to route the sub run to a specific model",
        },
        "tools": {
            "type": "array",
            "description": "Tool name allowlist; empty = report-only reasoning (no tools)",
        },
        "max_turns": {
            "type": "integer",
            "description": "Max LLM calls for the sub run (default 8)",
        },
        "fork": {
            "type": "boolean",
            "description": "Inherit parent conversation history (last 20 messages)",
        },
        "definition": {
            "type": "string",
            "description": "Named agent definition (markdown/frontmatter) to spawn from",
        },
        "background": {
            "type": "boolean",
            "description": "Return immediately with an agent_id instead of blocking for the report",
        },
    }

    def __init__(self, runner_provider, spec_provider=None, definition_provider=None):
        super().__init__()
        self.runner_provider = runner_provider
        self.spec_provider = spec_provider
        # #421: resolves a named AgentDefinition (file-based or programmatic).
        self.definition_provider = definition_provider

    async def execute(self, **kwargs) -> str:
        from agent_runtime.subagent import SubAgentSpec

        runner = None
        try:
            runner = self.runner_provider() if self.runner_provider else None
        except Exception as e:
            logger.warning("subagent_spawn runner_provider failed: %s", e)
            runner = None
        if runner is None:
            logger.warning("subagent_spawn called but runner not wired")
            return "Error: subagent runner not wired"
        try:
            name = str(kwargs.get("name", "") or "")
            task = str(kwargs.get("task", "") or "")
            if not name or not task:
                return "Error: name and task are required"
            spec = None
            definition_name = str(kwargs.get("definition", "") or "")
            if definition_name and self.definition_provider is not None:
                try:
                    spec = self.definition_provider(definition_name).to_spec(task=task)
                except Exception as e:
                    logger.warning("subagent_spawn definition_provider failed: %s", e)
                    spec = None
            if spec is None and self.spec_provider is not None:
                try:
                    spec = self.spec_provider(name)
                except Exception as e:
                    logger.warning("subagent_spawn spec_provider failed: %s", e)
                    spec = None
            if spec is None:
                spec = SubAgentSpec(name=name, task=task)
            else:
                spec = SubAgentSpec.from_dict({**spec.to_dict(), "name": name, "task": task})
            if kwargs.get("model"):
                spec.model = str(kwargs["model"])
            tools_arg = kwargs.get("tools")
            if isinstance(tools_arg, list):
                spec.tools = [str(t) for t in tools_arg]
            if kwargs.get("max_turns"):
                try:
                    spec.max_turns = int(kwargs["max_turns"])
                except (TypeError, ValueError):
                    pass
            if kwargs.get("fork") is not None:
                spec.fork = bool(kwargs["fork"])
            if kwargs.get("background") is not None:
                spec.background = bool(kwargs["background"])
            logger.info(
                "subagent_spawn executing name=%s tools=%d fork=%s model=%s background=%s",
                name,
                len(spec.tools),
                spec.fork,
                spec.model,
                spec.background,
            )
            if spec.background:
                launched = runner.spawn(spec)
                return json.dumps(launched, ensure_ascii=False)
            report = await runner.run(spec, parent_ctx=None)
            return json.dumps(report.to_dict(), ensure_ascii=False)
        except Exception as e:
            logger.error("subagent_spawn failed: %s", e)
            return f"Error: {e}"
