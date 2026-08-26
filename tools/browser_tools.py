"""Browser tool — Web Workflow automation via fusion-browser UDS (issue #234)."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import uuid
from pathlib import Path

from .base import BaseTool

logger = logging.getLogger(__name__)

BROWSER_CONFIG_PATH = Path.home() / ".fusion-browser" / "config.json"
_DEFAULT_SOCKET = "/tmp/fusion-browser.sock"
_MAX_FRAME = 8 * 1024 * 1024
_SOCKET_TIMEOUT = 30.0

# auth_ack.caps bitmask (FBCapabilities rawValue):
# bit0 navigate, bit1 click, bit2 type, bit3 scroll, bit4 screenshot, bit5 evaluate, bit6 close
_CAP_BITS = {
    "navigate": 1 << 0,
    "click": 1 << 1,
    "type_text": 1 << 2,
    "scroll": 1 << 3,
    "screenshot": 1 << 4,
    "evaluate": 1 << 5,
    "close": 1 << 6,
}

# agent action -> wire action. extract & screenshot both hit wire `screenshot`
# (only non-mutating state-fetch on the fixed 7-action wire protocol);
# extract surfaces ax_tree_markdown, screenshot surfaces the jpeg.
_WIRE_ACTION = {
    "navigate": "navigate",
    "click": "click",
    "type_text": "type_text",
    "scroll": "scroll",
    "screenshot": "screenshot",
    "evaluate": "evaluate",
    "extract": "screenshot",
}

# idempotent wire actions: safe to resend on session_recovered (contract §6).
_IDEMPOTENT = {"navigate", "scroll", "screenshot"}

_VALID_ACTIONS = [
    "create_session", "navigate", "extract", "click", "type_text",
    "scroll", "screenshot", "evaluate", "close_session",
]


def browser_available() -> bool:
    return BROWSER_CONFIG_PATH.exists()


def _load_config() -> tuple[str, str]:
    if not BROWSER_CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"fusion-browser config not found: {BROWSER_CONFIG_PATH} (issue #234)"
        )
    cfg = json.loads(BROWSER_CONFIG_PATH.read_text(encoding="utf-8"))
    socket_path = cfg.get("socketPath", _DEFAULT_SOCKET)
    token = cfg.get("authToken", "")
    return socket_path, token


class _BrowserConnection:
    # one UDS connection per session: auth handshake then framed request/response.
    def __init__(self, sock_path: str, token: str):
        self._sock_path = sock_path
        self._token = token
        self._sock: socket.socket | None = None
        self._caps = 0

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(_SOCKET_TIMEOUT)
        self._sock.connect(self._sock_path)
        self._send({"type": "auth", "token": self._token})
        ack = self._recv()
        if not ack or ack.get("type") != "auth_ack":
            self.close()
            raise ConnectionError(f"fusion-browser auth failed: {ack}")
        self._caps = int(ack.get("caps", 0))
        logger.info("fusion-browser auth ok caps=%d", self._caps)

    def has_cap(self, wire_action: str) -> bool:
        return bool(self._caps & _CAP_BITS.get(wire_action, 0))

    def request(self, msg: dict) -> dict:
        self._send(msg)
        return self._recv() or {}

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _send(self, obj: dict) -> None:
        if self._sock is None:
            raise RuntimeError("connection not established")
        data = json.dumps(obj).encode()
        if len(data) > _MAX_FRAME:
            raise ValueError(f"frame too large: {len(data)} > {_MAX_FRAME}")
        self._sock.sendall(struct.pack(">I", len(data)) + data)

    def _recv(self) -> dict | None:
        if self._sock is None:
            raise RuntimeError("connection not established")
        hdr = self._recvn(4)
        if not hdr:
            return None
        (length,) = struct.unpack(">I", hdr)
        if length > _MAX_FRAME:
            raise ValueError(f"frame too large: {length} > {_MAX_FRAME}")
        body = self._recvn(length)
        if not body:
            return None
        return json.loads(body)

    def _recvn(self, n: int) -> bytes | None:
        if self._sock is None:
            raise RuntimeError("connection not established")
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf


class BrowserTool(BaseTool):
    name = "browser"
    description = (
        "Automate Web Workflow via a local fusion-browser engine over UDS. "
        "Actions: create_session, navigate, extract, click, type_text, scroll, "
        "screenshot, evaluate, close_session. extract/navigate/click/etc. return "
        "ax_tree_markdown (reduced interactive-node tree) for the next-step "
        "decision; never feed raw node JSON to the LLM."
    )
    parameters = {
        "action": {
            "type": "string",
            "description": (
                "Browser action: create_session, navigate, extract, click, "
                "type_text, scroll, screenshot, evaluate, close_session"
            ),
            "enum": _VALID_ACTIONS,
        },
        "session_id": {
            "type": "string",
            "description": "Session id from create_session (required for all actions except create_session)",
            "default": "",
        },
        "url": {
            "type": "string",
            "description": "URL for navigate; also used as initial_url on create_session",
            "default": "",
        },
        "target_node_id": {
            "type": "string",
            "description": "AXTree stable node id (@eN) for click/type_text",
            "default": "",
        },
        "text": {
            "type": "string",
            "description": "text for type_text; JS source for evaluate",
            "default": "",
        },
        "scroll_delta_y": {
            "type": "number",
            "description": "scroll pixel delta (default 300)",
            "default": 300,
        },
        "mode": {
            "type": "string",
            "description": "create_session mode: headless (default) or headed (popup window, debug)",
            "enum": ["headless", "headed"],
            "default": "headless",
        },
        "credential_domain": {
            "type": "string",
            "description": "create_session: domain whose Keychain credentials to inject (plaintext never returned)",
            "default": "",
        },
        "trace_id": {
            "type": "string",
            "description": "optional trace id; auto-generated as fb-<uuid8> when empty",
            "default": "",
        },
    }

    def __init__(self):
        self._sessions: dict[str, _BrowserConnection] = {}

    async def execute(self, **kwargs) -> str:
        action = kwargs.get("action", "")
        if action not in _VALID_ACTIONS:
            return f"Error: Unknown browser action: {action}"
        try:
            if action == "create_session":
                return await self._create_session(kwargs)
            if action == "close_session":
                return await self._close_session(kwargs)
            return await self._do_execute_action(action, kwargs)
        except FileNotFoundError as e:
            return f"Error: {e}"
        except (ConnectionError, socket.timeout, OSError) as e:
            logger.warning("browser %s connection failed: %s", action, e)
            return f"Error: fusion-browser connection failed: {e} (is fusion-browser running?)"
        except Exception as e:
            logger.exception("browser tool error action=%s", action)
            return f"Error: browser {action} failed: {e}"

    async def _create_session(self, kwargs: dict) -> str:
        mode = kwargs.get("mode", "headless") or "headless"
        initial_url = kwargs.get("url", "") or None
        credential_domain = kwargs.get("credential_domain", "") or None
        sock_path, token = _load_config()
        conn = _BrowserConnection(sock_path, token)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, conn.connect)
        payload = {
            "mode": mode,
            "initial_url": initial_url,
            "credential_domain": credential_domain,
        }
        resp = await loop.run_in_executor(
            None, conn.request, {"type": "create_session", "payload": payload}
        )
        if resp.get("type") == "error":
            return self._format_error_payload(resp.get("payload", {}), "create_session")
        rp = resp.get("payload", {})
        sid = rp.get("session_id")
        if not sid:
            return f"Error: browser create_session failed: no session_id in response: {resp}"
        self._sessions[sid] = conn
        ci = rp.get("credential_injected", False)
        logger.info("browser create_session sid=%s credential_injected=%s", sid, ci)
        return f"session created: session_id={sid} credential_injected={ci}"

    async def _close_session(self, kwargs: dict) -> str:
        sid = kwargs.get("session_id", "")
        if not sid or sid not in self._sessions:
            return "Error: close_session requires a valid session_id"
        conn = self._sessions.pop(sid)
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None, conn.request, {"type": "close", "session_id": sid}
            )
            logger.info("browser close_session sid=%s resp_type=%s", sid, resp.get("type"))
        finally:
            conn.close()
        return f"session closed: session_id={sid}"

    async def _do_execute_action(self, agent_action: str, kwargs: dict) -> str:
        sid = kwargs.get("session_id", "")
        if not sid or sid not in self._sessions:
            return f"Error: {agent_action} requires a valid session_id"
        conn = self._sessions[sid]
        wire_action = _WIRE_ACTION[agent_action]
        if not conn.has_cap(wire_action):
            return f"Error: action '{agent_action}' not permitted by token caps (caps={conn._caps})"
        trace_id = kwargs.get("trace_id", "") or f"fb-{uuid.uuid4().hex[:8]}"
        payload = {
            "session_id": sid,
            "action": wire_action,
            "target_node_id": kwargs.get("target_node_id", "") or None,
            "payload_text": self._payload_text(agent_action, kwargs),
            "scroll_delta_y": float(kwargs.get("scroll_delta_y", 300))
            if agent_action == "scroll" else None,
            "trace_id": trace_id,
        }
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None, conn.request, {"type": "execute", "payload": payload}
        )
        if resp.get("type") == "error":
            return self._format_error_payload(resp.get("payload", {}), agent_action)
        state = resp.get("payload", {})
        return await self._handle_state(agent_action, wire_action, state, conn, sid, payload, loop)

    def _payload_text(self, agent_action: str, kwargs: dict):
        if agent_action == "navigate":
            return kwargs.get("url", "") or None
        if agent_action == "type_text":
            return kwargs.get("text", "") or None
        if agent_action == "evaluate":
            return kwargs.get("text", "") or None
        return None

    async def _handle_state(
        self, agent_action, wire_action, state, conn, sid, payload, loop
    ) -> str:
        err = state.get("error")
        if err:
            code = err.get("code", "")
            # node_stale (§5): DOM changed, do NOT resend old node_id.
            # auto re-extract for fresh @eN, surface to LLM to re-select.
            if code == "node_stale":
                fresh = await self._extract_fresh(conn, sid, loop)
                return (
                    "node_stale: target node fingerprint mismatch (DOM changed). "
                    "Refreshed tree below — re-select the node and retry:\n" + fresh
                )
            # session_recovered + retryable + idempotent (§6): auto-resend once.
            if (
                err.get("retryable")
                and state.get("session_recovered")
                and wire_action in _IDEMPOTENT
            ):
                logger.info(
                    "browser %s session_recovered, resending idempotent action sid=%s",
                    agent_action, sid,
                )
                resp2 = await loop.run_in_executor(
                    None, conn.request, {"type": "execute", "payload": payload}
                )
                state2 = (resp2 or {}).get("payload", {})
                if not state2.get("error"):
                    return self._format_state(state2, agent_action)
                return self._format_error_payload(state2["error"], agent_action)
            # session_not_found: connection dead, drop it.
            if code == "session_not_found":
                self._sessions.pop(sid, None)
                conn.close()
            return self._format_error_payload(err, agent_action)
        return self._format_state(state, agent_action)

    async def _extract_fresh(self, conn, sid, loop) -> str:
        trace_id = f"fb-{uuid.uuid4().hex[:8]}"
        payload = {
            "session_id": sid,
            "action": "screenshot",
            "target_node_id": None,
            "payload_text": None,
            "scroll_delta_y": None,
            "trace_id": trace_id,
        }
        try:
            resp = await loop.run_in_executor(
                None, conn.request, {"type": "execute", "payload": payload}
            )
            state = (resp or {}).get("payload", {})
            md = state.get("ax_tree_markdown")
            return md if md else "(no ax_tree_markdown on re-extract)"
        except Exception as e:
            return f"(re-extract failed: {e})"

    def _format_state(self, state: dict, agent_action: str) -> str:
        parts = []
        url = state.get("url")
        title = state.get("title")
        if url:
            parts.append(f"url: {url}")
        if title:
            parts.append(f"title: {title}")
        md = state.get("ax_tree_markdown")
        if agent_action == "screenshot":
            jpeg = state.get("screenshot_jpeg")
            if jpeg:
                if len(jpeg) > 65536:
                    parts.append(
                        f"screenshot_jpeg: {len(jpeg)} bytes base64 (too large for text context)"
                    )
                else:
                    parts.append(f"screenshot_jpeg: {jpeg}")
            else:
                parts.append("screenshot_jpeg: (none)")
            if md:
                parts.append(f"ax_tree_markdown:\n{md}")
        else:
            if md:
                parts.append(f"ax_tree_markdown:\n{md}")
            else:
                parts.append("(no ax_tree_markdown)")
        if state.get("session_recovered"):
            parts.append("note: session was recovered by engine watchdog this action")
        return "\n".join(parts)

    def _format_error_payload(self, err: dict, agent_action: str) -> str:
        code = err.get("code", "unknown")
        message = err.get("message", "")
        retryable = err.get("retryable", False)
        return f"Error: browser {agent_action} failed: {code} - {message} (retryable={retryable})"
