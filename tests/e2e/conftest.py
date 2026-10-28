"""#500: E2E fixtures — stub fusion-mlx + isolated daemon subprocess + Playwright.

Boot order (session-scoped, real processes):
1. Stub MLX: threading HTTP server on an ephemeral port answering OpenAI-shaped
   /v1/models + /v1/chat/completions from a scriptable response queue.
2. Daemon: `python -m agent_runtime.daemon_server` subprocess with private
   socket/store/ws/cluster/http ports and FUSION_GATEWAY_URL pointed at the
   stub — fully isolated from any real daemon on this machine.
3. Browser: Playwright chromium against the daemon-served web dist (web/dist).

The whole module is skipped when playwright or the built web dist is missing —
`pytest -m e2e` stays green on machines without bun/chromium via skips.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.e2e.client import E2E_API_KEY

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

_REPO = Path(__file__).resolve().parents[2]
_WEB_DIST = _REPO / "web" / "dist" / "index.html"

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StubMLXState:
    """Thread-safe scriptable state shared with the stub HTTP handler."""

    def __init__(self):
        self.lock = threading.Lock()
        # Queue of canned chat payloads; exhausted -> repeat the last one.
        self.chat_responses: list[dict] = [
            {"content": "stub reply", "finish_reason": "stop"}
        ]
        self.requests: list[dict] = []

    def push_chat(self, content: str, tool_calls: list | None = None) -> None:
        entry = {"content": content, "finish_reason": "stop"}
        if tool_calls:
            entry["tool_calls"] = tool_calls
            entry["content"] = content
        with self.lock:
            self.chat_responses.append(entry)

    def reset(self) -> None:
        with self.lock:
            self.chat_responses = [{"content": "stub reply", "finish_reason": "stop"}]
            self.requests = []


def _make_stub_handler(state: StubMLXState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.endswith("/models"):
                self._send_json({"data": [{"id": "stub-model"}]})
            else:
                self._send_json({"error": f"no route {self.path}"}, status=404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            with state.lock:
                state.requests.append({"path": self.path, "payload": payload})
                responses = list(state.chat_responses)
            if not self.path.endswith("/chat/completions"):
                self._send_json({"error": f"no route {self.path}"}, status=404)
                return
            entry = responses[min(len(state.requests) - 1, len(responses) - 1)]
            message = {"role": "assistant", "content": entry.get("content", "")}
            if entry.get("tool_calls"):
                message["tool_calls"] = entry["tool_calls"]
            self._send_json(
                {
                    "id": "stub-chat",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": entry.get("finish_reason", "stop"),
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            )

    return Handler


@pytest.fixture(scope="session")
def stub_mlx():
    state = StubMLXState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_stub_handler(state))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"state": state, "port": port, "url": f"http://127.0.0.1:{port}/v1"}
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def guard_stub_dir(tmp_path_factory):
    """In-process stub for the fusion_guard native module.

    The real fusion-guard daemon is not part of this repo's CI, and
    GuardSafetyBackend.confirm fails closed when guard is down — which would
    dead-end the HITL approval flow. A stub package on PYTHONPATH shadows the
    local maturin build (and provides the module where it is absent): benign
    evaluate verdicts, confirm always succeeds.
    """
    pkg = tmp_path_factory.mktemp("e2e-guard-stub") / "fusion_guard"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        '"""#500 e2e stub: minimal NativeGuardClient duck-type for GuardSafetyBackend."""\n'
        "import logging\n"
        "logger = logging.getLogger('fusion_guard.stub')\n"
        "\n"
        "\n"
        "class NativeGuardVerdict:\n"
        "    def __init__(self):\n"
        '        self.action = "allow"\n'
        '        self.risk_level = "l1"\n'
        '        self.reason = "stub guard: benign"\n'
        "        self.requires_approval = False\n"
        "        self.action_id = None\n"
        '        self.redacted_content = ""\n'
        '        self.inferred_category = ""\n'
        "        self.verdict_epoch = 0\n"
        '        self.stage = ""\n'
        "\n"
        "\n"
        "class NativeGuardClient:\n"
        "    def __init__(self, sock_path=None):\n"
        "        self.sock_path = sock_path\n"
        "        self.confirms = []\n"
        '        logger.info("stub guard client ready sock=%s", sock_path)\n'
        "\n"
        "    def ping(self):\n"
        "        return True\n"
        "\n"
        "    def list_rules(self):\n"
        "        return [], 0\n"
        "\n"
        "    def evaluate(self, content='', caller_epoch=0, tenant_id=None, "
        "requester=None, content_type='text', category_hint=None):\n"
        '        logger.info("stub guard evaluate hint=%s", category_hint)\n'
        "        return NativeGuardVerdict()\n"
        "\n"
        "    def confirm(self, action_id, approved, approved_by=None, tenant_id=None):\n"
        "        self.confirms.append({'action_id': action_id, 'approved': approved})\n"
        '        logger.info("stub guard confirm action_id=%s approved=%s", action_id, approved)\n'
        "        return True\n"
    )
    return pkg.parent


@pytest.fixture(scope="session")
def daemon_base(stub_mlx, guard_stub_dir, tmp_path_factory):
    if not _WEB_DIST.exists():
        pytest.skip(
            f"web dist not built at {_WEB_DIST} — run: cd web && bun install && bun run build"
        )
    tmp = tmp_path_factory.mktemp("e2e-daemon")
    # AF_UNIX paths are capped at ~104 chars — pytest tmp dirs blow past it,
    # so keep the socket itself under /tmp with a short unique name.
    sock_path = f"/tmp/fas-e2e-{os.getpid()}.sock"
    http_port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "FUSION_GATEWAY_URL": stub_mlx["url"],
            "FUSION_MLX_API_KEY": "e2e-stub-key",
            # deterministic auth: the machine's real key files must not leak in
            "FUSION_AGENT_STUDIO_API_KEY": E2E_API_KEY,
            "FUSION_SAFETY_TIMEOUT_DISPOSITION": "reject",
            "PYTHONUNBUFFERED": "1",
            # hermetic HOME: rules cache / apikeys / persona / plugins resolve
            # under the tmp dir instead of polluting the real ~/.fusion-*
            "HOME": str(tmp),
            # stub fusion_guard shadows any local build and fills in for CI
            "PYTHONPATH": str(guard_stub_dir)
            + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
        }
    )
    # full daemon output goes to a file — failures stay diagnosable after teardown
    log_path = f"/tmp/fas-e2e-daemon-{os.getpid()}.log"
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_runtime.daemon_server",
            "--socket-path",
            sock_path,
            "--store-path",
            str(tmp / "store.db"),
            "--ws-port",
            str(_free_port()),
            "--cluster-port",
            str(_free_port()),
            "--http-port",
            str(http_port),
        ],
        cwd=str(_REPO),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{http_port}"
    import urllib.error
    import urllib.request

    deadline = time.time() + 90
    last_err = None
    while time.time() < deadline:
        if proc.poll() is not None:
            log_file.close()
            out = Path(log_path).read_text(errors="replace")
            pytest.fail(f"daemon subprocess died during startup:\n{out[-3000:]}")
        try:
            with urllib.request.urlopen(f"{base}/v1/dashboard", timeout=2) as resp:
                if resp.status == 200:
                    break
        except (urllib.error.URLError, OSError) as e:
            last_err = e
        time.sleep(0.5)
    else:
        proc.terminate()
        log_file.close()
        pytest.fail(f"daemon did not become healthy within 90s: {last_err}")
    print(f"\n[e2e] daemon log: {log_path}")
    yield base
    proc.terminate()
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    log_file.close()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        instance = p.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser, daemon_base):
    context = browser.new_context()
    # the SPA holds no keys (spec S); the e2e "host" injects the static key
    # on every request the way the gateway /shell proxy would (Bearer header)
    context.route(
        "**/*",
        lambda route: route.continue_(
            headers={**route.request.headers, "x-api-key": E2E_API_KEY}
        ),
    )
    pg = context.new_page()
    pg.set_default_timeout(15000)
    yield pg
    context.close()
