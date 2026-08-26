"""Dynamic tools mixin — extracted from AgentRuntime (audit 0826 P2-4).

Holds dynamic tool register/unregister + schema + arg validation. Method
bodies are verbatim moves; AgentRuntime inherits this mixin.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ._runtime_helpers import logger

if TYPE_CHECKING:
    from tools.base import BaseTool


class _DynamicToolsMixin:
    def _validate_tool_args(self, tool: "BaseTool", args: dict) -> dict:
        schema = tool.parameters
        if not schema:
            return args
        validated = {}
        for key, value in args.items():
            if key not in schema:
                logger.warning("Tool '%s': unexpected arg '%s' dropped", tool.name, key)
                continue
            prop = schema[key]
            expected_type = prop.get("type", "")
            if expected_type == "string" and not isinstance(value, str):
                validated[key] = str(value)
                logger.warning("Tool '%s': coerced arg '%s' to string", tool.name, key)
            elif expected_type == "number" and not isinstance(value, (int, float)):
                try:
                    validated[key] = float(value)
                except (ValueError, TypeError):
                    validated[key] = value
                    logger.warning("Tool '%s': could not coerce arg '%s' to number", tool.name, key)
            elif expected_type == "integer" and not isinstance(value, int):
                try:
                    validated[key] = int(value)
                except (ValueError, TypeError):
                    validated[key] = value
                    logger.warning(
                        "Tool '%s': could not coerce arg '%s' to integer",
                        tool.name,
                        key,
                    )
            elif expected_type == "boolean" and not isinstance(value, bool):
                validated[key] = bool(value)
                logger.warning("Tool '%s': coerced arg '%s' to boolean", tool.name, key)
            else:
                validated[key] = value
        for req_key in (
            tool.openai_schema().get("function", {}).get("parameters", {}).get("required", [])
        ):
            if req_key not in validated:
                logger.warning("Tool '%s': missing required arg '%s'", tool.name, req_key)
        return validated

    @staticmethod
    def _dynamic_tool_schemas() -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "register_tool",
                    "description": "Register a new tool dynamically during execution. Creates a tool that can be used in subsequent steps.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Unique name for the tool",
                            },
                            "type": {
                                "type": "string",
                                "description": "Tool type (all types use safe subprocess execution)",
                                "default": "custom",
                            },
                            "description": {
                                "type": "string",
                                "description": "What this tool does",
                            },
                            "parameters": {
                                "type": "object",
                                "description": "OpenAI-style parameter definitions",
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "unregister_tool",
                    "description": "Remove a tool from the registry. It will no longer be available for subsequent steps.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Name of the tool to remove",
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
        ]

    _SAFE_TOOL_NAME_RE = __import__("re").compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

    def _dynamic_register_tool(self, args: dict) -> str:
        if not self.tools:
            return "Error: No tool registry available"
        tool_name = args.get("name", "")
        tool_type = args.get("type", "terminal")
        tool_description = args.get("description", "")
        tool_params = args.get("parameters", {})

        if not tool_name:
            return "Error: 'name' parameter required for register_tool"

        if not self._SAFE_TOOL_NAME_RE.match(tool_name):
            return f"Error: invalid tool name '{tool_name}' — must match [a-zA-Z_][a-zA-Z0-9_]*"

        if self.tools.has(tool_name):
            return f"Tool '{tool_name}' already registered"

        from types import new_class

        from tools.base import BaseTool

        param_dict = {}
        if isinstance(tool_params, dict):
            for pk, pv in tool_params.items():
                if isinstance(pv, dict):
                    param_dict[pk] = pv
                elif isinstance(pv, str):
                    param_dict[pk] = {"type": "string", "description": pv}

        safe_name = f"Dynamic_{self._SAFE_TOOL_NAME_RE.match(tool_name).group()}"
        dyn_cls = new_class(safe_name, (BaseTool,), {})
        dyn_cls.name = tool_name
        dyn_cls.description = tool_description or f"Dynamic tool: {tool_name}"
        dyn_cls.parameters = param_dict

        async def _dyn_execute(self_inner, **kwargs) -> str:
            cmd = kwargs.get("command", kwargs.get("url", kwargs.get("query", "")))
            if cmd:
                import asyncio
                import shlex

                # 审计 P1-3: 动态工具执行补 safety gate. 原仅语法名检查
                # (_SAFE_TOOL_NAME_RE), LLM 注册动态工具后可任意 exec subprocess
                # 绕过 L3 内容检查. 在 cmd 解析后过 evaluate_action, block 即拒.
                if self.safety_gateway:
                    sr = self.safety_gateway.evaluate_action(
                        category="tool_call",
                        content=str(cmd),
                        context=f"dynamic_tool={tool_name}",
                    )
                    if sr.action.value == "block" and not sr.requires_approval:
                        logger.warning(
                            "safety blocked dynamic tool=%s cmd=%s reason=%s",
                            tool_name,
                            str(cmd)[:100],
                            sr.reason,
                        )
                        return f"SafetyGateway blocked dynamic tool call: {sr.reason}"
                try:
                    split_args = shlex.split(str(cmd))
                except ValueError:
                    return f"Error: invalid command: {cmd[:100]}"
                if not split_args:
                    return "Error: empty command"
                proc = await asyncio.create_subprocess_exec(
                    split_args[0],
                    *split_args[1:],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                output = stdout.decode("utf-8", errors="replace")
                if stderr:
                    output += f"\n[STDERR] {stderr.decode('utf-8', errors='replace')}"
                return output.strip() or "Done"
            return "No command provided"

        dyn_cls.execute = _dyn_execute
        new_tool = dyn_cls()

        self.tools.register(new_tool)
        logger.info("Dynamic tool registered: %s (type=%s)", tool_name, tool_type)
        return f"Tool '{tool_name}' registered successfully"

    def _dynamic_unregister_tool(self, args: dict) -> str:
        if not self.tools:
            return "Error: No tool registry available"
        tool_name = args.get("name", "")
        if not tool_name:
            return "Error: 'name' parameter required for unregister_tool"
        if not self.tools.has(tool_name):
            return f"Tool '{tool_name}' not found"
        self.tools.unregister(tool_name)
        logger.info("Dynamic tool unregistered: %s", tool_name)
        return f"Tool '{tool_name}' unregistered successfully"
