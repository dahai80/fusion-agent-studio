from __future__ import annotations

import json
import socket
import threading

import pytest

from agent_runtime import cli


class _Recorder:
    def __init__(self, responses=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses = responses or {}

    def __call__(self, method: str, path: str, payload: dict | None = None) -> dict:
        self.calls.append((method, path, payload))
        return self.responses.get((method, path), {"ok": True})


@pytest.fixture
def rec():
    return _Recorder()


def _client(monkeypatch, recorder):
    # main() builds its own AgentStudioClient — patch the class request
    # method so every instance routes through the recorder.
    monkeypatch.setattr(cli.AgentStudioClient, "request", recorder)
    return cli.AgentStudioClient(base_url="http://test", api_key="k-test")


def test_parser_approvals_create(rec, monkeypatch, capsys):
    _client(monkeypatch, rec)
    rc = cli.main([
        "--base-url", "http://test", "--api-key", "k-test",
        "approvals", "create",
        "--title", "gate:g1 — proposal",
        "--requester", "pipeline-bot",
        "--assigned-to", "owner-a",
        "--type", "gate:g1",
        "--payload-json", '{"issue": "#578"}',
    ])
    assert rc == 0
    method, path, payload = rec.calls[0]
    assert method == "POST" and path == "/v1/agent-platform/approvals"
    assert payload["requester"] == "pipeline-bot"
    assert payload["assigned_to"] == "owner-a"
    assert payload["payload"]["gate"] == "gate:g1"
    assert payload["payload"]["issue"] == "#578"


def test_parser_approvals_list_and_decide(rec, monkeypatch, capsys):
    _client(monkeypatch, rec)
    rc = cli.main([
        "--base-url", "http://test", "--api-key", "k",
        "approvals", "list", "--status", "pending",
    ])
    assert rc == 0
    assert rec.calls[-1][:2] == ("GET", "/v1/agent-platform/approvals?status=pending&limit=50")

    rc = cli.main([
        "--base-url", "http://test", "--api-key", "k",
        "approvals", "approve", "abc123", "--decided-by", "owner-a",
    ])
    assert rc == 0
    method, path, payload = rec.calls[-1]
    assert method == "POST" and path == "/v1/agent-platform/approvals/abc123/approve"
    assert payload == {"decided_by": "owner-a"}

    rc = cli.main([
        "--base-url", "http://test", "--api-key", "k",
        "approvals", "reject", "abc123", "--decided-by", "owner-a",
        "--note", "scope too large",
    ])
    assert rc == 0
    method, path, payload = rec.calls[-1]
    assert path == "/v1/agent-platform/approvals/abc123/reject"
    assert payload == {"decided_by": "owner-a", "reason": "scope too large"}


def test_decider_env_fallback(rec, monkeypatch, capsys):
    monkeypatch.setenv(cli.DECIDER_ENV, "env-decider")
    _client(monkeypatch, rec)
    rc = cli.main([
        "--base-url", "http://test", "--api-key", "k",
        "approvals", "approve", "id9",
    ])
    assert rc == 0
    assert rec.calls[-1][2] == {"decided_by": "env-decider"}


def test_decider_missing_raises(rec, monkeypatch, capsys):
    monkeypatch.delenv(cli.DECIDER_ENV, raising=False)
    _client(monkeypatch, rec)
    rc = cli.main([
        "--base-url", "http://test", "--api-key", "k",
        "approvals", "approve", "id9",
    ])
    assert rc == 1
    assert "decider identity required" in capsys.readouterr().err


def test_webhooks_add_list_remove(rec, monkeypatch, capsys):
    _client(monkeypatch, rec)
    rc = cli.main([
        "--base-url", "http://test", "--api-key", "k",
        "approvals", "webhooks", "add",
        "--url", "https://cb.example/hook", "--secret", "sixteen-char-key",
        "--events", "approval.decided,gate.expired",
    ])
    assert rc == 0
    method, path, payload = rec.calls[-1]
    assert (method, path) == ("POST", "/v1/agent-platform/webhooks")
    assert payload["secret"] == "sixteen-char-key"
    assert payload["events"] == ["approval.decided", "gate.expired"]

    rc = cli.main([
        "--base-url", "http://test", "--api-key", "k", "--json",
        "approvals", "webhooks", "list",
    ])
    assert rc == 0
    assert rec.calls[-1][:2] == ("GET", "/v1/agent-platform/webhooks")

    rc = cli.main([
        "--base-url", "http://test", "--api-key", "k",
        "approvals", "webhooks", "remove", "deadbeef",
    ])
    assert rc == 0
    assert rec.calls[-1][:2] == ("DELETE", "/v1/agent-platform/webhooks/deadbeef")


def test_payload_json_invalid_rejected_early(rec, monkeypatch, capsys):
    _client(monkeypatch, rec)
    rc = cli.main([
        "--base-url", "http://test", "--api-key", "k",
        "approvals", "create",
        "--title", "t", "--requester", "r", "--assigned-to", "a",
        "--payload-json", "{not json",
    ])
    assert rc == 1
    assert "not valid JSON" in capsys.readouterr().err


def _serve_one_response(sock_path: str, result: dict, error: bool = False):
    def run():
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(1)
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while b"\n" not in buf:
                buf += conn.recv(65536)
            body = {"error": result} if error else {"result": result}
            conn.sendall(json.dumps({"jsonrpc": "2.0", "id": 1, **body}).encode() + b"\n")
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    import time

    for _ in range(100):
        import os

        if os.path.exists(sock_path):
            break
        time.sleep(0.01)
    return t


def test_apikeys_recover_via_uds(tmp_path, monkeypatch, capsys):
    import tempfile

    sock_dir = tempfile.mkdtemp(prefix="f579cli_")
    sock_path = sock_dir + "/s.sock"
    _serve_one_response(
        sock_path,
        {"key_id": "kid1", "key_secret": "fk-onetime-secret", "key_prefix": "fk-onet"},
    )
    rc = cli.main([
        "--base-url", "http://test",
        "apikeys", "recover", "--name", "boot-recovery",
        "--socket", sock_path,
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "kid1" in out
    assert "fk-onetime-secret" in out


def test_uds_client_error_maps_to_runtime_error(tmp_path):
    client = cli.UdsRpcClient(socket_path=str(tmp_path / "missing.sock"))
    with pytest.raises(RuntimeError, match="UDS call failed"):
        client.call("apikey.loopback_create", {"name": "x"})


def test_uds_client_rpc_error_raises(tmp_path):
    import tempfile

    sock_dir = tempfile.mkdtemp(prefix="f579cli_")
    sock_path = sock_dir + "/err.sock"
    _serve_one_response(
        sock_path, {"code": -32000, "message": "boom"}, error=True
    )
    client = cli.UdsRpcClient(socket_path=sock_path)
    with pytest.raises(RuntimeError, match="boom"):
        client.call("apikey.loopback_rotate", {"key_id": "k"})
