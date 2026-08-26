"""Prometheus /metrics endpoint verification — audit 0826 tech-debt #3.

The /metrics endpoint was already published (api_server.py:356) but had zero
test coverage. This validates the Prometheus text exposition format and that
telemetry counters/latencies + daemon active-executions are exposed correctly.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from agent_runtime import api_server
from agent_runtime.api_server import app


@pytest.fixture
def metrics_client(monkeypatch):
    # auth must NOT gate /metrics (it has no Depends); ensure no stray auth.
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    return TestClient(app)


def _parse_exposition(text: str) -> dict[str, float]:
    # minimal Prometheus exposition parser: name -> value, skipping HELP/TYPE comments
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)\s+([-+0-9.eE]+)", line)
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


class TestPrometheusMetrics:
    def test_metrics_returns_text_plain(self, metrics_client):
        resp = metrics_client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers.get("content-type", "")

    def test_metrics_exposition_format_valid(self, metrics_client):
        resp = metrics_client.get("/metrics")
        text = resp.text
        # every metric line has a TYPE comment preceding it (convention used by handler)
        assert "# TYPE fusion_" in text
        parsed = _parse_exposition(text)
        # core telemetry gauges must be present
        assert "fusion_telemetry_spans_total" in parsed
        assert "fusion_telemetry_traces_total" in parsed
        assert "fusion_telemetry_active_spans" in parsed
        # values are numeric (parse succeeded) and non-negative
        for name, val in parsed.items():
            assert val >= 0, f"{name} negative: {val}"

    def test_metrics_exposes_counters(self, metrics_client):
        resp = metrics_client.get("/metrics")
        text = resp.text
        # llm_calls / tool_calls counters exist in telemetry defaults
        assert re.search(r"# TYPE fusion_counter_llm_calls counter", text)
        assert re.search(r"# TYPE fusion_counter_tool_calls counter", text)

    def test_metrics_daemon_active_executions_when_daemon_set(self, metrics_client, monkeypatch):
        # when a daemon is attached, active-executions gauge must appear
        class _FakeDaemon:
            _active_executions = {"e1": 1, "e2": 2}

        monkeypatch.setattr(api_server, "_daemon", _FakeDaemon())
        resp = metrics_client.get("/metrics")
        parsed = _parse_exposition(resp.text)
        assert parsed.get("fusion_daemon_active_executions") == 2

    def test_metrics_no_daemon_no_active_executions_gauge(self, metrics_client, monkeypatch):
        # without a daemon, the active-executions gauge is omitted (no crash)
        monkeypatch.setattr(api_server, "_daemon", None)
        resp = metrics_client.get("/metrics")
        parsed = _parse_exposition(resp.text)
        assert "fusion_daemon_active_executions" not in parsed
        # other gauges still present
        assert "fusion_telemetry_spans_total" in parsed
