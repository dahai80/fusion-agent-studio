"""Sub-dispatcher: ChatDispatcher."""

from __future__ import annotations

import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class ChatDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "chat.create": self._handle_chat_create,
            "chat.get": self._handle_chat_get,
            "chat.list": self._handle_chat_list,
            "chat.delete": self._handle_chat_delete,
            "chat.send": self._handle_chat_send,
            "chat.branch": self._handle_chat_branch,
            "chat.edit": self._handle_chat_edit,
            "chat.switch_branch": self._handle_chat_switch_branch,
            "chat.branches": self._handle_chat_branches,
            "chat.message_tree": self._handle_chat_message_tree,
            "chat.history": self._handle_chat_message_tree,
            # #274: Chat↔FSB integration (env-gated FUSION_FSB_ENABLED).
            "chat.fsb_bind": self._handle_fsb_bind,
            "chat.fsb_unbind": self._handle_fsb_unbind,
            "chat.fsb_run": self._handle_fsb_run,
            "chat.fsb_status": self._handle_fsb_status,
            "session.create": self._handle_chat_create,
            "style.list": self._handle_style_list,
            "style.get": self._handle_style_get,
            "style.create": self._handle_style_create,
            "style.apply": self._handle_style_apply,
            "style.delete": self._handle_style_delete,
        }

    async def _handle_chat_create(self, params: dict) -> dict:
        engine = self._daemon._get_chat_engine()
        session = engine.create_session(
            mode=params.get("mode", "simple"),
            title=params.get("title", ""),
            graph_id=params.get("graph_id", ""),
            metadata=params.get("metadata"),
        )
        logger.info("chat.create: id=%s mode=%s", session.id, session.mode)
        return session.to_dict()

    async def _handle_chat_get(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        engine = self._daemon._get_chat_engine()
        session = engine.get_session(session_id)
        if session is None:
            return {"status": "error", "message": f"Session {session_id} not found"}
        return session.to_dict()

    async def _handle_chat_list(self, params: dict) -> dict:
        engine = self._daemon._get_chat_engine()
        sessions = engine.list_sessions()
        return {"sessions": [s.to_dict() for s in sessions]}

    async def _handle_chat_delete(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        engine = self._daemon._get_chat_engine()
        deleted = engine.delete_session(session_id)
        return {"deleted": deleted}

    async def _handle_chat_send(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        message = params.get("message", "")
        content = params.get("content")
        mode = params.get("mode", "")
        engine = self._daemon._get_chat_engine()

        if content and isinstance(content, list):
            has_image = any(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in content
            )
            if has_image:
                vision_models = {"llava", "qwen-vl", "phi-vision", "cogvlm", "internvl"}
                model = getattr(engine, "_model", "") or ""
                is_vision = any(vm in model.lower() for vm in vision_models)
                if not is_vision:
                    return {
                        "status": "error",
                        "message": "Image input requires a vision model (e.g., llava, qwen-vl). "
                        f"Current model: {model or 'unknown'}",
                        "code": 422,
                    }

        events = []
        full_content = ""
        async for ev in engine.send(session_id, message, mode=mode, content=content):
            ev_dict = ev.to_dict()
            events.append(ev_dict)
            if ev.type.value == "token":
                full_content += ev.content
            await self._daemon._broadcast_event(
                "chat_event",
                {
                    "session_id": session_id,
                    "event": ev_dict,
                },
            )

        logger.info(
            "chat.send: session=%s events=%d content_len=%d multimodal=%s",
            session_id,
            len(events),
            len(full_content),
            bool(content),
        )
        return {"events": events, "content": full_content}

    async def _handle_chat_branch(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        message_id = params.get("message_id", "")
        engine = self._daemon._get_chat_engine()
        branched = engine.branch(session_id, message_id)
        if branched is None:
            return {"status": "error", "message": "Branch failed"}
        return branched.to_dict()

    async def _handle_chat_edit(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        message_id = params.get("message_id", "")
        new_content = params.get("content", "")
        engine = self._daemon._get_chat_engine()
        edited = engine.edit(session_id, message_id, new_content)
        if edited is None:
            return {"status": "error", "message": "Edit failed"}
        return edited.to_dict()

    async def _handle_chat_switch_branch(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        message_id = params.get("message_id", "")
        engine = self._daemon._get_chat_engine()
        ok = engine.switch_branch(session_id, message_id)
        return {
            "status": "ok" if ok else "error",
            "session_id": session_id,
            "active_branch": message_id,
        }

    async def _handle_chat_branches(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        message_id = params.get("message_id", "")
        engine = self._daemon._get_chat_engine()
        branches = engine.get_branches(session_id, message_id)
        return {"branches": branches}

    async def _handle_chat_message_tree(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        engine = self._daemon._get_chat_engine()
        tree = engine.get_message_tree(session_id)
        return tree

    async def _handle_style_list(self, params: dict) -> dict:
        mgr = self._daemon._get_style_manager()
        return {"styles": mgr.list_styles()}

    async def _handle_style_get(self, params: dict) -> dict:
        mgr = self._daemon._get_style_manager()
        style_id = params.get("style_id", "")
        result = mgr.get(style_id)
        if result is None:
            return {"status": "error", "message": f"Style not found: {style_id}"}
        return {"style": result}

    async def _handle_style_create(self, params: dict) -> dict:
        mgr = self._daemon._get_style_manager()
        name = params.get("name", "")
        if not name:
            return {"status": "error", "message": "name parameter required"}
        return mgr.create(
            name, params.get("suffix", ""), params.get("output_format", "markdown")
        )

    async def _handle_style_apply(self, params: dict) -> dict:
        mgr = self._daemon._get_style_manager()
        style_id = params.get("style_id", "")
        system_prompt = params.get("system_prompt", "")
        return mgr.apply(system_prompt, style_id)

    async def _handle_style_delete(self, params: dict) -> dict:
        mgr = self._daemon._get_style_manager()
        style_id = params.get("style_id", "")
        if not style_id:
            return {"status": "error", "message": "style_id parameter required"}
        deleted = mgr.delete(style_id)
        if not deleted:
            return {"status": "error", "message": f"Style not found or not deletable: {style_id}"}
        logger.info("style.delete: id=%s", style_id)
        return {"deleted": True, "style_id": style_id}

    # ── #274: Chat↔FSB integration (env-gated FUSION_FSB_ENABLED) ──

    async def _handle_fsb_bind(self, params: dict) -> dict:
        # Register workspace↔agent binding locally + call FSB bind upstream.
        # FSB off -> returns disabled (chat works without FSB).
        from agent_runtime.fsb_client import get_fsb_client, is_fsb_enabled
        from agent_runtime.workspace_binder import get_workspace_binder

        workspace_id = params.get("workspace_id", "")
        agent_id = params.get("agent_id", "")
        session_id = params.get("session_id", "")
        if not workspace_id or not agent_id:
            return {"status": "error", "message": "workspace_id and agent_id required"}
        binder = get_workspace_binder()
        binder.bind(workspace_id, agent_id, session_id or None)
        # Stamp chat session metadata so notify can resolve session from workspace.
        if session_id:
            engine = self._daemon._get_chat_engine()
            session = engine.get_session(session_id)
            if session is not None:
                session.metadata["fsb_workspace_id"] = workspace_id
                session.metadata["fsb_agent_id"] = agent_id
        if not is_fsb_enabled():
            return {"status": "disabled", "bound": True, "workspace_id": workspace_id}
        upstream = get_fsb_client().bind(workspace_id, agent_id)
        return {"status": "ok", "bound": True, "workspace_id": workspace_id, "upstream": upstream}

    async def _handle_fsb_unbind(self, params: dict) -> dict:
        from agent_runtime.fsb_client import get_fsb_client, is_fsb_enabled
        from agent_runtime.workspace_binder import get_workspace_binder

        workspace_id = params.get("workspace_id", "")
        if not workspace_id:
            return {"status": "error", "message": "workspace_id required"}
        binder = get_workspace_binder()
        binder.unbind(workspace_id)
        if not is_fsb_enabled():
            return {"status": "disabled", "bound": False, "workspace_id": workspace_id}
        upstream = get_fsb_client().unbind(workspace_id)
        return {"status": "ok", "bound": False, "workspace_id": workspace_id, "upstream": upstream}

    async def _handle_fsb_run(self, params: dict) -> dict:
        # NL query -> FSB intent match -> workflow run. FSB off/none matched ->
        # chat shows no-workflow. Never raises (fail-soft).
        from agent_runtime.fsb_client import get_fsb_client, is_fsb_enabled

        if not is_fsb_enabled():
            return {"status": "disabled", "matched": False}
        workspace_id = params.get("workspace_id", "")
        query = params.get("query", "")
        input_data = params.get("input_data") or {}
        if not workspace_id or not query:
            return {"status": "error", "message": "workspace_id and query required"}
        result = get_fsb_client().chat_run(workspace_id, query, input_data)
        if result is None:
            return {"status": "error", "matched": False, "message": "fsb unreachable"}
        return {"status": "ok", **result}

    async def _handle_fsb_status(self, params: dict) -> dict:
        from agent_runtime.fsb_client import is_fsb_enabled
        from agent_runtime.workspace_binder import get_workspace_binder

        binder = get_workspace_binder()
        return {
            "enabled": is_fsb_enabled(),
            "bindings": binder.list(),
        }

    # ── Alert handlers ──
