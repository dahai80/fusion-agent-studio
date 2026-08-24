"""Variable manager — cross-node variable passing and state management."""

from __future__ import annotations

import json
import re
from typing import Any


class VariableManager:
    """Manages variables that can be passed between nodes in an agent graph.

    Supports:
    - Set/get variables by name
    - Nested variable access (dot notation: data.items.0.name)
    - Variable interpolation in strings ({{ variable.name }})
    - Type coercion (string, number, boolean, json)
    """

    def __init__(self):
        self._vars: dict[str, Any] = {}

    def set(self, name: str, value: Any, coerce: str = "") -> None:
        """Set a variable, optionally coercing the type."""
        if coerce == "number":
            value = float(value)
        elif coerce == "integer":
            value = int(value)
        elif coerce == "boolean":
            if isinstance(value, str):
                value = value.lower() in ("true", "1", "yes")
            else:
                value = bool(value)
        elif coerce == "json":
            if isinstance(value, str):
                value = json.loads(value)
        self._vars[name] = value

    def get(self, name: str, default: Any = "") -> Any:
        """Get a variable by name, supporting dot notation for nested access."""
        if "." in name:
            return self._get_nested(name, default)
        return self._vars.get(name, default)

    def _get_nested(self, name: str, default: Any = "") -> Any:
        """Access nested variables using dot notation: data.items.0.name"""
        parts = name.split(".")
        current = self._vars
        for part in parts:
            if isinstance(current, dict):
                if part not in current:
                    return default
                current = current[part]
            elif isinstance(current, (list, tuple)):
                try:
                    idx = int(part)
                    current = current[idx]
                except (ValueError, IndexError):
                    return default
            else:
                return default
        return current

    def interpolate(self, template: str) -> str:
        """Replace {{ variable.name }} placeholders with actual values.

        #211: 复杂类型 (dict/list/bool/None) 产合法 JSON 串, 非 Python repr.
        str(dict) = 单引号 repr, 下游 json.loads 崩; json.dumps 双引号合法.
        标量 (str/int/float) 保持 str() 原样, 避免字符串被多余引号包裹.
        """

        def replacer(match):
            var_name = match.group(1).strip()
            value = self.get(var_name, "")
            if isinstance(value, (dict, list, tuple)):
                return json.dumps(value, ensure_ascii=False)
            if value is None:
                return "null"
            if isinstance(value, bool):
                return "true" if value else "false"
            return str(value)

        return re.sub(r"\{\{(.+?)\}\}", replacer, template)

    def delete(self, name: str) -> None:
        """Delete a variable."""
        self._vars.pop(name, None)

    def clear(self) -> None:
        """Clear all variables."""
        self._vars.clear()

    def keys(self) -> list[str]:
        return list(self._vars.keys())

    def to_dict(self) -> dict[str, Any]:
        return dict(self._vars)

    def load_from(self, data: dict) -> None:
        """Load variables from a dictionary."""
        self._vars.update(data)

    @property
    def count(self) -> int:
        return len(self._vars)
