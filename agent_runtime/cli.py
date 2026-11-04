"""fusion-agent CLI — operator client over the Agent Studio REST surface (#432).

Thin client: no daemon imports, no DB access. Every command maps onto an
existing /v1 endpoint. `submit` creates a run through POST /v1/runs, then
streams its spans via GET /v1/runs/{id}/stream (SSE). Sessions survive
process exit, so `resume` works from another machine that can reach the
daemon's HTTP API.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11442"
BASE_URL_ENV = "FUSION_AGENT_STUDIO_URL"
API_KEY_ENV = "FUSION_AGENT_STUDIO_API_KEY"
API_KEY_FILE = Path.home() / ".fusion-agent-studio" / "web_module_key"
RETRY_STATUS = {502, 503, 504}
# #579: trusted UDS control plane — loopback credential recovery transport.
UDS_SOCKET_ENV = "FUSION_STUDIO_SOCK"
UDS_SOCKET_PATH = "/tmp/fusion-studio.sock"
# #578: who approves/rejects when the operator does not pass --decided-by.
DECIDER_ENV = "FUSION_APPROVAL_DECIDER"


def _api_key(explicit: str = "") -> str:
    if explicit:
        return explicit.strip()
    env = os.environ.get(API_KEY_ENV, "").strip()
    if env:
        return env
    try:
        if API_KEY_FILE.exists():
            return API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.warning("api key file unreadable: %s", e)
    return ""


class UdsRpcClient:
    """#579: JSON-RPC 2.0 over the daemon's UDS control plane.

    The daemon verifies the peer uid on accept (#209) and marks UDS
    connections as loopback-trusted — that is the entire trust model for
    admin-key recovery. Never network-exposed.
    """

    def __init__(self, socket_path: str = "", timeout: float = 10.0):
        self.socket_path = (
            socket_path or os.environ.get(UDS_SOCKET_ENV, "") or UDS_SOCKET_PATH
        )
        self.timeout = timeout

    def call(self, method: str, params: dict | None = None) -> dict:
        import socket as _socket

        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        ).encode() + b"\n"
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
                sock.sendall(payload)
                buf = b""
                while b"\n" not in buf:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as e:
            raise RuntimeError(
                f"UDS call failed on {self.socket_path}: {e} — is the daemon "
                "running? (loopback recovery needs the local daemon socket)"
            ) from e
        if not buf:
            raise RuntimeError(f"UDS call {method}: empty response from daemon")
        try:
            resp = json.loads(buf.split(b"\n", 1)[0])
        except ValueError as e:
            raise RuntimeError(f"UDS call {method}: malformed response: {e}") from e
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(
                f"UDS call {method} -> error {err.get('code')}: {err.get('message')}"
            )
        return resp.get("result", {})


class AgentStudioClient:
    def __init__(self, base_url: str = "", api_key: str = "", timeout: float = 60.0, retries: int = 2):
        self.base_url = (base_url or os.environ.get(BASE_URL_ENV, "") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = _api_key(api_key)
        self.timeout = timeout
        self.retries = max(0, retries)

    def _headers(self, stream: bool = False) -> dict[str, str]:
        headers = {"Accept": "text/event-stream" if stream else "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _url(self, path: str, stream: bool = False) -> str:
        url = f"{self.base_url}{path}"
        if stream and self.api_key:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}api_key={self.api_key}"
        return url

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        last_error = ""
        for attempt in range(self.retries + 1):
            try:
                resp = httpx.request(
                    method,
                    self._url(path),
                    json=payload,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            except httpx.HTTPError as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning("request %s %s failed attempt=%d: %s", method, path, attempt, last_error)
                time.sleep(0.2 * (attempt + 1))
                continue
            if resp.status_code in RETRY_STATUS and attempt < self.retries:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning("request %s %s retryable status=%s", method, path, resp.status_code)
                time.sleep(0.2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                detail = resp.text
                try:
                    body = resp.json()
                    detail = json.dumps(body, ensure_ascii=False)
                except ValueError:
                    pass
                raise RuntimeError(f"{method} {path} -> HTTP {resp.status_code}: {detail[:500]}")
            if not resp.content:
                return {}
            return resp.json()
        raise RuntimeError(f"{method} {path} failed after {self.retries + 1} attempts: {last_error}")

    def submit(
        self,
        goal: str,
        agent_id: str = "",
        graph_id: str = "",
        sandbox: str = "local",
        title: str = "",
        idempotency_key: str = "",
    ) -> dict:
        payload: dict = {"goal": goal, "sandbox": sandbox}
        if agent_id:
            payload["agent_id"] = agent_id
        if graph_id:
            payload["graph_id"] = graph_id
        if title:
            payload["title"] = title
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return self.request("POST", "/v1/runs", payload)

    def get_run(self, run_id: str) -> dict:
        return self.request("GET", f"/v1/runs/{run_id}")

    def run_events(self, run_id: str) -> dict:
        return self.request("GET", f"/v1/runs/{run_id}/events")

    def list_runs(self, status: str = "", limit: int = 50) -> dict:
        return self.request("GET", f"/v1/runs?status={status}&limit={limit}")

    def cancel_run(self, run_id: str) -> dict:
        return self.request("POST", f"/v1/runs/{run_id}/cancel", {})

    def list_sessions(self, limit: int = 50, offset: int = 0, tag: str = "") -> dict:
        return self.request("GET", f"/v1/sessions?limit={limit}&offset={offset}&tag={tag}")

    def get_session(self, session_id: str) -> dict:
        return self.request("GET", f"/v1/sessions/{session_id}")

    def session_messages(self, session_id: str, limit: int = 200) -> dict:
        return self.request("GET", f"/v1/sessions/{session_id}/messages?limit={limit}")

    def resume_session(self, session_id: str, text: str = "") -> dict:
        return self.request("POST", f"/v1/sessions/{session_id}/resume", {"input": text})

    def fork_session(self, session_id: str, title: str = "") -> dict:
        return self.request("POST", f"/v1/sessions/{session_id}/fork", {"title": title})

    def inject_session(self, session_id: str, text: str) -> dict:
        return self.request("POST", f"/v1/sessions/{session_id}/inject", {"input": text})

    # ── #578: approvals + webhook registry (agent-platform REST) ──

    def approvals_create(
        self,
        title: str,
        requester: str,
        assigned_to: str = "",
        payload_json: str = "",
        agent_id: str = "",
        gate_type: str = "",
    ) -> dict:
        payload: dict = {"title": title, "requester": requester}
        if assigned_to:
            payload["assigned_to"] = assigned_to
        if agent_id:
            payload["agent_id"] = agent_id
        body_payload: dict = {}
        if payload_json:
            try:
                parsed = json.loads(payload_json)
            except ValueError as e:
                raise RuntimeError(f"--payload-json is not valid JSON: {e}") from e
            if not isinstance(parsed, dict):
                raise RuntimeError("--payload-json must decode to a JSON object")
            body_payload = parsed
        if gate_type:
            body_payload = {**body_payload, "gate": gate_type}
        if body_payload:
            payload["payload"] = body_payload
        return self.request("POST", "/v1/agent-platform/approvals", payload)

    def approvals_list(self, status: str = "", limit: int = 50) -> dict:
        return self.request("GET", f"/v1/agent-platform/approvals?status={status}&limit={limit}")

    def approvals_decide(self, approval_id: str, approve: bool, decider: str, note: str = "") -> dict:
        body: dict = {"decided_by": decider}
        # Server contract: reject carries `reason` (required for reject);
        # approve carries only decided_by — a note is sent as `reason` too,
        # harmless if unused.
        if note:
            body["reason"] = note
        action = "approve" if approve else "reject"
        return self.request("POST", f"/v1/agent-platform/approvals/{approval_id}/{action}", body)

    def webhooks_add(self, url: str, secret: str, events: str = "") -> dict:
        body: dict = {"url": url, "secret": secret}
        ev = [e.strip() for e in events.split(",") if e.strip()]
        if ev:
            body["events"] = ev
        return self.request("POST", "/v1/agent-platform/webhooks", body)

    def webhooks_list(self) -> dict:
        return self.request("GET", "/v1/agent-platform/webhooks")

    def webhooks_remove(self, webhook_id: str) -> dict:
        return self.request("DELETE", f"/v1/agent-platform/webhooks/{webhook_id}")

    def stream(self, path: str) -> Iterator[dict]:
        with httpx.stream(
            "GET", self._url(path, stream=True), headers=self._headers(stream=True), timeout=None
        ) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"GET {path} -> HTTP {resp.status_code}")
            event_name = ""
            data_lines: list[str] = []
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[len("data:"):].lstrip())
                    continue
                if line.strip() == "" and data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        frame = json.loads(payload)
                    except ValueError:
                        frame = {"type": "raw", "data": payload}
                    if event_name:
                        frame.setdefault("event", event_name)
                    event_name = ""
                    yield frame

    def stream_run(self, run_id: str) -> Iterator[dict]:
        return self.stream(f"/v1/runs/{run_id}/stream")


def _print_frame(frame: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(frame, ensure_ascii=False), flush=True)
        return
    kind = frame.get("type", "")
    if kind == "status":
        print(f"[{frame.get('status')}]", flush=True)
    elif kind == "span":
        span = frame.get("span") or {}
        node = span.get("node_id", "")
        status = span.get("status", "")
        duration = span.get("duration_ms", "")
        print(f"  {node} {status} {duration}ms", flush=True)
    elif kind == "done":
        print(f"done status={frame.get('status')}", flush=True)
    else:
        print(json.dumps(frame, ensure_ascii=False), flush=True)


def _resolve_target(target: str, graph_flag: bool) -> tuple[str, str]:
    if target.startswith("agent:"):
        return target[len("agent:"):], ""
    if target.startswith("graph:"):
        return "", target[len("graph:"):]
    if graph_flag:
        return "", target
    return target, ""


def _read_input(value: str) -> str:
    if value == "-":
        return sys.stdin.read().strip()
    return value


def _cmd_submit(client: AgentStudioClient, args: argparse.Namespace) -> int:
    goal = _read_input(args.input)
    if not goal:
        print("error: --input required (use - for stdin)", file=sys.stderr)
        return 2
    agent_id, graph_id = _resolve_target(args.target, args.graph)
    if not agent_id and not graph_id:
        print("error: target required", file=sys.stderr)
        return 2
    res = client.submit(
        goal,
        agent_id=agent_id,
        graph_id=graph_id,
        sandbox=args.sandbox,
        title=args.title,
        idempotency_key=args.idempotency_key,
    )
    run_id = str(res.get("run_id", ""))
    print(json.dumps(res, ensure_ascii=False) if args.json else f"run_id={run_id} status={res.get('status')}")
    if not run_id or args.no_stream:
        return 0
    for frame in client.stream_run(run_id):
        _print_frame(frame, args.json)
        if frame.get("type") in ("done", "timeout", "error"):
            break
    return 0


def _cmd_attach(client: AgentStudioClient, args: argparse.Namespace) -> int:
    for frame in client.stream_run(args.run_id):
        _print_frame(frame, args.json)
        if frame.get("type") in ("done", "timeout", "error"):
            break
    return 0


def _cmd_resume(client: AgentStudioClient, args: argparse.Namespace) -> int:
    res = client.resume_session(args.session_id, _read_input(args.input))
    print(json.dumps(res, ensure_ascii=False) if args.json else json.dumps(res, ensure_ascii=False))
    return 0 if res.get("status") != "error" else 1


def _cmd_fork(client: AgentStudioClient, args: argparse.Namespace) -> int:
    res = client.fork_session(args.session_id, args.title)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("status") != "error" else 1


def _cmd_inject(client: AgentStudioClient, args: argparse.Namespace) -> int:
    text = _read_input(args.input)
    if not text:
        print("error: --input required", file=sys.stderr)
        return 2
    res = client.inject_session(args.session_id, text)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("status") != "error" else 1


def _cmd_list(client: AgentStudioClient, args: argparse.Namespace) -> int:
    if args.kind == "sessions":
        res = client.list_sessions(limit=args.limit, tag=args.tag)
    else:
        res = client.list_runs(status=args.status, limit=args.limit)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if args.kind == "sessions":
        for row in res.get("sessions", []):
            print(
                f"{row.get('id', row.get('session_id', ''))}  {row.get('mode', ''):10}  "
                f"{row.get('title', '') or row.get('summary', '')}"
            )
    else:
        for row in res.get("runs", []):
            print(
                f"{row.get('task_id', '')}  {row.get('status', ''):10}  "
                f"{row.get('agent_id', '') or row.get('graph_id', '')}  {row.get('title', '')}"
            )
    return 0


def _cmd_show(client: AgentStudioClient, args: argparse.Namespace) -> int:
    if args.kind == "session":
        res = client.get_session(args.target)
        if not args.json:
            res = {"session": res.get("session", res), "messages": client.session_messages(args.target).get("messages", [])}
    else:
        res = {"run": client.get_run(args.target), "events": client.run_events(args.target)}
    print(json.dumps(res, ensure_ascii=False, indent=None if args.json else 2))
    return 0


def _cmd_cancel(client: AgentStudioClient, args: argparse.Namespace) -> int:
    print(json.dumps(client.cancel_run(args.run_id), ensure_ascii=False))
    return 0


# ── #578: approvals command group ──


def _decider(explicit: str) -> str:
    d = explicit.strip() or os.environ.get(DECIDER_ENV, "").strip()
    if not d:
        raise RuntimeError(
            "decider identity required: pass --decided-by or set "
            f"{DECIDER_ENV} (who is approving/rejecting)"
        )
    return d


def _cmd_approvals_create(client: AgentStudioClient, args: argparse.Namespace) -> int:
    rec = client.approvals_create(
        title=args.title,
        requester=args.requester,
        assigned_to=args.assigned_to,
        payload_json=args.payload_json,
        agent_id=args.agent_id,
        gate_type=args.type,
    )
    print(f"approval created: {rec.get('approval_id')} (status={rec.get('status')})")
    print(f"poll: {client.base_url}{rec.get('poll', '/v1/agent-platform/approvals?status=pending')}")
    return 0


def _cmd_approvals_list(client: AgentStudioClient, args: argparse.Namespace) -> int:
    rec = client.approvals_list(status=args.status, limit=args.limit)
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0
    rows = rec.get("approvals", rec.get("data", []))
    if not rows:
        print(f"no approvals (status={args.status or 'all'})")
        return 0
    for r in rows:
        print(
            f"{r.get('id', '')}  status={r.get('status', '')}  "
            f"kind={r.get('kind', '')}  requester={r.get('requester', '')}  "
            f"assigned={r.get('assigned_to', '')}  {r.get('title', '')}"
        )
    return 0


def _cmd_approvals_decide(client: AgentStudioClient, args: argparse.Namespace) -> int:
    approve = args.decision == "approve"
    rec = client.approvals_decide(
        args.approval_id,
        approve=approve,
        decider=_decider(args.decided_by),
        note=args.note,
    )
    print(f"approval {args.approval_id}: {rec.get('status', 'decided')}")
    return 0


def _cmd_webhooks_add(client: AgentStudioClient, args: argparse.Namespace) -> int:
    rec = client.webhooks_add(args.url, args.secret, args.events)
    print(f"webhook registered: {rec.get('id')} -> {rec.get('url')}")
    return 0


def _cmd_webhooks_list(client: AgentStudioClient, args: argparse.Namespace) -> int:
    rec = client.webhooks_list()
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0
    rows = rec.get("webhooks", rec.get("data", []))
    if not rows:
        print("no webhooks registered")
        return 0
    for r in rows:
        print(f"{r.get('id', '')}  {r.get('url', '')}  events={','.join(r.get('events', []))}")
    return 0


def _cmd_webhooks_remove(client: AgentStudioClient, args: argparse.Namespace) -> int:
    client.webhooks_remove(args.webhook_id)
    print(f"webhook removed: {args.webhook_id}")
    return 0


# ── #579: loopback credential recovery (UDS, peer-uid verified) ──


def _cmd_apikeys_recover(client: AgentStudioClient, args: argparse.Namespace) -> int:
    uds = UdsRpcClient(socket_path=args.socket)
    if args.rotate:
        rec = uds.call("apikey.loopback_rotate", {"key_id": args.rotate})
        label = f"rotated {args.rotate}"
    else:
        if not args.name:
            raise RuntimeError(
                "--name required (or pass --rotate KEY_ID to rotate an existing key)"
            )
        params: dict = {"name": args.name}
        if args.permissions:
            params["permissions"] = [p.strip() for p in args.permissions.split(",") if p.strip()]
        rec = uds.call("apikey.loopback_create", params)
        label = f"created {rec.get('key_id')} ({args.name})"
    secret = rec.get("key_secret", "")
    if not secret:
        raise RuntimeError(f"issuance failed: {rec}")
    print(f"{label}")
    print(f"key_secret (shown once, store it now): {secret}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fusion-agent",
        description="Operator CLI for Fusion Agent Studio (thin REST client).",
    )
    parser.add_argument("--base-url", default="", help=f"daemon URL (default env {BASE_URL_ENV} or {DEFAULT_BASE_URL})")
    parser.add_argument("--api-key", default="", help=f"API key (default env {API_KEY_ENV} or module key file)")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="submit a run and stream its events")
    p_submit.add_argument("target", help="agent id, graph id, or agent:<id> / graph:<id>")
    p_submit.add_argument("-i", "--input", default="", help="run input (use - for stdin)")
    p_submit.add_argument("--graph", action="store_true", help="treat target as a graph id")
    p_submit.add_argument("--sandbox", default="local", help="local (default) or multi-node:<node_id>")
    p_submit.add_argument("--title", default="", help="run title")
    p_submit.add_argument("--idempotency-key", default="", help="dedupe key")
    p_submit.add_argument("--no-stream", action="store_true", help="return after submit")
    p_submit.set_defaults(func=_cmd_submit)

    p_attach = sub.add_parser("attach", help="stream events of an existing run")
    p_attach.add_argument("run_id")
    p_attach.set_defaults(func=_cmd_attach)

    p_resume = sub.add_parser("resume", help="resume a session by id")
    p_resume.add_argument("session_id")
    p_resume.add_argument("-i", "--input", default="", help="continue input")
    p_resume.set_defaults(func=_cmd_resume)

    p_fork = sub.add_parser("fork", help="fork a session")
    p_fork.add_argument("session_id")
    p_fork.add_argument("--title", default="")
    p_fork.set_defaults(func=_cmd_fork)

    p_inject = sub.add_parser("inject", help="append a user turn to a live session")
    p_inject.add_argument("session_id")
    p_inject.add_argument("-i", "--input", required=True)
    p_inject.set_defaults(func=_cmd_inject)

    p_list = sub.add_parser("list", help="list runs or sessions")
    p_list.add_argument("kind", choices=["runs", "sessions"])
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--status", default="")
    p_list.add_argument("--tag", default="")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="show a run or session")
    p_show.add_argument("kind", choices=["run", "session"])
    p_show.add_argument("target")
    p_show.set_defaults(func=_cmd_show)

    p_cancel = sub.add_parser("cancel", help="cancel a run")
    p_cancel.add_argument("run_id")
    p_cancel.set_defaults(func=_cmd_cancel)

    # #578: approvals — G-gate registration + decisions over agent-platform REST
    p_appr = sub.add_parser("approvals", help="approval gateway: create/list/approve/reject + webhooks")
    appr_sub = p_appr.add_subparsers(dest="command_approvals", required=True)

    p_acreate = appr_sub.add_parser("create", help="register an approval task (external kind)")
    p_acreate.add_argument("--title", required=True)
    p_acreate.add_argument("--requester", required=True)
    p_acreate.add_argument("--assigned-to", required=True, help="reviewer who may decide")
    p_acreate.add_argument("--type", default="", help="gate type tag, e.g. gate:g1 / gate:g4 / gate:g5")
    p_acreate.add_argument("--payload-json", default="", help="opaque consumer payload as JSON object")
    p_acreate.add_argument("--agent-id", default="")
    p_acreate.set_defaults(func=_cmd_approvals_create)

    p_alist = appr_sub.add_parser("list", help="list approvals")
    p_alist.add_argument("--status", default="", choices=["", "pending", "approved", "rejected"])
    p_alist.add_argument("--limit", type=int, default=50)
    p_alist.set_defaults(func=_cmd_approvals_list)

    for decision in ("approve", "reject"):
        p_adec = appr_sub.add_parser(decision, help=f"{decision} an approval")
        p_adec.add_argument("approval_id")
        p_adec.add_argument("--decided-by", default="", help=f"who decided (default env {DECIDER_ENV})")
        p_adec.add_argument("--note", default="", help="reason (required for reject)")
        p_adec.set_defaults(func=_cmd_approvals_decide, decision=decision)

    p_awk = appr_sub.add_parser("webhooks", help="decision webhook registry")
    wk_sub = p_awk.add_subparsers(dest="command_webhooks", required=True)

    p_wadd = wk_sub.add_parser("add", help="register a decision webhook")
    p_wadd.add_argument("--url", required=True)
    p_wadd.add_argument("--secret", required=True, help="HMAC secret, min 16 chars")
    p_wadd.add_argument("--events", default="", help="comma-separated event filter")
    p_wadd.set_defaults(func=_cmd_webhooks_add)

    p_wlist = wk_sub.add_parser("list", help="list registered webhooks")
    p_wlist.set_defaults(func=_cmd_webhooks_list)

    p_wrm = wk_sub.add_parser("remove", help="remove a webhook by id")
    p_wrm.add_argument("webhook_id")
    p_wrm.set_defaults(func=_cmd_webhooks_remove)

    # #579: loopback admin-key recovery over the trusted UDS control plane
    p_ak = sub.add_parser("apikeys", help="admin API key lifecycle")
    ak_sub = p_ak.add_subparsers(dest="command_apikeys", required=True)
    p_akr = ak_sub.add_parser(
        "recover",
        help="issue/rotate an admin key without an existing HTTP key "
        "(UDS loopback, peer-uid verified — local only)",
    )
    p_akr.add_argument("--name", default="", help="new key name (create path)")
    p_akr.add_argument("--rotate", default="", help="rotate existing key id instead of creating")
    p_akr.add_argument("--permissions", default="", help="comma-separated permissions (create path)")
    p_akr.add_argument(
        "--socket",
        default="",
        help=f"daemon UDS socket (default env {UDS_SOCKET_ENV} or {UDS_SOCKET_PATH})",
    )
    p_akr.set_defaults(func=_cmd_apikeys_recover)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    client = AgentStudioClient(base_url=args.base_url, api_key=args.api_key)
    try:
        return args.func(client, args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        logger.error("fusion-agent failed: %s", e)
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
