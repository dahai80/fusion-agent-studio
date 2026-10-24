from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_FS_DIR = os.path.join(os.path.expanduser("~"), ".fusion-agent-studio", "sessions")


class SessionStore:
    def list(self, limit: int = 50) -> list[dict]:
        raise NotImplementedError

    def get(self, session_id: str) -> dict | None:
        raise NotImplementedError

    def put(self, session: dict) -> dict:
        raise NotImplementedError

    def delete(self, session_id: str) -> bool:
        raise NotImplementedError

    def fork(self, session_id: str, title: str = "") -> dict | None:
        raise NotImplementedError


class SQLiteSessionStore(SessionStore):
    def __init__(self, store):
        if store is None:
            from .persistence import AgentStore

            store = AgentStore()
        self.store = store
        logger.info("SQLiteSessionStore ready db=%s", getattr(store, "db_path", "?"))

    def list(self, limit: int = 50) -> list[dict]:
        return [s.to_dict() for s in self.store.list_chat_sessions(limit=limit)]

    def get(self, session_id: str) -> dict | None:
        session = self.store.load_chat_session(session_id)
        return session.to_dict() if session is not None else None

    def put(self, session: dict) -> dict:
        from .chat_engine import ChatSession

        chat = ChatSession.from_dict(session)
        self.store.save_chat_session(chat)
        logger.info("session put id=%s messages=%d", chat.id, len(chat.messages))
        return chat.to_dict()

    def delete(self, session_id: str) -> bool:
        return bool(self.store.delete_chat_session(session_id))

    def fork(self, session_id: str, title: str = "") -> dict | None:
        from .chat_engine import ChatSession

        parent = self.store.load_chat_session(session_id)
        if parent is None:
            return None
        data = parent.to_dict()
        forked = ChatSession.from_dict(data)
        forked.id = f"chat-{int(time.time() * 1000):x}"
        forked.title = title or f"{parent.title} (fork)"
        forked.metadata = {**(parent.metadata or {}), "forked_from": session_id}
        forked.updated_at = time.time()
        self.store.save_chat_session(forked)
        logger.info("session forked from=%s to=%s", session_id, forked.id)
        return forked.to_dict()


class FileSessionStore(SessionStore):
    def __init__(self, base_dir: str = ""):
        self.base_dir = Path(base_dir or os.environ.get("FUSION_SESSION_STORE_DIR", DEFAULT_FS_DIR))
        self.base_dir.mkdir(parents=True, exist_ok=True)
        logger.info("FileSessionStore ready dir=%s", self.base_dir)

    def _path(self, session_id: str) -> Path:
        if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
            raise ValueError(f"invalid session id: {session_id!r}")
        return self.base_dir / f"{session_id}.json"

    def list(self, limit: int = 50) -> list[dict]:
        rows: list[dict] = []
        for path in self.base_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                logger.warning("session read failed path=%s: %s", path, e)
                continue
            data.pop("messages", None)
            rows.append(data)
        rows.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return rows[: int(limit)]

    def get(self, session_id: str) -> dict | None:
        try:
            path = self._path(session_id)
        except ValueError as e:
            logger.warning("session get rejected: %s", e)
            return None
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("session get failed id=%s: %s", session_id, e)
            return None

    def put(self, session: dict) -> dict:
        session_id = str(session.get("id", "") or "")
        path = self._path(session_id)
        payload = dict(session)
        payload["updated_at"] = time.time()
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        logger.info(
            "session put id=%s messages=%d backend=file",
            session_id,
            len(payload.get("messages") or []),
        )
        return payload

    def delete(self, session_id: str) -> bool:
        try:
            path = self._path(session_id)
        except ValueError:
            return False
        if not path.exists():
            return False
        path.unlink()
        logger.info("session delete id=%s backend=file", session_id)
        return True

    def fork(self, session_id: str, title: str = "") -> dict | None:
        parent = self.get(session_id)
        if parent is None:
            return None
        forked = dict(parent)
        forked["id"] = f"chat-{int(time.time() * 1000):x}"
        forked["title"] = title or f"{parent.get('title', 'session')} (fork)"
        forked["metadata"] = {
            **(parent.get("metadata") or {}),
            "forked_from": session_id,
        }
        return self.put(forked)


class ClusterSessionStore(SessionStore):
    """#450: shared cluster store — fusion-multi-node master is the single
    authority; any node hydrates/writes sessions over its Bearer-authed
    /api/v1/session-store API (semantics mirror this adapter 1:1)."""

    def __init__(self, base_url: str = "", token: str = ""):
        from .sandbox_router import cluster_url

        self.base_url = (base_url or cluster_url()).rstrip("/")
        self.token = token or os.environ.get("FUSION_CLUSTER_TOKEN", "")
        logger.info("ClusterSessionStore ready base=%s auth=%s", self.base_url, "bearer" if self.token else "none")

    def _headers(self) -> dict:
        headers = {"content-type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, json_body: dict | None = None,
                 params: dict | None = None):
        import httpx

        url = f"{self.base_url}{path}"
        try:
            resp = httpx.request(method, url, json=json_body, params=params,
                                 headers=self._headers(), timeout=10.0)
        except Exception as e:
            logger.error("cluster session store unreachable method=%s url=%s: %s", method, url, e)
            raise RuntimeError(f"cluster session store unreachable: {e}") from e
        if resp.status_code >= 500:
            logger.error("cluster session store error method=%s url=%s status=%s body=%s",
                         method, url, resp.status_code, resp.text[:200])
            raise RuntimeError(f"cluster session store {url} returned {resp.status_code}")
        return resp

    def list(self, limit: int = 50) -> list[dict]:
        resp = self._request("GET", "/api/v1/session-store", params={"limit": int(limit)})
        resp.raise_for_status()
        sessions = list(resp.json().get("sessions") or [])
        logger.info("session list backend=cluster count=%d", len(sessions))
        return sessions

    def get(self, session_id: str) -> dict | None:
        resp = self._request("GET", f"/api/v1/session-store/{session_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        session = resp.json().get("session")
        logger.info("session get id=%s backend=cluster found=%s", session_id, session is not None)
        return session

    def put(self, session: dict) -> dict:
        session_id = str(session.get("id", "") or "")
        payload = dict(session)
        payload["updated_at"] = time.time()
        resp = self._request("PUT", f"/api/v1/session-store/{session_id}", json_body=payload)
        resp.raise_for_status()
        stored = resp.json().get("session") or payload
        logger.info("session put id=%s messages=%d backend=cluster",
                    session_id, len(payload.get("messages") or []))
        return stored

    def delete(self, session_id: str) -> bool:
        resp = self._request("DELETE", f"/api/v1/session-store/{session_id}")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        logger.info("session delete id=%s backend=cluster", session_id)
        return True

    def fork(self, session_id: str, title: str = "") -> dict | None:
        if title:
            logger.info("session fork title ignored backend=cluster (cluster API has no title param)")
        resp = self._request("POST", f"/api/v1/session-store/{session_id}/fork")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        forked = resp.json().get("session")
        logger.info("session forked from=%s backend=cluster", session_id)
        return forked


_store_singleton: SessionStore | None = None


def get_session_store(store: Any = None) -> SessionStore:
    global _store_singleton
    if _store_singleton is not None:
        return _store_singleton
    backend = os.environ.get("FUSION_SESSION_STORE", "sqlite").strip().lower()
    if backend == "file":
        _store_singleton = FileSessionStore()
    elif backend == "cluster":
        _store_singleton = ClusterSessionStore()
    elif backend == "sqlite":
        _store_singleton = SQLiteSessionStore(store)
    else:
        logger.warning("unknown FUSION_SESSION_STORE=%r, using sqlite", backend)
        _store_singleton = SQLiteSessionStore(store)
    logger.info("session store selected backend=%s", type(_store_singleton).__name__)
    return _store_singleton


def reset_session_store() -> None:
    global _store_singleton
    _store_singleton = None
