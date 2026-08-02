"""Structured output — enforce JSON Schema output from LLM nodes."""

from __future__ import annotations

import json
from typing import Any


class JsonSchemaValidator:
    """Validates and enforces structured JSON output from LLM nodes.

    Supports:
    - JSON Schema validation (draft-07 subset)
    - Type coercion (string, number, integer, boolean, array, object)
    - Required field enforcement
    - Default value injection
    """

    def __init__(self, schema: dict | None = None):
        self.schema = schema or {}

    def validate(self, data: dict) -> list[str]:
        """Validate data against the schema. Returns list of errors."""
        errors: list[str] = []
        if not self.schema:
            return errors
        properties = self.schema.get("properties", {})
        required = self.schema.get("required", [])

        # Check required fields
        for field in required:
            if field not in data:
                errors.append(f"Missing required field: '{field}'")

        # Validate types
        for field, value in data.items():
            if field in properties:
                expected_type = properties[field].get("type", "")
                if expected_type and not self._check_type(value, expected_type):
                    errors.append(
                        f"Field '{field}': expected {expected_type}, got {type(value).__name__}"
                    )

        # Check for unknown fields
        for field in data:
            if field not in properties and field not in required:
                errors.append(f"Unknown field: '{field}'")

        return errors

    def coerce(self, data: dict) -> dict:
        """Coerce data types to match schema types."""
        result = dict(data)
        properties = self.schema.get("properties", {})

        for field, prop in properties.items():
            if field in result:
                expected_type = prop.get("type", "")
                result[field] = self._coerce_value(result[field], expected_type)

        # Inject default values
        for field, prop in properties.items():
            if field not in result and "default" in prop:
                result[field] = prop["default"]

        return result

    def extract_from_text(self, text: str) -> dict | None:
        """Try to extract a JSON object from text content."""
        # Try to find JSON in code blocks
        json_match = text.strip()
        if json_match.startswith("```"):
            lines = json_match.split("\n")
            if len(lines) >= 2:
                json_match = "\n".join(lines[1:-1])

        # Try to parse as JSON
        try:
            data = json.loads(json_match)
            if isinstance(data, dict):
                return self.coerce(data)
            return None
        except json.JSONDecodeError:
            pass

        # Try to find {...} in the text
        import re

        obj_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if obj_match:
            try:
                data = json.loads(obj_match.group())
                if isinstance(data, dict):
                    return self.coerce(data)
            except json.JSONDecodeError:
                pass

        return None

    def _check_type(self, value: Any, expected_type: str) -> bool:
        type_map = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "array": (list, tuple),
            "object": dict,
        }
        py_type = type_map.get(expected_type)
        if py_type is None:
            return True
        return isinstance(value, py_type)

    def _coerce_value(self, value: Any, expected_type: str) -> Any:
        try:
            if expected_type == "string":
                return str(value)
            elif expected_type == "number":
                return float(value)
            elif expected_type == "integer":
                return int(value)
            elif expected_type == "boolean":
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes")
                return bool(value)
            elif expected_type == "json":
                if isinstance(value, str):
                    return json.loads(value)
                return value
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        return value

    def to_instruction(self) -> str:
        """Generate an instruction string for the LLM prompt."""
        if not self.schema:
            return ""
        return (
            "You MUST respond with a valid JSON object matching this schema:\n"
            f"{json.dumps(self.schema, indent=2)}\n"
            "Do NOT include any text outside the JSON object."
        )

    @property
    def is_empty(self) -> bool:
        return not self.schema
