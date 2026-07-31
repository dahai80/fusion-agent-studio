"""Text processing tools — search, transform, and analyze text content."""

from __future__ import annotations

import re

from .base import BaseTool


class TextProcessTool(BaseTool):
    """Process and transform text content."""

    name = "text_process"
    description = "Process text with various transformations"
    parameters = {
        "text": {
            "type": "string",
            "description": "Text content to process",
        },
        "operation": {
            "type": "string",
            "description": "Operation to perform",
            "enum": [
                "uppercase", "lowercase", "trim", "split_lines",
                "count_words", "count_lines", "count_chars",
                "reverse", "sort_lines", "unique_lines",
            ],
        },
    }

    async def execute(self, **kwargs) -> str:
        text = kwargs.get("text", "")
        operation = kwargs.get("operation", "count_words")

        if not text:
            return "Error: text is required"

        if operation == "uppercase":
            return text.upper()
        elif operation == "lowercase":
            return text.lower()
        elif operation == "trim":
            return text.strip()
        elif operation == "split_lines":
            lines = text.split("\n")
            result = []
            for i, line in enumerate(lines, 1):
                result.append(f"{i:4d}: {line}")
            return "\n".join(result)
        elif operation == "count_words":
            words = text.split()
            return f"Word count: {len(words)}"
        elif operation == "count_lines":
            lines = text.split("\n")
            non_empty = sum(1 for line in lines if line.strip())
            return f"Total lines: {len(lines)}, Non-empty: {non_empty}"
        elif operation == "count_chars":
            return f"Character count: {len(text)} (with spaces: {len(text)})"
        elif operation == "reverse":
            return text[::-1]
        elif operation == "sort_lines":
            lines = sorted(text.split("\n"))
            return "\n".join(lines)
        elif operation == "unique_lines":
            seen = set()
            result = []
            for line in text.split("\n"):
                if line not in seen:
                    seen.add(line)
                    result.append(line)
            return "\n".join(result)
        else:
            return f"Error: Unknown operation: {operation}"


class TextSearchTool(BaseTool):
    """Search for patterns in text content."""

    name = "text_search"
    description = "Search for patterns in text using regex or plain text"
    parameters = {
        "text": {
            "type": "string",
            "description": "Text content to search in",
        },
        "pattern": {
            "type": "string",
            "description": "Search pattern (regex or plain text)",
        },
        "use_regex": {
            "type": "boolean",
            "description": "Use regex matching (default: False)",
            "default": False,
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of matches to return",
            "default": 20,
        },
    }

    async def execute(self, **kwargs) -> str:
        text = kwargs.get("text", "")
        pattern = kwargs.get("pattern", "")
        use_regex = kwargs.get("use_regex", False)
        max_results = int(kwargs.get("max_results", 20))

        if not text:
            return "Error: text is required"
        if not pattern:
            return "Error: pattern is required"

        try:
            if use_regex:
                matches = list(re.finditer(pattern, text))
                result_lines = []
                count = 0
                for m in matches:
                    if count >= max_results:
                        break
                    start = max(0, m.start() - 20)
                    end = min(len(text), m.end() + 20)
                    context = text[start:end].replace("\n", " ")
                    result_lines.append(
                        f"Match {count + 1} at pos {m.start()}: ...{context}..."
                    )
                    count += 1
                if not result_lines:
                    return f"No matches found for pattern: {pattern}"
                summary = f"Found {count} match(es) for pattern: {pattern}"
                if count < len(list(re.finditer(pattern, text))):
                    summary += f" (showing first {count})"
                return summary + "\n" + "\n".join(result_lines)
            else:
                count = text.count(pattern)
                if count == 0:
                    return f"No matches found for: {pattern}"
                # Show context around each match
                result_lines = []
                start = 0
                for i in range(min(max_results, count)):
                    pos = text.index(pattern, start)
                    ctx_start = max(0, pos - 20)
                    ctx_end = min(len(text), pos + len(pattern) + 20)
                    context = text[ctx_start:ctx_end].replace("\n", " ")
                    result_lines.append(f"Match {i + 1} at pos {pos}: ...{context}...")
                    start = pos + 1
                summary = f"Found {count} occurrence(s) of '{pattern}'"
                if count > max_results:
                    summary += f" (showing first {max_results})"
                return summary + "\n" + "\n".join(result_lines)

        except re.error as e:
            return f"Error in regex pattern: {e}"
        except Exception as e:
            return f"Error searching text: {e}"