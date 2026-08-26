"""Sub-dispatcher: DeployDispatcher."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class DeployDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "deploy.export": self._handle_deploy_export,
            "deploy.import": self._handle_deploy_import,
            "deploy.list_formats": self._handle_deploy_list_formats,
            "template.list": self._handle_template_list,
            "template.get": self._handle_template_get,
            "template.instantiate": self._handle_template_instantiate,
        }

    async def _handle_deploy_export(self, params: dict) -> dict:
        from ..deployer import GraphDeployer

        graph_id = params.get("graph_id", "")
        fmt = params.get("format", "json")
        filepath = params.get("filepath", "")

        graph = self._daemon.store.load_graph(graph_id)
        if graph is None:
            return {"status": "error", "message": f"Graph not found: {graph_id}"}
        if not filepath:
            import tempfile

            ext = {
                "json": ".json",
                "python": ".py",
                "yaml": ".yaml",
                "fastapi": ".py",
            }.get(fmt, ".json")
            filepath = str(Path(tempfile.gettempdir()) / f"{graph.name}{ext}")

        try:
            if fmt == "json":
                path = GraphDeployer.export_as_json(graph, filepath)
            elif fmt == "python":
                path = GraphDeployer.export_as_python(
                    graph, filepath, with_server=params.get("with_server", True)
                )
            elif fmt == "yaml":
                path = GraphDeployer.export_as_yaml(graph, filepath)
            elif fmt == "fastapi":
                path = GraphDeployer.export_as_fastapi(
                    graph, filepath, port=params.get("port", 11453)
                )
            else:
                return {"status": "error", "message": f"Unknown format: {fmt}"}
            logger.info(
                "deploy.export: graph=%s format=%s path=%s", graph_id, fmt, path
            )
            return {"status": "ok", "path": str(path), "format": fmt}
        except Exception as e:
            logger.exception("deploy.export failed")
            return {"status": "error", "message": str(e)}

    async def _handle_deploy_import(self, params: dict) -> dict:
        from ..deployer import GraphDeployer

        filepath = params.get("filepath", "")
        if not filepath:
            return {"status": "error", "message": "filepath parameter required"}
        # 审计 P2/dim3: deploy.import 原接任意路径 -> 路径穿越/任意文件读取.
        # 限定 import 源在 ~/.fusion-agent-studio/exports/ 内 (resolve + startswith,
        # 防 ../ 穿越逃逸). exports 目录由 deploy.export 写出, 自治闭环.
        import os

        export_root = Path(os.path.expanduser("~/.fusion-agent-studio/exports")).resolve()
        try:
            resolved = Path(filepath).resolve()
        except (OSError, ValueError) as e:
            return {"status": "error", "message": f"invalid filepath: {e}"}
        try:
            resolved.relative_to(export_root)
        except ValueError:
            logger.warning("deploy.import blocked: path=%s outside exports root", filepath)
            return {
                "status": "error",
                "message": "import path must be inside ~/.fusion-agent-studio/exports/",
            }
        try:
            graph = GraphDeployer.import_from_json(str(resolved))
            self._daemon.store.save_graph(graph)
            logger.info("deploy.import: path=%s graph_id=%s", resolved, graph.id)
            return {
                "graph_id": graph.id,
                "name": graph.name,
                "nodes": len(graph.nodes),
                "edges": len(graph.edges),
            }
        except FileNotFoundError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            logger.exception("deploy.import failed")
            return {"status": "error", "message": str(e)}

    async def _handle_deploy_list_formats(self, params: dict) -> dict:
        from ..deployer import GraphDeployer

        formats = GraphDeployer.list_formats()
        return {"formats": formats}

    # ── Agent & Marketplace lazy accessors ──

    def _get_marketplace(self):
        if self._daemon._marketplace is None:
            from ..agent_marketplace import AgentMarketplace

            self._daemon._marketplace = AgentMarketplace()
            logger.info(
                "AgentMarketplace created at %s", self._daemon._marketplace.store_dir
            )
        return self._daemon._marketplace

    def _agent_dir(self, agent_id: str) -> Path:
        return Path.home() / ".fusion-agent-studio" / "agents" / agent_id

    def _persist_agents_index(self):
        idx_path = Path.home() / ".fusion-agent-studio" / "agents" / "index.json"
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(self._daemon._agents, f, indent=4, ensure_ascii=False)
        logger.debug("Persisted agents index: %d agents", len(self._daemon._agents))

    def _load_agents_index(self):
        idx_path = Path.home() / ".fusion-agent-studio" / "agents" / "index.json"
        if idx_path.exists() and not self._daemon._agents:
            with open(idx_path, "r", encoding="utf-8") as f:
                self._daemon._agents = json.load(f)
            logger.info("Loaded agents index: %d agents", len(self._daemon._agents))

    # ── Agent handlers ──

    async def _handle_template_list(self, params: dict) -> dict:
        from ..agent_templates import list_templates

        category = params.get("category", "")
        templates = list_templates(category=category)
        return {"templates": [t.to_dict() for t in templates]}

    async def _handle_template_get(self, params: dict) -> dict:
        from ..agent_templates import get_template

        template_id = params.get("template_id", "")
        tmpl = get_template(template_id)
        if tmpl is None:
            return {"status": "error", "message": f"Template not found: {template_id}"}
        return {"template": tmpl.to_dict()}

    async def _handle_template_instantiate(self, params: dict) -> dict:
        from ..agent_templates import instantiate_template

        template_id = params.get("template_id", "")
        if not template_id:
            return {"status": "error", "message": "template_id parameter required"}
        graph_data = instantiate_template(
            template_id, variables=params.get("variables")
        )
        if not graph_data:
            return {"status": "error", "message": f"Template not found: {template_id}"}
        return {"graph_data": graph_data}

    # ── Deploy handlers ──
