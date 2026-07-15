"""Code execution tool — run Python code in a sandboxed environment."""
from __future__ import annotations
from .base import BaseTool


class CodeExecuteTool(BaseTool):
    name = "code_execute"
    description = "Execute Python code and return the output. Use print() to produce output."
    parameters = {
        "code": {"type": "string", "description": "Python code to execute"},
        "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 10},
    }

    async def execute(self, **kwargs) -> str:
        code = kwargs.get("code", "")
        timeout = int(kwargs.get("timeout", 10))
        if not code:
            return "Error: code is required"
        import asyncio, io, contextlib, sys
        allowed = {
            "abs": abs, "all": all, "any": any, "bool": bool, "chr": chr,
            "dict": dict, "dir": dir, "enumerate": enumerate, "float": float,
            "format": format, "frozenset": frozenset, "int": int,
            "isinstance": isinstance, "len": len, "list": list, "map": map,
            "max": max, "min": min, "ord": ord, "pow": pow, "print": print,
            "range": range, "repr": repr, "reversed": reversed, "round": round,
            "set": set, "slice": slice, "sorted": sorted, "str": str,
            "sum": sum, "tuple": tuple, "type": type, "zip": zip,
            "json": __import__("json"), "math": __import__("math"),
            "re": __import__("re"), "collections": __import__("collections"),
            "itertools": __import__("itertools"), "statistics": __import__("statistics"),
        }
        f = io.StringIO()
        try:
            with contextlib.redirect_stdout(f):
                exec(code, {"__builtins__": allowed}, {})
            output = f.getvalue()
            return output if output else "(no output)"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"