"""E5: approval lifecycle webhooks + offline permission reviewer."""

from __future__ import annotations

import pytest

from agent_runtime.offline_reviewer import OfflineReviewer


def _ev(type_, name="", ts=0.0, event_id="", args=None, content=""):
    return {
        "type": type_,
        "name": name,
        "timestamp": ts,
        "event_id": event_id,
        "args": args or {},
        "content": content,
    }


class TestOfflineReviewer:
    async def test_flags_destructive_shell(self):
        r = OfflineReviewer()
        events = [
            _ev("tool_call", "terminal", 1.0, "e1", {"command": "rm -rf /tmp/x /"}),
        ]
        report = await r.review_run("run-1", events)
        assert report["flagged"] == 1
        assert report["findings"][0]["rule_id"] == "destructive-shell"

    async def test_flags_workspace_escape(self):
        r = OfflineReviewer()
        events = [
            _ev("tool_call", "file_write", 1.0, "e1", {"path": "/etc/hosts"}),
        ]
        report = await r.review_run("run-1", events)
        assert report["flagged"] == 1

    async def test_benign_calls_zero_findings(self):
        r = OfflineReviewer()
        events = [
            _ev("tool_call", "read_file", 1.0, "e1", {"path": "src/main.py"}),
            _ev("tool_call", "file_list", 1.1, "e2", {}),
            _ev("tool_call", "terminal", 1.2, "e3", {"command": "pytest tests/ -q"}),
        ]
        report = await r.review_run("run-1", events)
        assert report["flagged"] == 0

    async def test_rules_can_be_overridden(self):
        rules = [{"id": "deny-browser", "tool": "browser_navigate", "pattern": ".", "severity": "high"}]
        r = OfflineReviewer(rules=rules)
        events = [_ev("tool_call", "browser_navigate", 1.0, "e1", {"url": "http://x"})]
        report = await r.review_run("run-1", events)
        assert report["flagged"] == 1

    async def test_llm_judge_flags_overreach(self):
        class JudgeClient:
            model = "m"

            async def chat(self, model, messages, **kw):
                class R:
                    content = '{"verdict": "overreach", "reason": "writes outside workspace"}'

                return R()

        r = OfflineReviewer(rules=[], mlx_client=JudgeClient())
        events = [_ev("tool_call", "custom_tool", 1.0, "e1", {"target": "prod-db"})]
        report = await r.review_run("run-1", events)
        assert report["flagged"] == 1
        assert report["findings"][0]["layer"] == "llm"

    async def test_llm_judge_failure_marks_unreviewed(self):
        class BadClient:
            model = "m"

            async def chat(self, model, messages, **kw):
                raise RuntimeError("mlx down")

        r = OfflineReviewer(rules=[], mlx_client=BadClient())
        events = [_ev("tool_call", "custom_tool", 1.0, "e1", {})]
        report = await r.review_run("run-1", events)
        assert report["flagged"] == 0
        assert report["unreviewed"] == 1
        assert report["findings"][0]["verdict"] == "unreviewed"

    async def test_no_llm_client_no_llm_findings(self):
        r = OfflineReviewer(rules=[])
        events = [_ev("tool_call", "custom_tool", 1.0, "e1", {})]
        report = await r.review_run("run-1", events)
        assert report["findings"] == []

    async def test_audit_write(self, tmp_path):
        from agent_runtime.audit_logger import AuditLogger

        al = AuditLogger(db_path=str(tmp_path / "audit.db"))
        r = OfflineReviewer(audit_logger=al)
        events = [_ev("tool_call", "terminal", 1.0, "e1", {"command": "sudo rm -rf /"})]
        report = await r.review_run("run-1", events)
        entries = al.query(resource_id="run-1") if hasattr(al, "query") else []
        if not entries:
            import sqlite3

            conn = sqlite3.connect(al.db_path)
            rows = conn.execute("SELECT action, result FROM audit_log").fetchall()
            conn.close()
        else:
            rows = [(e.action, e.result) for e in entries]
        assert rows and rows[0][0] == "offline_review.finding"
        assert rows[0][1] == "flagged"
        assert report["report_id"]


class TestRunsOfflineReviewRpc:
    @pytest.fixture
    async def daemon(self, tmp_path):
        import os

        from agent_runtime.daemon_server import DaemonServer

        sock = f"/tmp/test-e5-{os.getpid()}.sock"
        d = DaemonServer(
            socket_path=sock,
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=str(tmp_path / "s.db"),
        )
        await d.start()
        yield d
        await d.stop()
        if os.path.exists(sock):
            os.unlink(sock)

    async def test_review_via_history_store(self, daemon, tmp_path):
        from agent_runtime.run_history import RunHistoryStore

        daemon._run_history_store = RunHistoryStore(db_path=str(tmp_path / "h.db"))
        daemon._run_history_store.save_run(
            "run-e5",
            "g",
            "g",
            [_ev("tool_call", "terminal", 1.0, "e1", {"command": "rm -rf /"})],
        )
        h = daemon._get_handler("runs.offlineReview")
        r = await h({"run_id": "run-e5"})
        assert r["report"]["flagged"] == 1

    async def test_unknown_run(self, daemon):
        h = daemon._get_handler("runs.offlineReview")
        r = await h({"run_id": "nope"})
        assert "error" in r


class TestApprovalLifecycleWebhooks:
    async def test_requested_and_resolved_fired(self, monkeypatch, tmp_path):
        from agent_runtime.agent_platform import door as door_mod
        from agent_runtime.agent_platform.door import CapabilityApprovalStore, CapabilityDoor

        store = CapabilityApprovalStore(db_path=str(tmp_path / "ap.db"))
        fired = []

        class FakeRegistry:
            async def fire(self, event, payload):
                fired.append((event, payload))

        import agent_runtime.agent_platform.webhooks as wh

        monkeypatch.setattr(wh, "get_webhook_registry", lambda: FakeRegistry())
        monkeypatch.setattr(door_mod, "get_capability", lambda cid: None, raising=False)
        d = CapabilityDoor(approval_store=store)
        rec = store.create("cap-x", "agent-1", 1, "pub", {"a": 1}, requester="agent:agent-1")
        await d._notify_requested(rec)
        assert fired and fired[0][0] == "approval.requested"
        assert fired[0][1]["approval_id"] == rec["id"]

        # resolved fires alongside decided
        cap = store.create("cap-y", "agent-1", 1, "pub", {}, requester="agent:agent-1")
        await d.reject(cap["id"], "human-1", reason="no")
        events = [e for e, _ in fired]
        assert "approval.resolved" in events
        assert "approval.decided" in events
