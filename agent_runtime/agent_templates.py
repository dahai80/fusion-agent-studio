"""Agent templates — 8 preset configurations for common agent patterns.

Each template provides a complete AgentGraph config ready to execute.
Users can customize via variables before running.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentTemplate:
    """A reusable agent configuration template."""

    id: str
    name: str
    description: str
    category: str
    graph_data: dict[str, Any]
    variables: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "graph_data": self.graph_data,
            "variables": self.variables,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentTemplate:
        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            category=data["category"],
            graph_data=data["graph_data"],
            variables=data.get("variables", {}),
            tags=data.get("tags", []),
        )


def _llm_node(name: str, prompt: str, model: str = "") -> dict:
    return {
        "id": name,
        "type": "llm",
        "config": {"prompt": prompt, "model": model, "temperature": 0.7},
    }


def _start_node(name: str = "start") -> dict:
    return {"id": name, "type": "start", "config": {}}


def _end_node(name: str = "end") -> dict:
    return {"id": name, "type": "end", "config": {}}


def _tool_node(name: str, tool_name: str) -> dict:
    return {"id": name, "type": "tool", "config": {"tool": tool_name}}


def _condition_node(name: str, expression: str) -> dict:
    return {"id": name, "type": "condition", "config": {"expression": expression}}


def _edge(source: str, target: str, label: str = "") -> dict:
    return {"source": source, "target": target, "label": label}


TEMPLATES: dict[str, AgentTemplate] = {}


def _register(t: AgentTemplate) -> AgentTemplate:
    TEMPLATES[t.id] = t
    return t


# ── 1. Simple Chat ──────────────────────────────────────────
_register(
    AgentTemplate(
        id="simple-chat",
        name="Simple Chat",
        description="Basic single-turn chat agent with LLM response",
        category="basic",
        variables={"system_prompt": "You are a helpful assistant."},
        tags=["chat", "basic"],
        graph_data={
            "nodes": [
                _start_node("start"),
                _llm_node("chat", "{{system_prompt}}\n\nUser: {{input}}"),
                _end_node("end"),
            ],
            "edges": [
                _edge("start", "chat"),
                _edge("chat", "end"),
            ],
        },
    )
)


# ── 2. Code Reviewer ───────────────────────────────────────
_register(
    AgentTemplate(
        id="code-reviewer",
        name="Code Reviewer",
        description="Reviews code for bugs, style issues, and suggestions",
        category="development",
        variables={"language": "python", "focus_areas": "bugs, style, performance"},
        tags=["code", "review", "development"],
        graph_data={
            "nodes": [
                _start_node("start"),
                _llm_node(
                    "review",
                    (
                        "You are a code reviewer. Language: {{language}}\n"
                        "Focus: {{focus_areas}}\n\n"
                        "Review this code:\n{{input}}"
                    ),
                ),
                _end_node("end"),
            ],
            "edges": [
                _edge("start", "review"),
                _edge("review", "end"),
            ],
        },
    )
)


# ── 3. Research Assistant ──────────────────────────────────
_register(
    AgentTemplate(
        id="research-assistant",
        name="Research Assistant",
        description="Researches a topic and produces a structured summary",
        category="research",
        variables={"output_format": "markdown", "depth": "thorough"},
        tags=["research", "writing"],
        graph_data={
            "nodes": [
                _start_node("start"),
                _llm_node(
                    "research",
                    (
                        "You are a research assistant. Depth: {{depth}}\n"
                        "Output format: {{output_format}}\n\n"
                        "Research this topic:\n{{input}}"
                    ),
                ),
                _end_node("end"),
            ],
            "edges": [
                _edge("start", "research"),
                _edge("research", "end"),
            ],
        },
    )
)


# ── 4. Tool-Using Agent ───────────────────────────────────
_register(
    AgentTemplate(
        id="tool-agent",
        name="Tool-Using Agent",
        description="Agent that can invoke tools to complete tasks",
        category="advanced",
        variables={"available_tools": "shell, file_read, file_write"},
        tags=["tools", "agent"],
        graph_data={
            "nodes": [
                _start_node("start"),
                _llm_node(
                    "plan",
                    (
                        "You are an agent with tools: {{available_tools}}\n"
                        "Plan how to accomplish:\n{{input}}"
                    ),
                ),
                _tool_node("execute", "shell"),
                _llm_node(
                    "evaluate",
                    "Evaluate the results. Is the task complete? If not, what should change?",
                ),
                _condition_node("check_done", "result.contains('[COMPLETE]')"),
                _end_node("end"),
            ],
            "edges": [
                _edge("start", "plan"),
                _edge("plan", "execute"),
                _edge("execute", "evaluate"),
                _edge("evaluate", "check_done"),
                _edge("check_done", "end", label="true"),
                _edge("check_done", "plan", label="false"),
            ],
        },
    )
)


# ── 5. Multi-Step Pipeline ────────────────────────────────
_register(
    AgentTemplate(
        id="pipeline",
        name="Multi-Step Pipeline",
        description="Sequential pipeline: analyze → transform → output",
        category="advanced",
        variables={
            "analysis_prompt": "Analyze the input",
            "transform_prompt": "Transform based on analysis",
        },
        tags=["pipeline", "workflow"],
        graph_data={
            "nodes": [
                _start_node("start"),
                _llm_node("analyze", "{{analysis_prompt}}\n\nInput:\n{{input}}"),
                _llm_node(
                    "transform", "{{transform_prompt}}\n\nAnalysis:\n{{prev_output}}"
                ),
                _llm_node(
                    "output",
                    "Format the following as a final deliverable:\n{{prev_output}}",
                ),
                _end_node("end"),
            ],
            "edges": [
                _edge("start", "analyze"),
                _edge("analyze", "transform"),
                _edge("transform", "output"),
                _edge("output", "end"),
            ],
        },
    )
)


# ── 6. Code Generator ────────────────────────────────────
_register(
    AgentTemplate(
        id="code-generator",
        name="Code Generator",
        description="Generates code with planning, writing, and review stages",
        category="development",
        variables={"language": "python", "style_guide": "PEP 8"},
        tags=["code", "generation", "development"],
        graph_data={
            "nodes": [
                _start_node("start"),
                _llm_node(
                    "plan",
                    (
                        "Plan the code structure for:\n{{input}}\n"
                        "Language: {{language}}, Style: {{style_guide}}"
                    ),
                ),
                _llm_node(
                    "write",
                    (
                        "Write code based on this plan:\n{{prev_output}}\n"
                        "Language: {{language}}, Style: {{style_guide}}"
                    ),
                ),
                _llm_node(
                    "review",
                    (
                        "Review this code for correctness and style:\n{{prev_output}}\n"
                        "If issues found, describe fixes needed."
                    ),
                ),
                _end_node("end"),
            ],
            "edges": [
                _edge("start", "plan"),
                _edge("plan", "write"),
                _edge("write", "review"),
                _edge("review", "end"),
            ],
        },
    )
)


# ── 7. Data Analyst ──────────────────────────────────────
_register(
    AgentTemplate(
        id="data-analyst",
        name="Data Analyst",
        description="Analyzes data, produces insights and visualizations",
        category="data",
        variables={"data_format": "csv", "analysis_type": "descriptive"},
        tags=["data", "analysis"],
        graph_data={
            "nodes": [
                _start_node("start"),
                _llm_node(
                    "understand",
                    (
                        "Understand this data request:\n{{input}}\n"
                        "Data format: {{data_format}}, Analysis: {{analysis_type}}"
                    ),
                ),
                _llm_node(
                    "analyze",
                    (
                        "Perform {{analysis_type}} analysis:\n"
                        "Request understanding:\n{{prev_output}}"
                    ),
                ),
                _llm_node(
                    "insights",
                    "Summarize key insights from this analysis:\n{{prev_output}}",
                ),
                _end_node("end"),
            ],
            "edges": [
                _edge("start", "understand"),
                _edge("understand", "analyze"),
                _edge("analyze", "insights"),
                _edge("insights", "end"),
            ],
        },
    )
)


# ── 8. Multi-Agent Handoff ───────────────────────────────
_register(
    AgentTemplate(
        id="multi-agent-handoff",
        name="Multi-Agent Handoff",
        description="Chain of agents that hand off tasks sequentially",
        category="multi-agent",
        variables={"agents": "researcher,writer,reviewer"},
        tags=["multi-agent", "handoff"],
        graph_data={
            "nodes": [
                _start_node("start"),
                _llm_node(
                    "agent_1",
                    "You are the first agent in a chain. Agents: {{agents}}\nTask: {{input}}\nComplete your part, then write [HANDOFF] for the next agent.",
                ),
                _llm_node(
                    "agent_2",
                    "You are the second agent. Continue from where the previous agent left off:\n{{prev_output}}\nComplete your part, then write [COMPLETE] when done.",
                ),
                _end_node("end"),
            ],
            "edges": [
                _edge("start", "agent_1"),
                _edge("agent_1", "agent_2"),
                _edge("agent_2", "end"),
            ],
        },
    )
)


def list_templates(category: str = "") -> list[AgentTemplate]:
    templates = list(TEMPLATES.values())
    if category:
        templates = [t for t in templates if t.category == category]
    return templates


def get_template(template_id: str) -> AgentTemplate | None:
    return TEMPLATES.get(template_id)


def instantiate_template(
    template_id: str, variables: dict[str, str] | None = None
) -> dict[str, Any]:
    """Instantiate a template with variable substitutions."""
    tmpl = TEMPLATES.get(template_id)
    if not tmpl:
        logger.error("Template not found: %s", template_id)
        return {}

    graph_data = copy.deepcopy(tmpl.graph_data)
    merged_vars = {**tmpl.variables, **(variables or {})}

    for node in graph_data.get("nodes", []):
        config = node.get("config", {})
        if "prompt" in config:
            for key, val in merged_vars.items():
                config["prompt"] = config["prompt"].replace("{{" + key + "}}", val)

    logger.info(
        "Instantiated template %s with %d variables", template_id, len(merged_vars)
    )
    return graph_data
