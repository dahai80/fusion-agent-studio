"""Template manager — preset agent graph templates for quick start."""

from __future__ import annotations

import logging

from .graph import AgentGraph, NodeConfig

logger = logging.getLogger(__name__)


class TemplateManager:
    """Manages preset agent templates for quick-start and reuse."""

    _templates: dict[str, AgentGraph] = {}

    @classmethod
    def register(cls, name: str, graph: AgentGraph) -> None:
        cls._templates[name] = graph

    @classmethod
    def get(cls, name: str) -> AgentGraph:
        if name not in cls._templates:
            raise KeyError(
                f"Template '{name}' not found. Available: {list(cls._templates.keys())}"
            )
        return cls._templates[name]

    @classmethod
    def list(cls) -> list[dict]:
        return [
            {
                "name": k,
                "description": v.description,
                "node_count": len(v.nodes),
                "edge_count": len(v.edges),
            }
            for k, v in cls._templates.items()
        ]

    @classmethod
    def has(cls, name: str) -> bool:
        return name in cls._templates

    @classmethod
    def count(cls) -> int:
        return len(cls._templates)


def register_default_templates() -> None:
    """Register all built-in agent templates."""
    templates = [
        {
            "name": "code-assistant",
            "description": "Read source code → analyze → suggest improvements → write changes",
            "nodes": [
                (
                    "start",
                    NodeConfig(
                        type="start",
                        label="Start",
                        system_prompt="You are a senior code reviewer.",
                        x=50,
                        y=300,
                    ),
                ),
                (
                    "llm_read",
                    NodeConfig(
                        type="llm",
                        label="Analyze Code",
                        model="qwen3.5-9b",
                        temperature=0.3,
                        x=250,
                        y=200,
                    ),
                ),
                (
                    "tool_read",
                    NodeConfig(
                        type="tool",
                        label="Read File",
                        tool_name="file_read",
                        x=250,
                        y=400,
                    ),
                ),
                (
                    "llm_suggest",
                    NodeConfig(
                        type="llm",
                        label="Suggest Changes",
                        model="qwen3.5-9b",
                        temperature=0.5,
                        x=450,
                        y=300,
                    ),
                ),
                ("end", NodeConfig(type="end", label="Output Result", x=650, y=300)),
            ],
            "edges": [
                ("start", "tool_read"),
                ("tool_read", "llm_read"),
                ("llm_read", "llm_suggest"),
                ("llm_suggest", "end"),
            ],
        },
        {
            "name": "file-organizer",
            "description": "Scan directory → categorize files → organize into folders",
            "nodes": [
                (
                    "start",
                    NodeConfig(
                        type="start",
                        label="Start",
                        system_prompt="You are a file organization assistant.",
                        x=50,
                        y=300,
                    ),
                ),
                (
                    "tool_list",
                    NodeConfig(
                        type="tool",
                        label="List Files",
                        tool_name="file_list",
                        x=250,
                        y=200,
                    ),
                ),
                (
                    "llm_sort",
                    NodeConfig(
                        type="llm",
                        label="Categorize",
                        model="qwen3.5-9b",
                        temperature=0.3,
                        x=450,
                        y=200,
                    ),
                ),
                ("end", NodeConfig(type="end", label="Summary", x=650, y=300)),
            ],
            "edges": [
                ("start", "tool_list"),
                ("tool_list", "llm_sort"),
                ("llm_sort", "end"),
            ],
        },
        {
            "name": "terminal-automation",
            "description": "Natural language → shell commands → execute → review output",
            "nodes": [
                (
                    "start",
                    NodeConfig(
                        type="start",
                        label="Start",
                        system_prompt="You are a terminal automation assistant.",
                        x=50,
                        y=300,
                    ),
                ),
                (
                    "llm_plan",
                    NodeConfig(
                        type="llm",
                        label="Plan Command",
                        model="qwen3.5-9b",
                        temperature=0.2,
                        x=250,
                        y=200,
                    ),
                ),
                (
                    "tool_run",
                    NodeConfig(
                        type="tool",
                        label="Run Command",
                        tool_name="terminal",
                        x=250,
                        y=400,
                    ),
                ),
                (
                    "llm_review",
                    NodeConfig(
                        type="llm",
                        label="Review Output",
                        model="qwen3.5-9b",
                        temperature=0.3,
                        x=450,
                        y=300,
                    ),
                ),
                ("end", NodeConfig(type="end", label="Done", x=650, y=300)),
            ],
            "edges": [
                ("start", "llm_plan"),
                ("llm_plan", "tool_run"),
                ("tool_run", "llm_review"),
                ("llm_review", "end"),
            ],
        },
        {
            "name": "data-extractor",
            "description": "Read file → extract structured data → save as CSV",
            "nodes": [
                (
                    "start",
                    NodeConfig(
                        type="start",
                        label="Start",
                        system_prompt="Extract structured data from files.",
                        x=50,
                        y=300,
                    ),
                ),
                (
                    "tool_read",
                    NodeConfig(
                        type="tool",
                        label="Read File",
                        tool_name="file_read",
                        x=250,
                        y=200,
                    ),
                ),
                (
                    "llm_extract",
                    NodeConfig(
                        type="llm",
                        label="Extract Data",
                        model="qwen3.5-9b",
                        temperature=0.3,
                        x=450,
                        y=200,
                    ),
                ),
                (
                    "tool_save",
                    NodeConfig(
                        type="tool",
                        label="Save CSV",
                        tool_name="file_write",
                        x=450,
                        y=400,
                    ),
                ),
                ("end", NodeConfig(type="end", label="Done", x=650, y=300)),
            ],
            "edges": [
                ("start", "tool_read"),
                ("tool_read", "llm_extract"),
                ("llm_extract", "tool_save"),
                ("tool_save", "end"),
            ],
        },
        {
            "name": "web-summary",
            "description": "Fetch webpage → extract content → LLM summarize → save",
            "nodes": [
                (
                    "start",
                    NodeConfig(
                        type="start",
                        label="Start",
                        system_prompt="Summarize web content.",
                        x=50,
                        y=300,
                    ),
                ),
                (
                    "tool_fetch",
                    NodeConfig(
                        type="tool",
                        label="Fetch URL",
                        tool_name="http_request",
                        x=250,
                        y=200,
                    ),
                ),
                (
                    "llm_summarize",
                    NodeConfig(
                        type="llm",
                        label="Summarize",
                        model="qwen3.5-9b",
                        temperature=0.3,
                        x=450,
                        y=200,
                    ),
                ),
                (
                    "tool_save",
                    NodeConfig(
                        type="tool",
                        label="Save Summary",
                        tool_name="file_write",
                        x=450,
                        y=400,
                    ),
                ),
                ("end", NodeConfig(type="end", label="Done", x=650, y=300)),
            ],
            "edges": [
                ("start", "tool_fetch"),
                ("tool_fetch", "llm_summarize"),
                ("llm_summarize", "tool_save"),
                ("tool_save", "end"),
            ],
        },
        {
            "name": "batch-rename",
            "description": "List files → LLM generate new names → rename all",
            "nodes": [
                (
                    "start",
                    NodeConfig(
                        type="start",
                        label="Start",
                        system_prompt="Generate new filenames based on content.",
                        x=50,
                        y=300,
                    ),
                ),
                (
                    "tool_list",
                    NodeConfig(
                        type="tool",
                        label="List Files",
                        tool_name="file_list",
                        x=250,
                        y=200,
                    ),
                ),
                (
                    "llm_rename",
                    NodeConfig(
                        type="llm",
                        label="Generate Names",
                        model="qwen3.5-9b",
                        temperature=0.5,
                        x=450,
                        y=200,
                    ),
                ),
                ("end", NodeConfig(type="end", label="Done", x=650, y=300)),
            ],
            "edges": [
                ("start", "tool_list"),
                ("tool_list", "llm_rename"),
                ("llm_rename", "end"),
            ],
        },
        {
            "name": "code-review",
            "description": "Read multiple files → review code → generate report",
            "nodes": [
                (
                    "start",
                    NodeConfig(
                        type="start",
                        label="Start",
                        system_prompt="You are a thorough code reviewer.",
                        x=50,
                        y=300,
                    ),
                ),
                (
                    "tool_read",
                    NodeConfig(
                        type="tool",
                        label="Read Source",
                        tool_name="file_read",
                        x=250,
                        y=200,
                    ),
                ),
                (
                    "llm_review",
                    NodeConfig(
                        type="llm",
                        label="Review Code",
                        model="qwen3.5-9b",
                        temperature=0.3,
                        x=450,
                        y=200,
                    ),
                ),
                ("end", NodeConfig(type="end", label="Report", x=650, y=300)),
            ],
            "edges": [
                ("start", "tool_read"),
                ("tool_read", "llm_review"),
                ("llm_review", "end"),
            ],
        },
        {
            "name": "git-automation",
            "description": "Check git status → commit changes → push to remote",
            "nodes": [
                (
                    "start",
                    NodeConfig(
                        type="start",
                        label="Start",
                        system_prompt="You are a git automation assistant.",
                        x=50,
                        y=300,
                    ),
                ),
                (
                    "tool_status",
                    NodeConfig(
                        type="tool", label="Check Status", tool_name="git", x=250, y=200
                    ),
                ),
                (
                    "llm_decide",
                    NodeConfig(
                        type="llm",
                        label="Decide Action",
                        model="qwen3.5-9b",
                        temperature=0.2,
                        x=450,
                        y=200,
                    ),
                ),
                (
                    "tool_commit",
                    NodeConfig(
                        type="tool", label="Commit", tool_name="git", x=450, y=400
                    ),
                ),
                ("end", NodeConfig(type="end", label="Done", x=650, y=300)),
            ],
            "edges": [
                ("start", "tool_status"),
                ("tool_status", "llm_decide"),
                ("llm_decide", "tool_commit"),
                ("tool_commit", "end"),
            ],
        },
    ]

    for tpl in templates:
        graph = AgentGraph(name=tpl["name"], description=tpl["description"])
        for nid, cfg in tpl["nodes"]:
            graph.add_node(nid, cfg)
        for src, tgt in tpl["edges"]:
            graph.add_edge(src, tgt)
        TemplateManager.register(tpl["name"], graph)


def _plan_template_graphs() -> list[dict]:
    """#558: 7 plan §7.3 builtin templates as from_dict-shaped dicts."""
    import json as _json

    iteration_sub = {
        "name": "map-item",
        "nodes": [
            {"id": "s", "type": "start", "label": "Item Start"},
            {
                "id": "llm",
                "type": "llm",
                "label": "Process Item",
                "model": "qwen3.5-9b",
                "system_prompt": "Process the given item and return the result.",
            },
            {"id": "out", "type": "end", "label": "Item End"},
        ],
        "edges": [
            {"source_id": "s", "target_id": "llm"},
            {"source_id": "llm", "target_id": "out"},
        ],
        "start_node_id": "s",
    }
    return [
        {
            "name": "basic-llm-prompt",
            "description": "Single LLM call: prompt in → answer out (simplest useful graph)",
            "nodes": [
                {"id": "start", "type": "start", "label": "Start"},
                {
                    "id": "llm",
                    "type": "llm",
                    "label": "LLM Answer",
                    "model": "qwen3.5-9b",
                    "system_prompt": "You are a helpful assistant.",
                },
                {"id": "end", "type": "end", "label": "End"},
            ],
            "edges": [
                {"source_id": "start", "target_id": "llm"},
                {"source_id": "llm", "target_id": "end"},
            ],
            "start_node_id": "start",
        },
        {
            "name": "rag-qa",
            "description": "RAG retrieval → LLM grounded answer (knowledge-base QA)",
            "nodes": [
                {"id": "start", "type": "start", "label": "Start"},
                {
                    "id": "rag",
                    "type": "rag",
                    "label": "Retrieve Context",
                    "tool_params": {"rag_config": {"top_k": 5}},
                },
                {
                    "id": "llm",
                    "type": "llm",
                    "label": "Grounded Answer",
                    "model": "qwen3.5-9b",
                    "system_prompt": "Answer strictly from the retrieved context.",
                },
                {"id": "end", "type": "end", "label": "End"},
            ],
            "edges": [
                {"source_id": "start", "target_id": "rag"},
                {"source_id": "rag", "target_id": "llm"},
                {"source_id": "llm", "target_id": "end"},
            ],
            "start_node_id": "start",
        },
        {
            "name": "agent-with-tools",
            "description": "Agentic LLM loop with tool calls (multi-turn tool use)",
            "nodes": [
                {"id": "start", "type": "start", "label": "Start"},
                {
                    "id": "agent",
                    "type": "llm",
                    "label": "Agent Loop",
                    "model": "qwen3.5-9b",
                    "system_prompt": "You are an agent that uses tools to complete the task.",
                    "loop_mode": "agent",
                    "max_loop_iterations": 10,
                },
                {"id": "end", "type": "end", "label": "End"},
            ],
            "edges": [
                {"source_id": "start", "target_id": "agent"},
                {"source_id": "agent", "target_id": "end"},
            ],
            "start_node_id": "start",
        },
        {
            "name": "supervisor-worker",
            "description": "Supervisor LLM → condition branch → worker LLM (delegate pattern)",
            "nodes": [
                {"id": "start", "type": "start", "label": "Start"},
                {
                    "id": "supervisor",
                    "type": "llm",
                    "label": "Supervisor",
                    "model": "qwen3.5-9b",
                    "system_prompt": "Decide whether the task needs revision. Set needs_revision yes/no.",
                },
                {
                    "id": "branch",
                    "type": "condition",
                    "label": "Needs Revision?",
                    "condition_expr": "needs_revision == 'yes'",
                },
                {
                    "id": "worker",
                    "type": "llm",
                    "label": "Worker Rewrite",
                    "model": "qwen3.5-9b",
                    "system_prompt": "Revise the work according to the supervisor feedback.",
                },
                {"id": "end", "type": "end", "label": "End"},
            ],
            "edges": [
                {"source_id": "start", "target_id": "supervisor"},
                {"source_id": "supervisor", "target_id": "branch"},
                {"source_id": "branch", "target_id": "worker", "label": "true"},
                {"source_id": "branch", "target_id": "end", "label": "false"},
                {"source_id": "worker", "target_id": "end"},
            ],
            "start_node_id": "start",
        },
        {
            "name": "human-approval-flow",
            "description": "LLM draft → human approval gate (pause/resume on decision)",
            "nodes": [
                {"id": "start", "type": "start", "label": "Start"},
                {
                    "id": "draft",
                    "type": "llm",
                    "label": "Draft",
                    "model": "qwen3.5-9b",
                    "system_prompt": "Draft the deliverable requested by the user.",
                },
                {
                    "id": "approve",
                    "type": "human_input",
                    "label": "Human Approval",
                    "tool_params": {
                        "input_var": "approval",
                        "prompt": "Approve this draft?",
                        "timeout_seconds": 3600,
                    },
                },
                {"id": "end", "type": "end", "label": "End"},
            ],
            "edges": [
                {"source_id": "start", "target_id": "draft"},
                {"source_id": "draft", "target_id": "approve"},
                {"source_id": "approve", "target_id": "end"},
            ],
            "start_node_id": "start",
        },
        {
            "name": "document-pipeline",
            "description": "Read file → LLM transform → write result file",
            "nodes": [
                {"id": "start", "type": "start", "label": "Start"},
                {
                    "id": "read",
                    "type": "tool",
                    "label": "Read Document",
                    "tool_name": "file_read",
                    "tool_params": {"path": "{{ doc_path }}"},
                },
                {
                    "id": "llm",
                    "type": "llm",
                    "label": "Transform",
                    "model": "qwen3.5-9b",
                    "system_prompt": "Transform the document content as requested.",
                },
                {
                    "id": "write",
                    "type": "tool",
                    "label": "Write Result",
                    "tool_name": "file_write",
                    "tool_params": {"path": "{{ out_path }}", "content": "{{ llm_output }}"},
                },
                {"id": "end", "type": "end", "label": "End"},
            ],
            "edges": [
                {"source_id": "start", "target_id": "read"},
                {"source_id": "read", "target_id": "llm"},
                {"source_id": "llm", "target_id": "write"},
                {"source_id": "write", "target_id": "end"},
            ],
            "start_node_id": "start",
        },
        {
            "name": "iteration-map",
            "description": "Map over a list variable → aggregate results (fan-out pattern)",
            "nodes": [
                {"id": "start", "type": "start", "label": "Start"},
                {
                    "id": "map",
                    "type": "iteration",
                    "label": "Map Items",
                    "tool_params": {
                        "items_var": "items",
                        "output_var": "results",
                        "max_items": 100,
                        "graph_json": _json.dumps(iteration_sub, ensure_ascii=False),
                        "input_mapping": {"item": "input"},
                    },
                },
                {
                    "id": "agg",
                    "type": "variable_aggregator",
                    "label": "Aggregate",
                    "tool_params": {"input_vars": ["results"], "output_var": "final"},
                },
                {"id": "end", "type": "end", "label": "End"},
            ],
            "edges": [
                {"source_id": "start", "target_id": "map"},
                {"source_id": "map", "target_id": "agg"},
                {"source_id": "agg", "target_id": "end"},
            ],
            "start_node_id": "start",
        },
    ]


def register_plan_templates() -> int:
    """#558: register the 7 plan §7.3 templates; returns count registered."""
    registered = 0
    for data in _plan_template_graphs():
        graph = AgentGraph.from_dict(data)
        graph.description = data["description"]
        TemplateManager.register(data["name"], graph)
        registered += 1
    logger.info("plan templates registered count=%d", registered)
    return registered
