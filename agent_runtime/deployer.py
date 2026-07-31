"""Deployer — export and deploy agent graphs as standalone services."""
from __future__ import annotations

from pathlib import Path

from .graph import AgentGraph


class GraphDeployer:
    """Export agent graphs as standalone deployable artifacts."""

    @staticmethod
    def export_as_json(graph: AgentGraph, filepath: str | Path) -> Path:
        """Export graph as a JSON file."""
        path = Path(filepath).expanduser().resolve()
        path.write_text(graph.to_json(indent=2), encoding="utf-8")
        return path

    @staticmethod
    def export_as_python(graph: AgentGraph, filepath: str | Path, with_server: bool = True) -> Path:
        """Export graph as a standalone Python script."""
        from .exporter import GraphExporter
        path = Path(filepath).expanduser().resolve()
        code = GraphExporter.to_python(graph, include_runtime=True)
        path.write_text(code, encoding="utf-8")
        path.chmod(0o755)
        return path

    @staticmethod
    def export_as_yaml(graph: AgentGraph, filepath: str | Path) -> Path:
        """Export graph as a YAML-like config file."""
        from .exporter import GraphExporter
        path = Path(filepath).expanduser().resolve()
        path.write_text(GraphExporter.to_yaml(graph), encoding="utf-8")
        return path

    @staticmethod
    def export_as_fastapi(graph: AgentGraph, filepath: str | Path, port: int = 8000) -> Path:
        """Export graph as a FastAPI server that can be run independently."""
        llm_model = graph.find_llm_model() or "qwen3.5-9b"
        code = f'''#!/usr/bin/env python3
"""Auto-deployed agent: {graph.name}"""
import asyncio
import json
import httpx
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="{graph.name}", description="{graph.description}")

class Query(BaseModel):
    input: str = ""
    model: str = "{llm_model}"

@app.post("/run")
async def run_agent(query: Query):
    async with httpx.AsyncClient(base_url="http://localhost:{port}/v1", timeout=120.0) as client:
        messages = [{{"role": "user", "content": query.input}}]
        resp = await client.post("/chat/completions", json={{"model": query.model, "messages": messages}})
        resp.raise_for_status()
        data = resp.json()
        return {{"output": data["choices"][0]["message"]["content"]}}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port={port + 1})
'''
        path = Path(filepath).expanduser().resolve()
        path.write_text(code, encoding="utf-8")
        path.chmod(0o755)
        return path

    @staticmethod
    def import_from_json(filepath: str | Path) -> AgentGraph:
        """Import a graph from a JSON file."""
        path = Path(filepath).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return AgentGraph.from_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def list_formats() -> list[dict[str, str]]:
        return [
            {"name": "json", "extension": ".json", "description": "Portable JSON format"},
            {"name": "python", "extension": ".py", "description": "Standalone Python script"},
            {"name": "yaml", "extension": ".yaml", "description": "YAML-like config"},
            {"name": "fastapi", "extension": ".py", "description": "FastAPI microservice"},
        ]