"""Sub-dispatcher: WorkflowDispatcher."""
from __future__ import annotations
import logging
from typing import Any
from .base import SubDispatcher

logger = logging.getLogger(__name__)


class WorkflowDispatcher(SubDispatcher):
    async def _handle_workflow_create(self, params: dict) -> dict:
        engine = self._daemon._get_workflow_engine()
        name = params.get("name", "Untitled Workflow")
        phases = params.get("phases", [])
        wf = engine.create_workflow(
            name=name,
            phases=phases,
            input_schema=params.get("input_schema", {}),
            output_schema=params.get("output_schema", {}),
            metadata=params.get("metadata", {}),
        )
        logger.info("workflow.create: name=%s id=%s phases=%d", name, wf.id, len(wf.phases))
        return wf.to_dict()

    async def _handle_workflow_execute(self, params: dict) -> dict:
        engine = self._daemon._get_workflow_engine()
        workflow_id = params.get("workflow_id", "")
        initial_input = params.get("input", "")
        budget = params.get("budget")
        run = await engine.execute_workflow(workflow_id, initial_input, budget)
        logger.info("workflow.execute: workflow=%s run=%s status=%s", workflow_id, run.id, run.status.value)
        return run.to_dict()

    async def _handle_workflow_pause(self, params: dict) -> dict:
        engine = self._daemon._get_workflow_engine()
        run_id = params.get("run_id", "")
        ok = engine.pause_run(run_id)
        logger.info("workflow.pause: run=%s ok=%s", run_id, ok)
        return {"run_id": run_id, "paused": ok}

    async def _handle_workflow_resume(self, params: dict) -> dict:
        engine = self._daemon._get_workflow_engine()
        run_id = params.get("run_id", "")
        ok = engine.resume_run(run_id)
        logger.info("workflow.resume: run=%s ok=%s", run_id, ok)
        return {"run_id": run_id, "resumed": ok}

    async def _handle_workflow_cancel(self, params: dict) -> dict:
        engine = self._daemon._get_workflow_engine()
        run_id = params.get("run_id", "")
        ok = engine.cancel_run(run_id)
        logger.info("workflow.cancel: run=%s ok=%s", run_id, ok)
        return {"run_id": run_id, "cancelled": ok}

    async def _handle_workflow_status(self, params: dict) -> dict:
        engine = self._daemon._get_workflow_engine()
        run_id = params.get("run_id", "")
        run = engine.get_run_status(run_id)
        if not run:
            raise ValueError(f"Workflow run not found: {run_id}")
        return run.to_dict()

    async def _handle_workflow_list(self, params: dict) -> dict:
        engine = self._daemon._get_workflow_engine()
        workflow_id = params.get("workflow_id")
        if workflow_id:
            runs = engine.list_runs(workflow_id)
            return {"runs": [r.to_dict() for r in runs]}
        workflows = engine.list_workflows()
        return {"workflows": [w.to_dict() for w in workflows]}

    async def _handle_workflow_get(self, params: dict) -> dict:
        engine = self._daemon._get_workflow_engine()
        workflow_id = params.get("workflow_id", "")
        wf = engine.get_workflow(workflow_id)
        if not wf:
            raise ValueError(f"Workflow not found: {workflow_id}")
        return wf.to_dict()

    async def _handle_workflow_delete(self, params: dict) -> dict:
        engine = self._daemon._get_workflow_engine()
        workflow_id = params.get("workflow_id", "")
        ok = engine.delete_workflow(workflow_id)
        logger.info("workflow.delete: workflow=%s ok=%s", workflow_id, ok)
        return {"workflow_id": workflow_id, "deleted": ok}

    async def _handle_langgraph_create(self, params: dict) -> dict:
        from .langgraph_engine import WorkflowDefinition
        engine = self._daemon._get_langgraph_engine()
        wf = WorkflowDefinition.from_dict(params)
        result = engine.create_workflow(wf)
        logger.info("langgraph.create: wf_id=%s name=%s", result.get("wf_id", ""), params.get("name", ""))
        return result

    async def _handle_langgraph_get(self, params: dict) -> dict:
        wf_id = params.get("wf_id", "")
        engine = self._daemon._get_langgraph_engine()
        return engine.get_workflow(wf_id)

    async def _handle_langgraph_list(self, params: dict) -> dict:
        engine = self._daemon._get_langgraph_engine()
        workflows = engine.list_workflows()
        return {"workflows": workflows}

    async def _handle_langgraph_delete(self, params: dict) -> dict:
        wf_id = params.get("wf_id", "")
        engine = self._daemon._get_langgraph_engine()
        return engine.delete_workflow(wf_id)

    async def _handle_langgraph_run(self, params: dict) -> dict:
        wf_id = params.get("wf_id", "")
        trigger_type = params.get("trigger_type", "manual")
        input_data = params.get("input_data")
        engine = self._daemon._get_langgraph_engine()
        result = await engine.run_workflow(wf_id, trigger_type=trigger_type, input_data=input_data)
        logger.info("langgraph.run: wf_id=%s status=%s", wf_id, result.get("status"))
        return result

    async def _handle_langgraph_approve(self, params: dict) -> dict:
        run_id = params.get("run_id", "")
        action = params.get("action", "approve")
        reviewer = params.get("reviewer", "")
        comment = params.get("comment", "")
        engine = self._daemon._get_langgraph_engine()
        result = engine.approve_run(run_id, action=action, reviewer=reviewer, comment=comment)
        logger.info("langgraph.approve: run_id=%s action=%s", run_id, action)
        return result

    async def _handle_langgraph_cancel(self, params: dict) -> dict:
        run_id = params.get("run_id", "")
        engine = self._daemon._get_langgraph_engine()
        return engine.cancel_run(run_id)

    async def _handle_langgraph_get_run(self, params: dict) -> dict:
        run_id = params.get("run_id", "")
        engine = self._daemon._get_langgraph_engine()
        return engine.get_run(run_id)

    # ── Artifact handlers (#32, #33, #34) ──

    async def _handle_artifact_create(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        name = params.get("name", "")
        artifact_type = params.get("artifact_type", "document")
        content = params.get("content", "")
        metadata = params.get("metadata")
        mgr = self._daemon._get_artifact_manager()
        return mgr.create_artifact(agent_id, name, artifact_type=artifact_type, content=content, metadata=metadata)

    async def _handle_artifact_update(self, params: dict) -> dict:
        artifact_id = params.get("artifact_id", "")
        agent_id = params.get("agent_id", "")
        content = params.get("content")
        metadata = params.get("metadata")
        mgr = self._daemon._get_artifact_manager()
        return mgr.update_artifact(artifact_id, agent_id, content=content, metadata=metadata)

    async def _handle_artifact_search(self, params: dict) -> dict:
        query = params.get("query", "")
        artifact_type = params.get("artifact_type", "")
        owner_agent_id = params.get("owner_agent_id", "")
        mgr = self._daemon._get_artifact_manager()
        results = mgr.search_artifacts(query=query, artifact_type=artifact_type, owner_agent_id=owner_agent_id)
        return {"results": results}

    async def _handle_artifact_get(self, params: dict) -> dict:
        artifact_id = params.get("artifact_id", "")
        mgr = self._daemon._get_artifact_manager()
        return mgr.get_artifact(artifact_id)

    async def _handle_artifact_list(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        mgr = self._daemon._get_artifact_manager()
        artifacts = mgr.list_artifacts(agent_id=agent_id)
        return {"artifacts": artifacts}

    async def _handle_artifact_delete(self, params: dict) -> dict:
        artifact_id = params.get("artifact_id", "")
        agent_id = params.get("agent_id", "")
        mgr = self._daemon._get_artifact_manager()
        return mgr.delete_artifact(artifact_id, agent_id=agent_id)

    async def _handle_artifact_export(self, params: dict) -> dict:
        artifact_id = params.get("artifact_id", "")
        mgr = self._daemon._get_artifact_manager()
        return mgr.export_artifact(artifact_id)

    async def _handle_artifact_context(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        limit = params.get("limit", 5)
        mgr = self._daemon._get_artifact_manager()
        context = mgr.get_active_artifacts_context(agent_id, limit=limit)
        return {"context": context}

    # ── #53 RPC handlers: model.status, kb.*, audit.list, system.*, agent.diff_review, permission.* ──
