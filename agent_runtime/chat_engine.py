"""Unified Chat Engine — single chat experience with branching, editing, and multi-mode support."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import AgentRuntime
    from .persistence import AgentStore
    from .graph import AgentGraph

logger = logging.getLogger(__name__)


class ChatEventType(str, Enum):
    TOKEN = "token"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    AGENT_HANDOFF = "agent_handoff"
    THINKING = "thinking"
    DONE = "done"
    ERROR = "error"


class ChatMode(str, Enum):
    SIMPLE = "simple"
    AGENT = "agent"
    CODE = "code"
    DESIGN = "design"
    RAG = "rag"


@dataclass
class ChatEvent:
    type: ChatEventType
    content: str = ""
    name: str = ""
    args: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "content": self.content,
            "name": self.name,
            "args": self.args,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ChatEvent:
        return cls(
            type=ChatEventType(data["type"]),
            content=data.get("content", ""),
            name=data.get("name", ""),
            args=data.get("args", {}),
            metadata=data.get("metadata", {}),
            timestamp=data.get("timestamp", 0.0),
        )


@dataclass
class ChatMessage:
    id: str = ""
    role: str = "user"
    content: str | list[dict] = ""
    mode: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    tool_call_id: str = ""
    parent_id: str = ""
    children_ids: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    created_at: float = 0.0

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:16]
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "mode": self.mode,
            "tool_calls": self.tool_calls,
            "tool_call_id": self.tool_call_id,
            "parent_id": self.parent_id,
            "children_ids": self.children_ids,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ChatMessage:
        return cls(
            id=data.get("id", ""),
            role=data.get("role", "user"),
            content=data.get("content", ""),
            mode=data.get("mode", ""),
            tool_calls=data.get("tool_calls", []),
            tool_call_id=data.get("tool_call_id", ""),
            parent_id=data.get("parent_id", ""),
            children_ids=data.get("children_ids", []),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", 0.0),
        )


@dataclass
class ChatSession:
    id: str = ""
    title: str = ""
    mode: str = ChatMode.SIMPLE.value
    messages: list[ChatMessage] = field(default_factory=list)
    active_branch: str = ""
    graph_id: str = ""
    metadata: dict = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:16]
        if not self.created_at:
            self.created_at = time.time()
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.active_branch and self.messages:
            self.active_branch = self._root_message_id() or ""

    def _root_message_id(self) -> str:
        for m in self.messages:
            if not m.parent_id:
                return m.id
        return ""

    def get_message(self, message_id: str) -> ChatMessage | None:
        for m in self.messages:
            if m.id == message_id:
                return m
        return None

    def get_linear_branch(self, leaf_id: str = "") -> list[ChatMessage]:
        if not self.messages:
            return []
        leaf = leaf_id or self.active_branch
        if not leaf:
            return list(self.messages)
        chain: list[ChatMessage] = []
        current = self.get_message(leaf)
        while current:
            chain.append(current)
            if not current.parent_id:
                break
            current = self.get_message(current.parent_id)
        chain.reverse()
        return chain

    def add_message(self, message: ChatMessage, parent_id: str = "") -> None:
        if parent_id:
            message.parent_id = parent_id
            parent = self.get_message(parent_id)
            if parent and message.id not in parent.children_ids:
                parent.children_ids.append(message.id)
        if not message.parent_id and self.messages:
            roots = [m for m in self.messages if not m.parent_id]
            if roots:
                message.parent_id = roots[-1].id
                roots[-1].children_ids.append(message.id)
        self.messages.append(message)
        self.active_branch = message.id
        self.updated_at = time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "mode": self.mode,
            "messages": [m.to_dict() for m in self.messages],
            "active_branch": self.active_branch,
            "graph_id": self.graph_id,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ChatSession:
        messages = [ChatMessage.from_dict(m) for m in data.get("messages", [])]
        return cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            mode=data.get("mode", ChatMode.SIMPLE.value),
            messages=messages,
            active_branch=data.get("active_branch", ""),
            graph_id=data.get("graph_id", ""),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
        )


class ChatEngine:
    MAX_CACHED_SESSIONS = 128

    def __init__(
        self,
        runtime: "AgentRuntime | None" = None,
        store: "AgentStore | None" = None,
    ):
        self.runtime = runtime
        self.store = store
        self._sessions: dict[str, ChatSession] = {}
        logger.info("ChatEngine init, runtime=%s, store=%s",
                     "provided" if runtime else "none",
                     "provided" if store else "none")

    def create_session(
        self,
        mode: str = ChatMode.SIMPLE.value,
        title: str = "",
        graph_id: str = "",
        metadata: dict | None = None,
    ) -> ChatSession:
        session = ChatSession(
            mode=mode,
            title=title or f"Chat {time.strftime('%H:%M')}",
            graph_id=graph_id,
            metadata=metadata or {},
        )
        self._sessions[session.id] = session
        self._evict_sessions()
        self._persist_session(session)
        logger.info("ChatEngine create_session id=%s mode=%s", session.id, mode)
        return session

    def get_session(self, session_id: str) -> ChatSession | None:
        if session_id in self._sessions:
            return self._sessions[session_id]
        if self.store:
            loaded = self.store.load_chat_session(session_id)
            if loaded:
                self._sessions[session_id] = loaded
                self._evict_sessions()
                return loaded
        return None

    def list_sessions(self) -> list[ChatSession]:
        if self.store:
            stored = self.store.list_chat_sessions()
            for s in stored:
                if s.id not in self._sessions:
                    self._sessions[s.id] = s
        return sorted(self._sessions.values(), key=lambda s: s.updated_at, reverse=True)

    def delete_session(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if self.store:
            self.store.delete_chat_session(session_id)
        existed = session is not None
        logger.info("ChatEngine delete_session id=%s existed=%s", session_id, existed)
        return existed

    def _evict_sessions(self) -> None:
        if len(self._sessions) <= self.MAX_CACHED_SESSIONS:
            return
        sorted_sessions = sorted(self._sessions.values(), key=lambda s: s.updated_at)
        to_remove = len(self._sessions) - self.MAX_CACHED_SESSIONS
        for s in sorted_sessions[:to_remove]:
            self._sessions.pop(s.id, None)
        logger.info("Evicted %d inactive sessions from cache", to_remove)

    async def send(
        self,
        session_id: str,
        message: str,
        mode: str = "",
        content: list[dict] | None = None,
    ) -> AsyncIterator[ChatEvent]:
        session = self.get_session(session_id)
        if not session:
            yield ChatEvent(type=ChatEventType.ERROR, content=f"Session {session_id} not found")
            return

        effective_mode = mode or session.mode
        msg_content: str | list[dict] = content if content else message
        user_msg = ChatMessage(role="user", content=msg_content, mode=effective_mode)
        parent_id = session.active_branch
        session.add_message(user_msg, parent_id=parent_id)

        logger.info("ChatEngine send session=%s mode=%s msg_len=%d", session_id, effective_mode, len(message))

        assistant_msg = ChatMessage(role="assistant", content="", mode=effective_mode)
        session.add_message(assistant_msg, parent_id=user_msg.id)

        full_content = ""

        if effective_mode == ChatMode.AGENT.value and self.runtime:
            async for event in self._execute_agent(session, message):
                if event.type == ChatEventType.TOKEN:
                    full_content += event.content
                    assistant_msg.content = full_content
                elif event.type == ChatEventType.TOOL_CALL:
                    assistant_msg.tool_calls.append(event.args)
                yield event
        elif effective_mode == ChatMode.RAG.value and self.runtime:
            async for event in self._execute_rag(session, message):
                if event.type == ChatEventType.TOKEN:
                    full_content += event.content
                    assistant_msg.content = full_content
                yield event
        elif effective_mode == ChatMode.CODE.value and self.runtime:
            async for event in self._execute_code(session, message):
                if event.type == ChatEventType.TOKEN:
                    full_content += event.content
                    assistant_msg.content = full_content
                yield event
        else:
            async for event in self._execute_simple(session, message):
                if event.type == ChatEventType.TOKEN:
                    full_content += event.content
                    assistant_msg.content = full_content
                yield event

        assistant_msg.content = full_content
        if not session.title and full_content:
            session.title = full_content[:60]
        self._persist_session(session)
        yield ChatEvent(type=ChatEventType.DONE)

    def branch(self, session_id: str, message_id: str) -> ChatSession | None:
        session = self.get_session(session_id)
        if not session:
            logger.warning("ChatEngine branch: session %s not found", session_id)
            return None
        original = session.get_message(message_id)
        if not original:
            logger.warning("ChatEngine branch: message %s not found", message_id)
            return None

        branched = ChatSession(
            mode=session.mode,
            title=f"{session.title} (branch)",
            graph_id=session.graph_id,
            metadata={"branched_from": session_id, "branched_at": message_id},
        )
        prefix = session.get_linear_branch(message_id)
        for msg in prefix:
            new_msg = ChatMessage(
                id=uuid.uuid4().hex[:16],
                role=msg.role,
                content=msg.content,
                mode=msg.mode,
                tool_calls=list(msg.tool_calls),
                tool_call_id=msg.tool_call_id,
                metadata=dict(msg.metadata),
            )
            branched.add_message(new_msg, parent_id=branched.active_branch if branched.messages else "")

        self._sessions[branched.id] = branched
        self._persist_session(branched)
        logger.info("ChatEngine branch from session=%s msg=%s -> new=%s", session_id, message_id, branched.id)
        return branched

    def edit(self, session_id: str, message_id: str, new_content: str) -> ChatMessage | None:
        session = self.get_session(session_id)
        if not session:
            logger.warning("ChatEngine edit: session %s not found", session_id)
            return None
        original = session.get_message(message_id)
        if not original:
            logger.warning("ChatEngine edit: message %s not found", message_id)
            return None
        if original.role != "user":
            logger.warning("ChatEngine edit: can only edit user messages, got role=%s", original.role)
            return None

        edited = ChatMessage(
            role="user",
            content=new_content,
            mode=original.mode,
            parent_id=original.parent_id,
            metadata={"edited_from": message_id},
        )
        original.children_ids.append(edited.id)
        session.messages.append(edited)
        session.active_branch = edited.id
        self._persist_session(session)
        logger.info("ChatEngine edit session=%s msg=%s -> new=%s", session_id, message_id, edited.id)
        return edited

    def switch_branch(self, session_id: str, message_id: str) -> bool:
        session = self.get_session(session_id)
        if not session:
            logger.warning("ChatEngine switch_branch: session %s not found", session_id)
            return False
        msg = session.get_message(message_id)
        if not msg:
            logger.warning("ChatEngine switch_branch: message %s not found", message_id)
            return False
        chain = session.get_linear_branch(message_id)
        if not chain:
            return False
        session.active_branch = message_id
        self._persist_session(session)
        logger.info("ChatEngine switch_branch session=%s -> msg=%s", session_id, message_id)
        return True

    def get_branches(self, session_id: str, message_id: str = "") -> list[dict[str, Any]]:
        session = self.get_session(session_id)
        if not session:
            return []
        target_id = message_id or session.active_branch
        target = session.get_message(target_id)
        if not target:
            return []
        parent = session.get_message(target.parent_id) if target.parent_id else None
        if not parent:
            roots = [m for m in session.messages if not m.parent_id]
            siblings = roots
        else:
            siblings = [session.get_message(cid) for cid in parent.children_ids]
            siblings = [s for s in siblings if s is not None]
        branches = []
        for sib in siblings:
            branch_msg = session.get_linear_branch(sib.id)
            branches.append({
                "leaf_id": sib.id,
                "content_preview": sib.content[:100] if sib.content else "",
                "role": sib.role,
                "created_at": sib.created_at,
                "message_count": len(branch_msg),
                "is_active": sib.id == session.active_branch,
            })
        return branches

    def get_message_tree(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        if not session:
            return {"nodes": [], "active_branch": ""}
        nodes = []
        for msg in session.messages:
            nodes.append({
                "id": msg.id,
                "role": msg.role,
                "content_preview": msg.content[:80] if msg.content else "",
                "parent_id": msg.parent_id,
                "children_ids": msg.children_ids,
                "created_at": msg.created_at,
            })
        return {
            "nodes": nodes,
            "active_branch": session.active_branch,
            "total_messages": len(session.messages),
        }

    async def _execute_simple(
        self,
        session: ChatSession,
        message: str,
    ) -> AsyncIterator[ChatEvent]:
        if not self.runtime or not self.runtime.llm_gateway:
            yield ChatEvent(type=ChatEventType.TOKEN, content="[no LLM available]")
            return

        history = self._build_llm_history(session)
        try:
            if self.runtime.mlx:
                async for chunk in self.runtime.llm_gateway.chat_stream(
                    messages=history,
                    model="default",
                ):
                    if chunk.get("delta_content"):
                        yield ChatEvent(type=ChatEventType.TOKEN, content=chunk["delta_content"])
                    if chunk.get("delta_tool_calls"):
                        for tc in chunk["delta_tool_calls"]:
                            yield ChatEvent(
                                type=ChatEventType.TOOL_CALL,
                                name=tc.get("function", {}).get("name", ""),
                                args=tc,
                            )
            else:
                resp = await self.runtime.llm_gateway.chat(messages=history, model="default")
                if resp.content:
                    yield ChatEvent(type=ChatEventType.TOKEN, content=resp.content)
        except Exception as e:
            logger.error("ChatEngine _execute_simple error: %s", e)
            yield ChatEvent(type=ChatEventType.ERROR, content=str(e))

    async def _execute_agent(
        self,
        session: ChatSession,
        message: str,
    ) -> AsyncIterator[ChatEvent]:
        if not self.runtime:
            yield ChatEvent(type=ChatEventType.ERROR, content="no runtime for agent mode")
            return

        graph_id = session.graph_id
        graph = None
        if graph_id and self.runtime.store:
            graph = self.runtime.store.load_graph(graph_id)

        if not graph:
            from .graph import AgentGraph, NodeConfig, NodeType
            graph = AgentGraph(name=f"chat-agent-{session.id}")
            graph.add_node("start", NodeConfig(type=NodeType.START, system_prompt="You are a helpful assistant."))
            graph.add_node("llm", NodeConfig(type=NodeType.LLM, model="default"))
            graph.add_node("end", NodeConfig(type=NodeType.END))
            graph.add_edge("start", "llm")
            graph.add_edge("llm", "end")

        try:
            from .context import AgentEventType, AgentContext
            # 预加载会话历史，避免 agent 模式丢失上下文 (bug2/3/4)
            # send() 已追加新 user 消息 + 空 assistant 消息，排除最后两条，
            # 否则与 initial_input=message 重复
            history = self._build_llm_history(session)
            if len(history) >= 2:
                history = history[:-2]
            else:
                history = []
            ctx = AgentContext()
            for msg in history:
                ctx.add_message(
                    msg.get("role", "user"),
                    msg.get("content", ""),
                    tool_calls=msg.get("tool_calls"),
                    tool_call_id=msg.get("tool_call_id", ""),
                )
            logger.info(
                "ChatEngine _execute_agent session=%s preloaded %d history msgs",
                session.id, len(history),
            )
            async for event in self.runtime.execute_graph_stream(
                graph, initial_input=message, context=ctx
            ):
                if event.type == AgentEventType.TOKEN:
                    yield ChatEvent(type=ChatEventType.TOKEN, content=event.content)
                elif event.type == AgentEventType.THINKING_TOKEN:
                    yield ChatEvent(type=ChatEventType.THINKING, content=event.content)
                elif event.type == AgentEventType.TOOL_CALL or event.type == AgentEventType.TOOL_CALL_START:
                    yield ChatEvent(
                        type=ChatEventType.TOOL_CALL,
                        name=event.name,
                        args=event.args,
                    )
                elif event.type == AgentEventType.TOOL_RESULT or event.type == AgentEventType.TOOL_CALL_END:
                    yield ChatEvent(
                        type=ChatEventType.TOOL_RESULT,
                        name=event.name,
                        content=event.content,
                        args=event.args,
                    )
                elif event.type == AgentEventType.ERROR:
                    yield ChatEvent(type=ChatEventType.ERROR, content=event.content)
        except Exception as e:
            logger.error("ChatEngine _execute_agent error: %s", e)
            yield ChatEvent(type=ChatEventType.ERROR, content=str(e))

    async def _execute_rag(
        self,
        session: ChatSession,
        message: str,
    ) -> AsyncIterator[ChatEvent]:
        if not self.runtime:
            yield ChatEvent(type=ChatEventType.ERROR, content="no runtime for RAG mode")
            return

        context_text = ""
        if hasattr(self.runtime, "knowledge_engine") and self.runtime.knowledge_engine:
            import asyncio
            results = await asyncio.to_thread(self.runtime.knowledge_engine.search, message, top_k=5)
            context_text = "\n".join(r.get("content", "") for r in results)
            logger.info("ChatEngine _execute_rag: %d results from knowledge_engine", len(results))

        rag_prompt = f"Based on the following context, answer the question.\n\nContext:\n{context_text}\n\nQuestion: {message}" if context_text else message
        async for event in self._execute_simple(session, rag_prompt):
            yield event

    async def _execute_code(
        self,
        session: ChatSession,
        message: str,
    ) -> AsyncIterator[ChatEvent]:
        code_prompt = (
            f"You are a code assistant. Help with coding tasks.\n\n"
            f"User request: {message}"
        )
        async for event in self._execute_simple(session, code_prompt):
            yield event

    def _build_llm_history(self, session: ChatSession) -> list[dict]:
        branch = session.get_linear_branch()
        history: list[dict] = []
        for msg in branch:
            entry: dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            history.append(entry)
        return history

    def _persist_session(self, session: ChatSession) -> None:
        if self.store:
            try:
                self.store.save_chat_session(session)
            except Exception as e:
                logger.error("ChatEngine persist session %s failed: %s", session.id, e)
