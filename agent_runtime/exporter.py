"""Graph exporter — exports an AgentGraph to a standalone Python script."""

from __future__ import annotations

from .graph import AgentGraph


class GraphExporter:
    """Exports agent graphs to various output formats."""

    @staticmethod
    def to_python(graph: AgentGraph, include_runtime: bool = True) -> str:
        """Export graph as a standalone Python script.

        Args:
            graph: The agent graph to export.
            include_runtime: If True, include the runtime code inline.

        Returns:
            A complete Python script that can run independently.
        """
        lines = []
        lines.append("#!/usr/bin/env python3")
        lines.append('"""Auto-generated agent: %s"""' % graph.name)
        lines.append("")
        lines.append("import asyncio")
        lines.append("import json")
        lines.append("import httpx")
        lines.append("")

        if include_runtime:
            lines.extend(GraphExporter._runtime_code())
            lines.append("")

        # Create the graph
        lines.append("# Agent Graph Definition")
        lines.append(f"graph_id = '{graph.id}'")
        lines.append(f"graph_name = '{graph.name}'")
        lines.append(f"graph_description = '{graph.description}'")
        lines.append("")
        lines.append("nodes = {")

        for nid, node in graph.nodes.items():
            lines.append(f"    '{nid}': {{")
            lines.append(f"        'type': '{node.type}',")
            lines.append(f"        'label': '{node.label}',")
            if node.model:
                lines.append(f"        'model': '{node.model}',")
            if node.system_prompt:
                lines.append(f"        'system_prompt': '''{node.system_prompt}''',")
            lines.append(f"        'temperature': {node.temperature},")
            lines.append(f"        'max_tokens': {node.max_tokens},")
            if node.tool_name:
                lines.append(f"        'tool_name': '{node.tool_name}',")
            if node.tool_params:
                lines.append(f"        'tool_params': {node.tool_params},")
            if node.condition_expr:
                lines.append(f"        'condition_expr': '{node.condition_expr}',")
            lines.append(f"        'max_iterations': {node.max_iterations},")
            lines.append("    },")

        lines.append("}")
        lines.append("")

        # Create edges
        lines.append("edges = [")
        for edge in graph.edges:
            label = f", 'label': '{edge.label}'" if edge.label else ""
            lines.append(f"    {{'source': '{edge.source_id}', 'target': '{edge.target_id}'{label}}},")
        lines.append("]")
        lines.append("")

        # Start node
        lines.append(f"start_node = '{graph.start_node_id}'")
        lines.append("")

        # Main execution
        lines.append("")
        lines.append("async def main():")
        lines.append("    client = httpx.AsyncClient(base_url='http://localhost:11434/v1', timeout=120.0)")
        lines.append("    try:")
        lines.append("        result = await execute_agent(client, nodes, edges, start_node,")
        lines.append("                                     initial_input=input('Enter your input: '))")
        lines.append("        print('\\n=== Result ===')")
        lines.append("        print(result)")
        lines.append("    finally:")
        lines.append("        await client.aclose()")
        lines.append("")
        lines.append("")
        lines.append('if __name__ == "__main__":')
        lines.append("    asyncio.run(main())")
        lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _runtime_code() -> list[str]:
        """Generate inline runtime code for standalone execution."""
        return [
            "# Inline Agent Runtime",
            "",
            "async def call_llm(client, model, messages, tools=None):",
            '    """Call fusion-mlx via HTTP API."""',
            "    payload = {",
            "        'model': model,",
            "        'messages': messages,",
            "        'max_tokens': 4096,",
            "        'temperature': 0.7,",
            "    }",
            "    if tools:",
            "        payload['tools'] = tools",
            "    resp = await client.post('/chat/completions', json=payload)",
            "    resp.raise_for_status()",
            "    data = resp.json()",
            "    return data['choices'][0]['message']",
            "",
            "",
            "async def execute_agent(client, nodes, edges, start_node, initial_input=''):",
            '    """Execute an agent graph."""',
            "    messages = [{'role': 'user', 'content': initial_input}]",
            "    current = start_node",
            "    max_iter = 25",
            "",
            "    for _ in range(max_iter):",
            "        if current not in nodes:",
            "            break",
            "        node = nodes[current]",
            "",
            "        if node['type'] == 'llm':",
            "            system = node.get('system_prompt', '')",
            "            msgs = []",
            "            if system:",
            "                msgs.append({'role': 'system', 'content': system})",
            "            msgs.extend(messages)",
            "",
            "            response = await call_llm(client, node.get('model', 'qwen3.5-9b'), msgs)",
            "            messages.append(response)",
            "",
            "            if response.get('tool_calls'):",
            "                for tc in response['tool_calls']:",
            "                    messages.append({",
            "                        'role': 'tool',",
            "                        'tool_call_id': tc.get('id', ''),",
            "                        'content': f\"Executed: {tc['function']['name']}\",",
            "                    })",
            "                continue",
            "",
            "            # Find next node",
            "            for e in edges:",
            "                if e['source'] == current:",
            "                    current = e['target']",
            "                    break",
            "            else:",
            "                break",
            "",
            "        elif node['type'] == 'end':",
            "            break",
            "        else:",
            "            for e in edges:",
            "                if e['source'] == current:",
            "                    current = e['target']",
            "                    break",
            "            else:",
            "                break",
            "",
            "    return messages[-1].get('content', '') if messages else ''",
        ]

    @staticmethod
    def to_json(graph: AgentGraph, indent: int = 2) -> str:
        """Export graph as JSON."""
        return graph.to_json(indent=indent)

    @staticmethod
    def to_yaml(graph: AgentGraph) -> str:
        """Export graph as YAML-like format (simple serialization)."""
        lines = []
        lines.append(f"# Agent Graph: {graph.name}")
        lines.append(f"id: {graph.id}")
        lines.append(f"name: {graph.name}")
        lines.append(f"description: {graph.description}")
        lines.append(f"version: {graph.version}")
        lines.append(f"start_node: {graph.start_node_id}")
        lines.append("")
        lines.append("nodes:")
        for nid, node in graph.nodes.items():
            lines.append(f"  {nid}:")
            lines.append(f"    type: {node.type}")
            lines.append(f"    label: \"{node.label}\"")
            if node.model:
                lines.append(f"    model: {node.model}")
            if node.system_prompt:
                # Truncate long prompts for readability
                prompt = node.system_prompt[:60].replace("\n", "\\n")
                lines.append(f"    system_prompt: \"{prompt}...\"")
            if node.tool_name:
                lines.append(f"    tool_name: {node.tool_name}")
            if node.condition_expr:
                lines.append(f"    condition_expr: \"{node.condition_expr}\"")
        lines.append("")
        lines.append("edges:")
        for edge in graph.edges:
            label = f'  label: "{edge.label}"' if edge.label else ""
            lines.append(f"  - source: {edge.source_id}")
            lines.append(f"    target: {edge.target_id}")
            if label:
                lines.append(f"    {label}")
        return "\n".join(lines)