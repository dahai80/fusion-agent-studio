"""Data format tools — JSON, CSV, YAML, Base64 parsing and conversion."""
from __future__ import annotations
import json, csv, io, base64
from .base import BaseTool


class JsonParseTool(BaseTool):
    name = "json_parse"
    description = "Parse, validate, extract, or transform JSON data"
    parameters = {
        "input": {"type": "string", "description": "JSON string to process"},
        "operation": {
            "type": "string", "enum": ["parse", "validate", "pretty_print", "extract_keys", "count"],
            "description": "Operation to perform",
        },
        "query": {"type": "string", "description": "Key path to extract (dot-separated, e.g. 'data.items')", "default": ""},
    }

    async def execute(self, **kwargs) -> str:
        input_str = kwargs.get("input", "")
        operation = kwargs.get("operation", "parse")
        query = kwargs.get("query", "")
        if not input_str:
            return "Error: input is required"
        try:
            data = json.loads(input_str) if isinstance(input_str, str) else input_str
        except json.JSONDecodeError as e:
            return f"Invalid JSON: {e}"
        if operation == "validate":
            return "Valid JSON"
        if operation == "pretty_print":
            return json.dumps(data, indent=2, ensure_ascii=False)
        if operation == "extract_keys":
            if isinstance(data, dict):
                return "\n".join(f"  {k}: {type(v).__name__}" for k, v in data.items())
            return "Error: input is not a JSON object"
        if operation == "count":
            if isinstance(data, list):
                return f"Array with {len(data)} items"
            if isinstance(data, dict):
                return f"Object with {len(data)} keys"
            return "Scalar value"
        return json.dumps(data, indent=2, ensure_ascii=False)[:10000]


class CsvParseTool(BaseTool):
    name = "csv_parse"
    description = "Parse, filter, or convert CSV data"
    parameters = {
        "input": {"type": "string", "description": "CSV string to process"},
        "operation": {
            "type": "string", "enum": ["parse", "count_rows", "get_headers", "to_json"],
            "description": "Operation to perform",
        },
        "delimiter": {"type": "string", "description": "CSV delimiter", "default": ","},
        "max_rows": {"type": "integer", "description": "Max rows to return", "default": 20},
    }

    async def execute(self, **kwargs) -> str:
        input_str = kwargs.get("input", "")
        operation = kwargs.get("operation", "parse")
        delimiter = kwargs.get("delimiter", ",")
        max_rows = int(kwargs.get("max_rows", 20))
        if not input_str:
            return "Error: input is required"
        try:
            reader = csv.DictReader(io.StringIO(input_str), delimiter=delimiter)
            rows = list(reader)
        except Exception as e:
            return f"Error parsing CSV: {e}"
        if not rows:
            return "Empty CSV"
        headers = list(rows[0].keys())
        if operation == "get_headers":
            return f"Headers ({len(headers)}): {', '.join(headers)}"
        if operation == "count_rows":
            return f"Rows: {len(rows)}"
        if operation == "to_json":
            return json.dumps(rows[:max_rows], indent=2, ensure_ascii=False)
        result = [f"Headers: {', '.join(headers)}", f"Rows: {len(rows)}"]
        for i, row in enumerate(rows[:max_rows]):
            result.append(f"Row {i+1}: {dict(row)}")
        if len(rows) > max_rows:
            result.append(f"... ({len(rows) - max_rows} more rows)")
        return "\n".join(result)


class Base64Tool(BaseTool):
    name = "base64"
    description = "Encode or decode Base64 data"
    parameters = {
        "input": {"type": "string", "description": "Text to encode or decode"},
        "operation": {"type": "string", "enum": ["encode", "decode"], "description": "Encode or decode"},
    }

    async def execute(self, **kwargs) -> str:
        input_str = kwargs.get("input", "")
        operation = kwargs.get("operation", "encode")
        if not input_str:
            return "Error: input is required"
        try:
            if operation == "encode":
                return base64.b64encode(input_str.encode()).decode()
            else:
                return base64.b64decode(input_str.encode()).decode()
        except Exception as e:
            return f"Error: {e}"