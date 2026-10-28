"""#500: E2E core flows — real daemon subprocess + stub MLX + Playwright.

Covers the #500 acceptance path against the deployed web GUI:
- dashboard / workflows pages render from the daemon-served dist
- a graph created over REST shows up in the workflows page
- graph execution streams SSE events end-to-end with stubbed LLM output
- HITL: confirmation_policy tier "always" pends a tool action, the Safety
  page approves it, the blocked run completes

Runner: pytest tests/e2e -m e2e   (default run excludes this marker)
"""

from __future__ import annotations

import threading
import time

import pytest

from tests.e2e.client import E2E_API_KEY
from tests.e2e.client import http_json as _http_json
from tests.e2e.client import rpc as _rpc

pytestmark = pytest.mark.e2e


def _llm_graph(name: str) -> dict:
    return {
        "name": name,
        "nodes": {
            "start": {"type": "start", "label": "Start"},
            "llm1": {
                "type": "llm",
                "label": "Reply",
                "model": "stub-model",
                "system_prompt": "reply briefly",
            },
            "end": {"type": "end", "label": "End"},
        },
        "edges": [
            {"source": "start", "target": "llm1"},
            {"source": "llm1", "target": "end"},
        ],
    }


class TestPages:
    def test_dashboard_loads(self, page, daemon_base):
        # hash router: studio tabs are flat segments (#/dashboard, #/safety) —
        # parseHash falls back to workflows for anything else
        page.goto(f"{daemon_base}/#/dashboard")
        page.wait_for_selector("text=仪表盘", timeout=20000)
        assert page.title() != ""

    def test_workflows_page_lists_created_graph(self, page, daemon_base):
        created = _http_json(
            daemon_base, "POST", "/v1/graphs", {"graph_data": _llm_graph("E2E Smoke Graph")}
        )
        graph_id = created["graph_id"]
        try:
            page.goto(f"{daemon_base}/#/workflows")
            # canvas badge + list row can both match; require a visible one
            page.wait_for_selector("text=E2E Smoke Graph >> visible=true", timeout=20000)
        finally:
            # cleanup keeps the shared daemon store small across CI runs
            _http_json(daemon_base, "DELETE", f"/v1/graphs/{graph_id}")


class TestExecution:
    def test_graph_execute_returns_stub_events(self, daemon_base, stub_mlx):
        created = _http_json(
            daemon_base, "POST", "/v1/graphs", {"graph_data": _llm_graph("E2E Exec Graph")}
        )
        graph_id = created["graph_id"]
        try:
            result = _http_json(
                daemon_base,
                "POST",
                f"/v1/graphs/{graph_id}/execute",
                {"graph_id": graph_id, "input_text": "hello"},
            )
        finally:
            _http_json(daemon_base, "DELETE", f"/v1/graphs/{graph_id}")
        assert result["status"] == "completed"
        types = [e.get("type") for e in result["events"]]
        assert "result" in types or "end" in types
        # the stub MLX actually served the LLM call for this run
        assert stub_mlx["state"].requests, "stub MLX saw no chat request"
        assert any("/chat/completions" in r["path"] for r in stub_mlx["state"].requests)

    def test_sse_stream_emits_events(self, page, daemon_base, stub_mlx):
        created = _http_json(
            daemon_base, "POST", "/v1/graphs", {"graph_data": _llm_graph("E2E SSE Graph")}
        )
        graph_id = created["graph_id"]
        # EventSource cannot set headers — the SSE endpoint accepts ?api_key=
        url = (
            f"{daemon_base}/v1/graphs/{graph_id}/execute/stream"
            f"?input=hello&api_key={E2E_API_KEY}"
        )
        # EventSource from about:blank is cross-origin (no CORS on the
        # endpoint) — load a real page first so the stream is same-origin,
        # exactly like the GUI's own usage
        page.goto(f"{daemon_base}/#/workflows")
        try:
            events = page.evaluate(
                """async (url) => {
                    return await new Promise((resolve, reject) => {
                        const got = [];
                        const es = new EventSource(url);
                        const timer = setTimeout(() => { es.close(); reject(new Error('SSE timeout')); }, 30000);
                        es.onmessage = (m) => {
                            got.push(JSON.parse(m.data));
                            if (got[got.length - 1].type === 'done') {
                                clearTimeout(timer); es.close(); resolve(got);
                            }
                        };
                        es.onerror = () => {
                            clearTimeout(timer); es.close();
                            // server closes the stream after done — EventSource
                            // raises error on close, so treat events-so-far as done
                            if (got.length) { resolve(got); } else { reject(new Error('SSE error')); }
                        };
                    });
                }""",
                url,
            )
        finally:
            _http_json(daemon_base, "DELETE", f"/v1/graphs/{graph_id}")
        types = [e.get("type") for e in events]
        assert "done" in types
        assert any(t in ("result", "end") for t in types), f"no terminal event in {types}"


class TestHitlApproval:
    def test_confirmation_policy_pends_and_ui_approves(self, page, daemon_base):
        graph_data = {
            "name": "E2E HITL Graph",
            "confirmation_policy": {"tier": "always"},
            "nodes": {
                "start": {"type": "start", "label": "Start"},
                "t1": {
                    "type": "tool",
                    "label": "List",
                    "tool_name": "file_list",
                    "tool_params": {"path": "/tmp"},
                },
                "end": {"type": "end", "label": "End"},
            },
            "edges": [
                {"source": "start", "target": "t1"},
                {"source": "t1", "target": "end"},
            ],
        }
        created = _http_json(daemon_base, "POST", "/v1/graphs", {"graph_data": graph_data})
        graph_id = created["graph_id"]

        outcome: dict = {}

        def _run() -> None:
            try:
                outcome["result"] = _http_json(
                    daemon_base,
                    "POST",
                    f"/v1/graphs/{graph_id}/execute",
                    {"graph_id": graph_id, "input_text": "go"},
                )
            except Exception as e:  # pragma: no cover - surfaced via assert below
                outcome["error"] = repr(e)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        try:
            # wait until the tier-"always" policy pends the tool action
            deadline = time.time() + 30
            action_id = None
            while time.time() < deadline:
                pending = _rpc(daemon_base, "safety.get_pending_actions")
                actions = pending.get("actions") or []
                if actions:
                    action_id = actions[0]["action_id"]
                    break
                time.sleep(0.5)
            assert action_id, "no pending approval appeared within 30s"

            # approve through the real Safety page UI (flat hash segment)
            page.goto(f"{daemon_base}/#/safety")
            page.wait_for_selector("text=待审动作", timeout=20000)
            page.click("text=待审动作")
            page.wait_for_selector(f"text={action_id}", timeout=20000)
            page.locator("div", has_text=action_id).filter(
                has=page.get_by_role("button", name="批准")
            ).first.get_by_role("button", name="批准").click()
            # standalone host renders no toast; the empty state is the visible
            # proof that the approve RPC landed and the queue drained
            page.wait_for_selector("text=无待审动作", timeout=20000)

            worker.join(timeout=60)
            assert "result" in outcome, f"execute did not finish: {outcome.get('error')}"
            events = outcome["result"]["events"]
            actions = [e.get("metadata", {}).get("action") for e in events]
            assert "pending_approval" in actions and "approved" in actions
        finally:
            worker.join(timeout=10)
            _http_json(daemon_base, "DELETE", f"/v1/graphs/{graph_id}")
