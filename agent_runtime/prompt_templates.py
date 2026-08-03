"""Prompt template — reusable prompt templates with variable interpolation.

Importers: runtime.py (PromptTemplateManager), tests/test_prompt_templates.py
API: register_default_prompt_templates(), PromptTemplateManager.register/render/list
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class PromptTemplate:
    """A reusable prompt template with variable placeholders.

    Variables are referenced as {{ variable_name }} and are replaced
    at render time. Supports default values, descriptions, and
    structured variable definitions.
    """

    name: str
    template: str
    description: str = ""
    variables: dict[str, dict] = field(default_factory=dict)
    category: str = "general"

    def render(self, **kwargs) -> str:
        """Render the template by replacing variables with provided values."""
        result = self.template
        for var_name, var_config in self.variables.items():
            value = kwargs.get(var_name, var_config.get("default", ""))
            # Replace both {{ name }} and {{name}} variants
            result = result.replace("{{ " + var_name + " }}", str(value))
            result = result.replace("{{" + var_name + "}}", str(value))
        # Replace any remaining variables with empty string
        result = re.sub(r"\{\{.*?\}\}", "", result)
        return result

    def validate(self, **kwargs) -> list[str]:
        """Check that all required variables are provided."""
        errors = []
        for var_name, var_config in self.variables.items():
            if var_config.get("required", False) and var_name not in kwargs:
                errors.append(f"Missing required variable: '{var_name}'")
        return errors

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "template": self.template[:100] + "..."
            if len(self.template) > 100
            else self.template,
            "description": self.description,
            "variables": self.variables,
            "category": self.category,
        }


class PromptTemplateManager:
    """Manages a collection of reusable prompt templates."""

    def __init__(self):
        self._templates: dict[str, PromptTemplate] = {}

    def register(self, template: PromptTemplate) -> None:
        self._templates[template.name] = template

    def get(self, name: str) -> PromptTemplate:
        if name not in self._templates:
            raise KeyError(f"Template '{name}' not found")
        return self._templates[name]

    def list(self, category: str = "") -> list[dict]:
        templates = self._templates.values()
        if category:
            templates = [t for t in templates if t.category == category]
        return [t.to_dict() for t in templates]

    def render(self, name: str, **kwargs) -> str:
        return self.get(name).render(**kwargs)


def register_default_prompt_templates(manager: PromptTemplateManager) -> None:
    """Register the built-in prompt templates."""
    templates = [
        PromptTemplate(
            name="code-review",
            template=(
                "You are a senior code reviewer. Review the following code:\n\n"
                "```{{ language }}\n{{ code }}\n```\n\n"
                "Focus on: {{ focus_areas }}\n"
                "Provide specific, actionable feedback."
            ),
            description="Review code with specific focus areas",
            variables={
                "code": {
                    "type": "string",
                    "description": "Source code to review",
                    "required": True,
                },
                "language": {
                    "type": "string",
                    "description": "Programming language",
                    "default": "python",
                },
                "focus_areas": {
                    "type": "string",
                    "description": "Areas to focus on",
                    "default": "correctness, performance, readability",
                },
            },
            category="coding",
        ),
        PromptTemplate(
            name="summarize",
            template=(
                "Summarize the following text in {{ style }} style:\n\n"
                "{{ text }}\n\n"
                "Keep the summary under {{ max_words }} words."
            ),
            description="Summarize text in a specified style",
            variables={
                "text": {
                    "type": "string",
                    "description": "Text to summarize",
                    "required": True,
                },
                "style": {
                    "type": "string",
                    "description": "Summary style",
                    "default": "concise",
                },
                "max_words": {
                    "type": "number",
                    "description": "Maximum words",
                    "default": "200",
                },
            },
            category="writing",
        ),
        PromptTemplate(
            name="data-extract",
            template=(
                "Extract the following information from the text below:\n"
                "- {{ field1 }}\n"
                "- {{ field2 }}\n"
                "- {{ field3 }}\n\n"
                "Text:\n{{ text }}\n\n"
                "Return the result as a JSON object."
            ),
            description="Extract specific fields from text",
            variables={
                "text": {
                    "type": "string",
                    "description": "Text to extract from",
                    "required": True,
                },
                "field1": {
                    "type": "string",
                    "description": "First field to extract",
                    "default": "name",
                },
                "field2": {
                    "type": "string",
                    "description": "Second field to extract",
                    "default": "date",
                },
                "field3": {
                    "type": "string",
                    "description": "Third field to extract",
                    "default": "value",
                },
            },
            category="data",
        ),
        PromptTemplate(
            name="translate",
            template=(
                "Translate the following text from {{ source_lang }} to {{ target_lang }}:\n\n"
                "{{ text }}\n\n"
                "Only return the translation, no explanations."
            ),
            description="Translate text between languages",
            variables={
                "text": {
                    "type": "string",
                    "description": "Text to translate",
                    "required": True,
                },
                "source_lang": {
                    "type": "string",
                    "description": "Source language",
                    "default": "English",
                },
                "target_lang": {
                    "type": "string",
                    "description": "Target language",
                    "default": "Chinese",
                },
            },
            category="writing",
        ),
        PromptTemplate(
            name="terminal-command",
            template=(
                "Convert the following request into a shell command:\n\n"
                "Request: {{ request }}\n\n"
                "Constraints: {{ constraints }}\n"
                "Return ONLY the command, no explanations."
            ),
            description="Convert natural language to shell commands",
            variables={
                "request": {
                    "type": "string",
                    "description": "Natural language request",
                    "required": True,
                },
                "constraints": {
                    "type": "string",
                    "description": "Safety constraints",
                    "default": "Safe, read-only operations only",
                },
            },
            category="coding",
        ),
        PromptTemplate(
            name="artifact-long-text",
            template=(
                "{{ artifact_guidelines }}\n\n"
                "Current session has {{ artifact_count }} active artifact(s):\n"
                "{{ artifact_list }}\n\n"
                "Follow the artifact guidelines above when producing or editing long content."
            ),
            description="Inject artifact-aware long-text guidelines when artifacts are active",
            variables={
                "artifact_guidelines": {
                    "type": "string",
                    "description": "Full artifact system prompt text",
                    "required": True,
                },
                "artifact_count": {
                    "type": "integer",
                    "description": "Number of active artifacts",
                    "default": "0",
                },
                "artifact_list": {
                    "type": "string",
                    "description": "Summary list of active artifacts",
                    "default": "",
                },
            },
            category="artifact",
        ),
    ]
    for t in templates:
        manager.register(t)
