"""Tests for #209: UDS socket security hardening.

Covers:
1. socket created with mode 0o600 (not 0o666) after start
2. FUSION_SOCKET_DIR env opt-in resolves to private dir + 0o700
3. default (no env) stays /tmp/fusion-studio.sock (backward compat)
4. _verify_peer_uid returns True for same-uid peer (live UDS)
5. _verify_peer_uid rejects a writer whose getsockopt reports a foreign uid
   (monkeypatched, since we cannot forge a real cross-uid connection in CI)
6. _handle_client closes the connection when _verify_peer_uid returns False
   (no dispatch happens, daemon stays up)
"""

from __future__ import annotations

import asyncio
import os
import stat
import struct
import tempfile

import pytest

from agent_runtime.daemon_server import (
    SOCKET_PATH,
    DaemonServer,
    _resolve_socket_path,
    _verify_peer_uid,
)


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def socket_path():
    p = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield p
    if os.path.exists(p):
        os.unlink(p)


@pytest.fixture
def store_db():
    p = tempfile.mktemp(suffix=".db")
    yield p
    if os.path.exists(p):
        os.unlink(p)


@pytest.fixture
def daemon(socket_path, store_db):
    d = DaemonServer(
        socket_path=socket_path,
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=store_db,
    )
    # gate LLM/RAG behind no-client so startup does not hit fusion-mlx
    d._gateway._default_client = None
    d._gateway._default_model = ""
    return d


class TestSocketMode:
    @pytest.mark.asyncio
    async def test_socket_mode_is_0o600_not_0o666(self, daemon, socket_path):
        # #209: central router socket must not be world-writable.
        await daemon.start()
        try:
            assert os.path.exists(socket_path)
            assert _mode(socket_path) == 0o600
        finally:
            await daemon.stop()


class TestSocketDirEnv:
    def test_env_private_dir_resolves_and_is_0o700(self, monkeypatch):
        # #209: FUSION_SOCKET_DIR -> <dir>/fusion-studio.sock, dir 0o700.
        d = tempfile.mkdtemp()
        try:
            monkeypatch.setenv("FUSION_SOCKET_DIR", d)
            p = _resolve_socket_path()
            assert p == os.path.join(d, "fusion-studio.sock")
            assert _mode(d) == 0o700
        finally:
            os.rmdir(d)

    def test_no_env_keeps_default_path(self, monkeypatch):
        # #209: backward compat — no env => /tmp/fusion-studio.sock unchanged.
        monkeypatch.delenv("FUSION_SOCKET_DIR", raising=False)
        assert _resolve_socket_path() == SOCKET_PATH


class TestPeerUidCheck:
    @pytest.mark.asyncio
    async def test_same_uid_peer_accepted(self, socket_path):
        # Live UDS: a real same-uid connection must pass the credential check.
        async def _close(r, w):
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass

        server = await asyncio.start_unix_server(
            _close, path=socket_path
        )
        try:
            _, writer = await asyncio.open_unix_connection(socket_path)
            try:
                assert _verify_peer_uid(writer) is True
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_foreign_uid_rejected(self):
        # #209: a peer reporting a foreign uid via LOCAL_PEERCRED must be denied.
        # We cannot forge a real cross-uid UDS in CI, so build a fake writer whose
        # get_extra_info('socket') returns a fake socket with a controlled
        # getsockopt. This exercises _verify_peer_uid's comparison directly.
        foreign_uid = os.getuid() + 4242

        class _FakeSock:
            def getsockopt(self, level, opt, buflen):
                return struct.pack("iII", os.getpid(), foreign_uid, foreign_uid)

        class _FakeWriter:
            def get_extra_info(self, name, default=None):
                return _FakeSock() if name == "socket" else default

        assert _verify_peer_uid(_FakeWriter()) is False

    @pytest.mark.asyncio
    async def test_handle_client_closes_on_rejected_uid(self, daemon, socket_path, monkeypatch):
        # #209: when _verify_peer_uid denies a connection, _handle_client must
        # close the writer and return WITHOUT dispatching. daemon stays up and a
        # subsequent allowed call still works (regression for the gate path).
        await daemon.start()
        try:
            # Force the deny path.
            monkeypatch.setattr(
                "agent_runtime.daemon_server._verify_peer_uid", lambda w: False
            )

            reader, writer = await asyncio.open_unix_connection(socket_path)
            writer.write(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n')
            await writer.drain()
            # Rejected: server closes us -> empty read (EOF), no response.
            data = await asyncio.wait_for(reader.read(256), timeout=3.0)
            assert data == b""
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

            # Now restore allow + confirm daemon still serves a real call.
            monkeypatch.setattr(
                "agent_runtime.daemon_server._verify_peer_uid", lambda w: True
            )
            reader2, writer2 = await asyncio.open_unix_connection(socket_path)
            try:
                writer2.write(b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{}}\n')
                await writer2.drain()
                line = await asyncio.wait_for(reader2.readline(), timeout=5.0)
                assert b'"pong"' in line
            finally:
                writer2.close()
                try:
                    await writer2.wait_closed()
                except Exception:
                    pass
        finally:
            await daemon.stop()
