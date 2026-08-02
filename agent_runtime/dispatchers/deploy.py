import logging
import tempfile
from pathlib import Path

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class DeployDispatcher(SubDispatcher):
    def get_handlers(self) -> dict:
        return {
            "template.list": self._handle_template_list,
            "template.get": self._handle_template_get,
            "template.instantiate": self._handle_template_instantiate,
            "deploy.export": self._handle_deploy_export,
            "deploy.import": self._handle_deploy_import,
            "deploy.list_formats": self._handle_deploy_list_formats,
        }

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
        graph_data = instantiate_template(template_id, variables=params.get("variables"))
        if not graph_data:
            return {"status": "error", "message": f"Template not found: {template_id}"}
        return {"graph_data": graph_data}

    async def _handle_deploy_export(self, params: dict) -> dict:
        from ..deployer import GraphDeployer
        graph_id = params.get("graph_id", "")
        fmt = params.get("format", "json")
        filepath = params.get("filepath", "")

        graph = self._daemon.store.load_graph(graph_id)
        if graph is None:
            return {"status": "error", "message": f"Graph not found: {graph_id}"}
        if not filepath:
            ext = {"json": ".json", "python": ".py", "yaml": ".yaml", "fastapi": ".py"}.get(fmt, ".json")
            filepath = str(Path(tempfile.gettempdir()) / f"{graph.name}{ext}")

        try:
            if fmt == "json":
                path = GraphDeployer.export_as_json(graph, filepath)
            elif fmt == "python":
                path = GraphDeployer.export_as_python(graph, filepath, with_server=params.get("with_server", True))
            elif fmt == "yaml":
                path = GraphDeployer.export_as_yaml(graph, filepath)
            elif fmt == "fastapi":
                path = GraphDeployer.export_as_fastapi(graph, filepath, port=params.get("port", 8000))
            else:
                return {"status": "error", "message": f"Unknown format: {fmt}"}
            logger.info("deploy.export: graph=%s format=%s path=%s", graph_id, fmt, path)
            return {"status": "ok", "path": str(path), "format": fmt}
        except Exception as e:
            logger.exception("deploy.export failed")
            return {"status": "error", "message": str(e)}

    async def _handle_deploy_import(self, params: dict) -> dict:
        from ..deployer import GraphDeployer
        filepath = params.get("filepath", "")
        if not filepath:
            return {"status": "error", "message": "filepath parameter required"}
        try:
            graph = GraphDeployer.import_from_json(filepath)
            self._daemon.store.save_graph(graph)
            logger.info("deploy.import: path=%s graph_id=%s", filepath, graph.id)
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
