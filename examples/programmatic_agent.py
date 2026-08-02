"""Example: Programmatic agent creation and execution using the Agent Runtime API.

This example demonstrates:
1. Creating an agent graph programmatically
2. Using the VariableManager for cross-node state
3. Using the StepDebugger for debugging
4. Using the JsonSchemaValidator for structured output
5. Exporting the graph as a Python script

Run this with a running fusion-mlx server:
    fusion-mlx serve --model qwen3.5-9b --port 11434
    python examples/programmatic_agent.py
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_runtime import AgentRuntime, AgentGraph, NodeConfig
from agent_runtime.variable_manager import VariableManager
from agent_runtime.debugger import StepDebugger
from agent_runtime.json_schema import JsonSchemaValidator
from agent_runtime.exporter import GraphExporter
from agent_runtime.templates import TemplateManager, register_default_templates
from tools import create_default_registry
from server.fusion_mlx_client import FusionMLXClient


async def example_1_basic_agent():
    """Simple agent: read a file, analyze it, save the analysis."""
    print("=== Example 1: Basic Agent ===")

    mlx = FusionMLXClient(base_url="http://localhost:11434/v1")
    registry = create_default_registry()

    # Build graph
    graph = AgentGraph(name="Code Analyzer", description="Read, analyze, and save")
    graph.add_node("start", NodeConfig(
        type="start", label="Start",
        system_prompt="You are a code analyzer. Read code and provide analysis.",
    ))
    graph.add_node("read", NodeConfig(
        type="tool", label="Read File", tool_name="file_read",
        tool_params={"path": "README.md"},
    ))
    graph.add_node("analyze", NodeConfig(
        type="llm", label="Analyze", model="qwen3.5-9b",
        temperature=0.3, max_tokens=2048,
    ))
    graph.add_node("save", NodeConfig(
        type="tool", label="Save Analysis", tool_name="file_write",
        tool_params={"path": "/tmp/analysis.md"},
    ))
    graph.add_node("end", NodeConfig(type="end", label="Done"))
    graph.add_edge("start", "read")
    graph.add_edge("read", "analyze")
    graph.add_edge("analyze", "save")
    graph.add_edge("save", "end")

    # Validate
    errors = graph.validate()
    if errors:
        print(f"Graph validation errors: {errors}")
        return

    # Execute
    runtime = AgentRuntime(mlx, registry)
    async for event in runtime.execute_graph(graph, "Analyze README.md"):
        print(f"  [{event.type.value}] {event.content[:100]}")
    print()


async def example_2_variable_manager():
    """Using VariableManager for cross-node state."""
    print("=== Example 2: Variable Manager ===")

    vm = VariableManager()
    vm.set("project_name", "Fusion-MLX Agent Studio")
    vm.set("version", "0.1.0")
    vm.set("author", "dahai80")

    # Interpolation
    template = "Project: {{ project_name }} v{{ version }} by {{ author }}"
    result = vm.interpolate(template)
    print(f"  Interpolated: {result}")

    # Nested access
    vm.set("config", {"models": {"default": "qwen3.5-9b", "max_tokens": 4096}})
    print(f"  Default model: {vm.get('config.models.default')}")
    print(f"  Max tokens: {vm.get('config.models.max_tokens')}")
    print()


async def example_3_json_schema():
    """Using JsonSchemaValidator for structured output."""
    print("=== Example 3: JSON Schema Validation ===")

    schema = {
        "type": "object",
        "required": ["name", "version", "dependencies"],
        "properties": {
            "name": {"type": "string"},
            "version": {"type": "string"},
            "dependencies": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"},
        },
    }

    validator = JsonSchemaValidator(schema)

    # Valid data
    valid_data = {
        "name": "my-project",
        "version": "1.0.0",
        "dependencies": ["httpx", "pydantic"],
    }
    errors = validator.validate(valid_data)
    print(f"  Valid data errors: {errors}")

    # Invalid data (missing required)
    invalid_data = {"name": "test"}
    errors = validator.validate(invalid_data)
    print(f"  Invalid data errors: {errors}")

    # Extract from text
    text = 'Here is the result:\n```json\n{"name": "extracted", "version": "2.0"}\n```'
    extracted = validator.extract_from_text(text)
    print(f"  Extracted from text: {extracted}")

    # Generate instruction
    instruction = validator.to_instruction()
    print(f"  Instruction (first 80 chars): {instruction[:80]}...")
    print()


async def example_4_step_debugger():
    """Using StepDebugger for debugging."""
    print("=== Example 4: Step Debugger ===")

    debugger = StepDebugger()

    # Add a breakpoint
    debugger.add_breakpoint("node_analyze")
    print(f"  Breakpoint on 'node_analyze': {debugger.has_breakpoint('node_analyze')}")

    # Simulate pausing
    await debugger.pause()
    print(f"  State after pause: {debugger.state.value}")

    await debugger.resume()
    print(f"  State after resume: {debugger.state.value}")

    # Step over
    await debugger.step_over()
    print(f"  State after step_over: {debugger.state.value}")

    debugger.remove_breakpoint("node_analyze")
    print(f"  Breakpoint after remove: {debugger.has_breakpoint('node_analyze')}")
    print()


async def example_5_export_graph():
    """Export a graph as a standalone Python script."""
    print("=== Example 5: Graph Export ===")

    graph = AgentGraph(name="Exported Agent", description="A test export")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("llm", NodeConfig(type="llm", label="Think", model="qwen3.5-9b",
                                        system_prompt="You are helpful."))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")

    # Export as Python
    python_code = GraphExporter.to_python(graph)
    print(f"  Generated {len(python_code.splitlines())} lines of Python code")
    print(f"  First 3 lines:\n    {python_code.splitlines()[0]}")
    print(f"    {python_code.splitlines()[1]}")
    print(f"    {python_code.splitlines()[2]}")
    print()


async def example_6_templates():
    """List and use preset templates."""
    print("=== Example 6: Preset Templates ===")

    register_default_templates()
    templates = TemplateManager.list()
    print(f"  Available templates ({len(templates)}):")
    for t in templates:
        print(f"    - {t['name']}: {t['description']} ({t['node_count']} nodes)")

    # Load a template
    graph = TemplateManager.get("code-assistant")
    print(f"\n  Loaded template '{graph.name}': {graph.description}")
    errors = graph.validate()
    print(f"  Validation: {'PASSED' if not errors else 'FAILED: ' + str(errors)}")
    print()


async def main():
    """Run all examples."""
    print("=" * 60)
    print("Fusion-MLX Agent Studio — API Examples")
    print("=" * 60)
    print()

    await example_1_basic_agent()
    await example_2_variable_manager()
    await example_3_json_schema()
    await example_4_step_debugger()
    await example_5_export_graph()
    await example_6_templates()

    print("=" * 60)
    print("All examples completed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())