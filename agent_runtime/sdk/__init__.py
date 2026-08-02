from .agent import Agent
from .tool import Tool
from .client import AgentClient

__all__ = [
    "Agent",
    "Tool",
    "AgentClient",
    "list_available_types",
    "verify_agent",
    "scaffold_agent",
]


def list_available_types() -> list[dict]:
    return [
        {
            "name": "Agent",
            "description": "Core agent with graph-based execution",
            "module": "agent_runtime.sdk.agent",
        },
        {
            "name": "Tool",
            "description": "Custom tool definition with schema",
            "module": "agent_runtime.sdk.tool",
        },
        {
            "name": "AgentClient",
            "description": "JSON-RPC client for daemon_server",
            "module": "agent_runtime.sdk.client",
        },
    ]


def verify_agent(agent_def: dict) -> dict:
    errors = []
    if not agent_def.get("name"):
        errors.append("Missing required field: name")
    if not agent_def.get("graph_id") and not agent_def.get("system_prompt"):
        errors.append("Must provide either graph_id or system_prompt")
    skills = agent_def.get("skills", [])
    if not isinstance(skills, list):
        errors.append("skills must be a list")
    return {"valid": len(errors) == 0, "errors": errors}


def scaffold_agent(
    name: str = "my_agent", template: str = "basic", output_dir: str = ""
) -> dict:
    import json

    templates = {
        "basic": {
            "name": name,
            "system_prompt": f"You are {name}, a helpful assistant.",
            "skills": [],
        },
        "coder": {
            "name": name,
            "system_prompt": f"You are {name}, an expert programmer. Write clean, efficient code.",
            "skills": ["code_search", "file_edit", "shell_exec"],
        },
        "reviewer": {
            "name": name,
            "system_prompt": f"You are {name}, a code reviewer. Analyze code for bugs, security issues, and style.",
            "skills": ["code_search", "file_read"],
        },
        "researcher": {
            "name": name,
            "system_prompt": f"You are {name}, a research agent. Find and synthesize information.",
            "skills": ["web_search", "knowledge_search", "memory_store"],
        },
    }
    agent_def = templates.get(template, templates["basic"])
    agent_def["template"] = template
    if output_dir:
        import os

        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, f"{name}.json")
        with open(path, "w") as f:
            json.dump(agent_def, f, indent=2, ensure_ascii=False)
        agent_def["output_file"] = path
    return agent_def
