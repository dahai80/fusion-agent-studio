"""Sub-dispatcher: ChatDispatcher."""
from __future__ import annotations
import logging
from typing import Any
from .base import SubDispatcher

logger = logging.getLogger(__name__)


class ChatDispatcher(SubDispatcher):
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
            await self._broadcast_event("chat_event", {
                "session_id": session_id,
                "event": ev_dict,
            })

        logger.info("chat.send: session=%s events=%d content_len=%d multimodal=%s",
                     session_id, len(events), len(full_content), bool(content))
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
        return {"status": "ok" if ok else "error", "session_id": session_id, "active_branch": message_id}

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
        return mgr.create(name, params.get("suffix", ""), params.get("output_format", "markdown"))

    async def _handle_style_apply(self, params: dict) -> dict:
        mgr = self._daemon._get_style_manager()
        style_id = params.get("style_id", "")
        system_prompt = params.get("system_prompt", "")
        return mgr.apply(system_prompt, style_id)

    # ── Alert handlers ──
