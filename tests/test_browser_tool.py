"""Issue #234 — BrowserTool Web Workflow automation over fusion-browser UDS.

Pins the consumer-side integration in this repo:
  - create_session / navigate / extract / click / type_text / scroll /
    screenshot / evaluate / close_session dispatch + framing.
  - node_stale auto re-extract (NOT silent failure, NOT resend old node_id).
  - session_recovered + idempotent action auto-resend once.
  - caps bitmask gating (evaluate denied when bit 5 unset).
  - credential_injected surfaced as bool only (plaintext never returned).
  - trace_id auto-generation + echo.
  - graceful skip: registry.failed_plugins["browser"] when config absent.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import uuid

import pytest

import tools as _tools_mod
from tools import create_default_registry
from tools.browser_tools import (
    _CAP_BITS,
    BrowserTool,
    browser_available,
)

# --- fake UDS server: speaks [u32 BE len][JSON] framing, scripts responses ---


class _FakeBrowserServer:
    def __init__(self, path: str):
        self.path = path
        self._server: asyncio.AbstractServer | None = None
        # scripted replies per received request (FIFO queue of dicts).
        self.replies: list[dict] = []
        self.received: list[dict] = []
        self._token = "test-token"
        self._caps = 95  # navigate|click|type|scroll|screenshot|close (no evaluate)
        self._conns: set[asyncio.StreamWriter] = set()

    async def start(self):
        self._server = await asyncio.start_unix_server(self._handle, self.path)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._conns.add(writer)
        try:
            while True:
                hdr = await reader.readexactly(4)
                (length,) = struct.unpack(">I", hdr)
                body = await reader.readexactly(length)
                msg = json.loads(body)
                self.received.append(msg)
                if msg.get("type") == "auth":
                    if msg.get("token") == self._token:
                        writer.write(self._frame({"type": "auth_ack", "caps": self._caps}))
                    else:
                        writer.write(
                            self._frame(
                                {
                                    "type": "error",
                                    "payload": {"code": "auth_denied", "retryable": False},
                                }
                            )
                        )
                    await writer.drain()
                    continue
                if not self.replies:
                    writer.write(
                        self._frame(
                            {
                                "type": "error",
                                "payload": {"code": "internal_error", "retryable": False},
                            }
                        )
                    )
                    await writer.drain()
                    continue
                reply = self.replies.pop(0)
                writer.write(self._frame(reply))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            self._conns.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    def _frame(obj: dict) -> bytes:
        data = json.dumps(obj).encode()
        return struct.pack(">I", len(data)) + data

    async def stop(self):
        if self._server is not None:
            self._server.close()
            # force-close any lingering client connections so wait_closed() returns
            for w in list(self._conns):
                try:
                    w.close()
                except Exception:
                    pass
            self._conns.clear()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2)
            except asyncio.TimeoutError:
                pass
            self._server = None


@pytest.fixture
def fb_config(tmp_path, monkeypatch):
    cfg = tmp_path / "fb-config.json"
    # macOS AF_UNIX path limit ~104 chars; tmp_path under /private/var/folders
    # overflows it. Use a short /tmp prefix for the socket itself.
    sock = "/tmp/fb-test-" + uuid.uuid4().hex[:8] + ".sock"
    cfg.write_text(
        json.dumps({"authToken": "test-token", "socketPath": sock, "allowedOrigins": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr("tools.browser_tools.BROWSER_CONFIG_PATH", cfg)
    monkeypatch.setattr("tools.browser_tools._load_config", lambda: (sock, "test-token"))
    return cfg, sock


@pytest.fixture
async def fb_server(fb_config):
    _cfg, sock = fb_config
    srv = _FakeBrowserServer(sock)
    await srv.start()
    yield srv
    await srv.stop()
    if os.path.exists(sock):
        os.unlink(sock)


def _state(sid="s1", url="https://example.com", md="# Page\n- @e1 [button] Login", **extra):
    payload = {
        "session_id": sid,
        "url": url,
        "title": "Example",
        "ax_tree_markdown": md,
        "interactive_nodes": [
            {
                "node_id": "@e1",
                "role": "button",
                "name": "Login",
                "is_disabled": False,
                "current_value": "",
            }
        ],
        "screenshot_jpeg": None,
        "session_recovered": False,
        "error": None,
        "trace_id": "fb-test",
        "execution_time_ms": 10,
    }
    payload.update(extra)
    return {"type": "state", "payload": payload}


# --- unit: caps bitmask mapping ---


def test_caps_bits_mapping():
    assert _CAP_BITS["navigate"] == 1
    assert _CAP_BITS["evaluate"] == 32
    assert 95 & _CAP_BITS["evaluate"] == 0
    assert 95 & _CAP_BITS["navigate"] != 0


# --- unit: graceful skip when config absent ---


def test_browser_available_default_false(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.browser_tools.BROWSER_CONFIG_PATH", tmp_path / "nope.json")
    assert browser_available() is False


def test_registry_skips_browser_when_config_absent(monkeypatch, tmp_path):
    nope = tmp_path / "nope.json"
    monkeypatch.setattr("tools.browser_tools.BROWSER_CONFIG_PATH", nope)
    monkeypatch.setattr(_tools_mod, "BROWSER_CONFIG_PATH", nope)
    reg = create_default_registry()
    assert not reg.has("browser")
    assert "browser" in reg.failed_plugins
    assert "config not found" in reg.failed_plugins["browser"]


def test_registry_registers_browser_when_config_present(fb_config, monkeypatch):
    monkeypatch.setattr(_tools_mod, "BROWSER_CONFIG_PATH", fb_config[0])
    reg = create_default_registry()
    assert reg.has("browser")
    assert "browser" not in reg.failed_plugins


# --- integration: create_session + close_session ---


@pytest.mark.asyncio
async def test_create_and_close_session(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
        {"type": "closed", "session_id": "s1"},
    ]
    tool = BrowserTool()
    r = await tool.execute(action="create_session")
    assert "session_id=s1" in r
    assert "credential_injected=False" in r
    assert "s1" in tool._sessions

    r2 = await tool.execute(action="close_session", session_id="s1")
    assert "session closed" in r2
    assert "s1" not in tool._sessions
    # server got auth + create_session + close frames
    types = [m.get("type") for m in srv.received]
    assert types[0] == "auth"
    assert "create_session" in types
    assert "close" in types


@pytest.mark.asyncio
async def test_create_session_quota_exceeded(fb_server):
    srv = fb_server
    srv.replies = [
        {
            "type": "error",
            "payload": {"code": "quota_exceeded", "message": "ram tier", "retryable": False},
        },
    ]
    tool = BrowserTool()
    r = await tool.execute(action="create_session")
    assert "Error" in r
    assert "quota_exceeded" in r
    assert "retryable=False" in r


# --- integration: navigate + extract return ax_tree_markdown ---


@pytest.mark.asyncio
async def test_navigate_returns_ax_tree_markdown(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
        _state(sid="s1", url="https://example.com/home", md="# Home\n- @e2 [link] About"),
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    r = await tool.execute(action="navigate", session_id="s1", url="https://example.com/home")
    assert "url: https://example.com/home" in r
    assert "ax_tree_markdown" in r
    assert "@e2" in r
    # LLM gets markdown, NOT raw interactive_nodes JSON
    assert "interactive_nodes" not in r


@pytest.mark.asyncio
async def test_extract_returns_markdown_not_raw_nodes(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
        _state(sid="s1", md="# Form\n- @e5 [textbox] email"),
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    r = await tool.execute(action="extract", session_id="s1")
    assert "ax_tree_markdown" in r
    assert "@e5" in r
    assert "interactive_nodes" not in r
    # extract maps to wire `screenshot` action (non-mutating state fetch)
    exec_msg = [m for m in srv.received if m.get("type") == "execute"][0]
    assert exec_msg["payload"]["action"] == "screenshot"


# --- critical: node_stale auto re-extract, not silent fail, not resend old ---


@pytest.mark.asyncio
async def test_node_stale_auto_re_extract(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
        # click returns node_stale error
        {
            "type": "state",
            "payload": {
                "session_id": "s1",
                "error": {"code": "node_stale", "message": "dom changed", "retryable": True},
                "session_recovered": False,
            },
        },
        # auto re-extract returns fresh tree
        _state(sid="s1", md="# Refreshed\n- @e9 [button] Login"),
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    r = await tool.execute(action="click", session_id="s1", target_node_id="@e3")
    assert "node_stale" in r
    assert "Refreshed" in r
    assert "@e9" in r
    # a re-extract (screenshot) frame was sent after the stale error
    exec_msgs = [m for m in srv.received if m.get("type") == "execute"]
    assert len(exec_msgs) == 2
    assert exec_msgs[0]["payload"]["action"] == "click"
    assert exec_msgs[1]["payload"]["action"] == "screenshot"
    # re-extract used a NEW trace_id, not the stale click's
    assert exec_msgs[1]["payload"]["trace_id"] != exec_msgs[0]["payload"]["trace_id"]


@pytest.mark.asyncio
async def test_node_stale_re_extract_failure_handled(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
        {
            "type": "state",
            "payload": {
                "session_id": "s1",
                "error": {"code": "node_stale", "message": "dom changed", "retryable": True},
            },
        },
        {
            "type": "error",
            "payload": {"code": "internal_error", "message": "boom", "retryable": False},
        },
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    r = await tool.execute(action="click", session_id="s1", target_node_id="@e3")
    # node_stale surfaced + re-extract attempted (no crash); error frame yields no markdown
    assert "node_stale" in r
    assert "no ax_tree_markdown" in r or "re-extract failed" in r


# --- critical: session_recovered + idempotent action auto-resend once ---


@pytest.mark.asyncio
async def test_session_recovered_resend_idempotent(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
        # navigate returns recovered + retryable
        {
            "type": "state",
            "payload": {
                "session_id": "s1",
                "error": {"code": "timeout", "message": "slow", "retryable": True},
                "session_recovered": True,
            },
        },
        # resend succeeds
        _state(sid="s1", url="https://example.com/ok", md="# Ok"),
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    r = await tool.execute(action="navigate", session_id="s1", url="https://example.com/ok")
    assert "url: https://example.com/ok" in r
    exec_msgs = [m for m in srv.received if m.get("type") == "execute"]
    assert len(exec_msgs) == 2
    assert exec_msgs[0]["payload"]["action"] == "navigate"
    assert exec_msgs[1]["payload"]["action"] == "navigate"
    # resend uses SAME trace_id (same logical action)
    assert exec_msgs[1]["payload"]["trace_id"] == exec_msgs[0]["payload"]["trace_id"]


@pytest.mark.asyncio
async def test_non_idempotent_click_not_resend_on_recovered(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
        # click returns recovered+retryable, but click is NON-idempotent -> no resend
        {
            "type": "state",
            "payload": {
                "session_id": "s1",
                "error": {"code": "timeout", "message": "slow", "retryable": True},
                "session_recovered": True,
            },
        },
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    r = await tool.execute(action="click", session_id="s1", target_node_id="@e3")
    assert "Error" in r
    assert "timeout" in r
    exec_msgs = [m for m in srv.received if m.get("type") == "execute"]
    assert len(exec_msgs) == 1  # no resend for non-idempotent click


# --- caps gating: evaluate denied when bit 5 unset (caps=95) ---


@pytest.mark.asyncio
async def test_evaluate_denied_by_caps(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    r = await tool.execute(action="evaluate", session_id="s1", text="document.title")
    assert "Error" in r
    assert "caps" in r
    # evaluate never sent to server (gated client-side)
    exec_msgs = [m for m in srv.received if m.get("type") == "execute"]
    assert all(m["payload"]["action"] != "evaluate" for m in exec_msgs)


@pytest.mark.asyncio
async def test_evaluate_allowed_when_cap_set(fb_server, monkeypatch):
    srv = fb_server
    srv._caps = 127  # 95 | 32 -> evaluate allowed
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
        _state(sid="s1", md="eval done"),
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    r = await tool.execute(action="evaluate", session_id="s1", text="document.title")
    assert "eval done" in r
    exec_msgs = [m for m in srv.received if m.get("type") == "execute"]
    assert exec_msgs[-1]["payload"]["action"] == "evaluate"
    assert exec_msgs[-1]["payload"]["payload_text"] == "document.title"


# --- trace_id auto-generation + echo ---


@pytest.mark.asyncio
async def test_trace_id_auto_generated(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
        _state(sid="s1"),
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    await tool.execute(action="navigate", session_id="s1", url="https://example.com")
    exec_msgs = [m for m in srv.received if m.get("type") == "execute"]
    tid = exec_msgs[-1]["payload"]["trace_id"]
    assert tid.startswith("fb-")
    assert len(tid) == 11  # fb- + 8 hex


@pytest.mark.asyncio
async def test_trace_id_passthrough_when_provided(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
        _state(sid="s1"),
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    await tool.execute(action="click", session_id="s1", target_node_id="@e1", trace_id="fb-custom")
    exec_msgs = [m for m in srv.received if m.get("type") == "execute"]
    assert exec_msgs[-1]["payload"]["trace_id"] == "fb-custom"


# --- credential_injected bool only ---


@pytest.mark.asyncio
async def test_credential_injected_bool_only(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": True}},
    ]
    tool = BrowserTool()
    r = await tool.execute(action="create_session", credential_domain="example.com")
    assert "credential_injected=True" in r
    # no plaintext credential ever surfaces
    assert "password" not in r.lower()
    # create_session payload carried credential_domain
    cs_msg = [m for m in srv.received if m.get("type") == "create_session"][0]
    assert cs_msg["payload"]["credential_domain"] == "example.com"


# --- connection-failure resilience ---


@pytest.mark.asyncio
async def test_action_without_session_id_errors():
    tool = BrowserTool()
    r = await tool.execute(action="click", target_node_id="@e1")
    assert "Error" in r
    assert "session_id" in r


@pytest.mark.asyncio
async def test_unknown_action_errors():
    tool = BrowserTool()
    r = await tool.execute(action="nope")
    assert "Error" in r
    assert "Unknown" in r


@pytest.mark.asyncio
async def test_session_not_found_drops_connection(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
        {
            "type": "state",
            "payload": {
                "session_id": "s1",
                "error": {"code": "session_not_found", "message": "gone", "retryable": False},
            },
        },
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    r = await tool.execute(action="navigate", session_id="s1", url="https://example.com")
    assert "Error" in r
    assert "session_not_found" in r
    # connection dropped from local session map
    assert "s1" not in tool._sessions


# --- framing: u32 BE length-prefix JSON ---


@pytest.mark.asyncio
async def test_framing_big_endian_length_prefix(fb_server):
    srv = fb_server
    srv.replies = [
        {"type": "create_session", "payload": {"session_id": "s1", "credential_injected": False}},
    ]
    tool = BrowserTool()
    await tool.execute(action="create_session")
    # server decoded frames -> received populated -> framing (BE u32 + JSON) correct
    assert any(m.get("type") == "auth" for m in srv.received)
    assert any(m.get("type") == "create_session" for m in srv.received)
