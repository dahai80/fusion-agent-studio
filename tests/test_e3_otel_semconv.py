"""E3: OTel GenAI semconv mapping, SDK bridge, W3C traceparent continuation."""

from __future__ import annotations

import json

import pytest

from agent_runtime.telemetry import (
    TelemetryEngine,
    format_traceparent,
    parse_traceparent,
    semconv_span,
)


class TestSemconv:
    def test_llm_call_mapping(self):
        d = semconv_span(
            {
                "name": "llm.call",
                "attributes": {
                    "model": "qwen3-4b",
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                },
            }
        )
        assert d["name"] == "chat"
        assert d["attributes"]["gen_ai.request.model"] == "qwen3-4b"
        assert d["attributes"]["gen_ai.usage.input_tokens"] == 100
        assert d["attributes"]["gen_ai.usage.output_tokens"] == 50

    def test_agent_and_tool_mapping(self):
        d = semconv_span({"name": "graph.execute", "attributes": {"graph_name": "g"}})
        assert d["name"] == "invoke_agent g"
        assert d["attributes"]["gen_ai.agent.name"] == "g"
        t = semconv_span({"name": "tool.call", "attributes": {"tool": "read_file"}})
        assert t["name"] == "execute_tool read_file"
        assert t["attributes"]["gen_ai.tool.name"] == "read_file"

    def test_unknown_name_passthrough(self):
        d = semconv_span({"name": "custom.span", "attributes": {"a": 1}})
        assert d["name"] == "custom.span" and d["attributes"] == {"a": 1}

    def test_otlp_export_uses_semconv(self):
        eng = TelemetryEngine()
        s = eng.start_span("graph.execute", trace_id="t1", attributes={"graph_name": "x"})
        eng.end_span(s.span_id)
        payload = json.loads(eng.export(fmt="otlp"))
        names = [
            sp["name"]
            for rs in payload["resourceSpans"]
            for ss in rs["scopeSpans"]
            for sp in ss["spans"]
        ]
        assert "invoke_agent x" in names


class TestTraceparent:
    def test_parse_valid(self):
        tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        parsed = parse_traceparent(tp)
        assert parsed == ("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7")

    @pytest.mark.parametrize(
        "tp",
        [
            "",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",  # 3 parts
            "00-zzz92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # non-hex
            "00-00000000000000000000000000000000-00f067aa0ba902b7-01",  # zero trace
            "0-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # bad version
        ],
    )
    def test_parse_invalid(self, tp):
        assert parse_traceparent(tp) is None

    def test_format_roundtrip(self):
        tp = format_traceparent("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7")
        assert parse_traceparent(tp) == (
            "4bf92f3577b34da6a3ce929d0e0e4736",
            "00f067aa0ba902b7",
        )


class TestSpanFromTraceparent:
    def test_continues_upstream_trace(self):
        eng = TelemetryEngine()
        tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        span = eng.span_from_traceparent("http.request", tp, attributes={"http.method": "POST"})
        assert span is not None
        assert span.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"[:16]
        assert span.parent_id == "00f067aa0ba902b7"
        assert span.attributes["w3c.traceparent"] == tp
        assert span.attributes["otel.trace_id"].startswith("4bf92f3577")
        eng.end_span(span.span_id)
        trace = eng.get_trace("4bf92f3577b34da6a3ce929d0e0e4736"[:16])
        assert trace is not None and trace["span_count"] == 1

    def test_invalid_traceparent_returns_none(self):
        eng = TelemetryEngine()
        assert eng.span_from_traceparent("http.request", "garbage") is None


class TestSdkBridge:
    def test_bridge_absent_degrades(self):
        eng = TelemetryEngine()
        s = eng.start_span("llm.call", attributes={"model": "m", "prompt_tokens": 1})
        eng.end_span(s.span_id)
        # no exception either way; spans recorded in memory regardless
        assert len(eng.list_spans(trace_id=s.trace_id)) == 1

    def test_bridge_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("FUSION_OTEL_SDK", "0")
        eng = TelemetryEngine()
        assert eng.sdk_bridge is None

    def test_bridge_export_with_fake_sdk(self, monkeypatch):
        calls = []

        class FakeSpan:
            def __init__(self, name, attributes=None, start_time=None):
                calls.append(("start", name, dict(attributes or {})))

            def end(self, end_time=None):
                calls.append(("end",))

        class FakeTracer:
            def start_span(self, name, attributes=None, start_time=None):
                return FakeSpan(name, attributes, start_time)

        import agent_runtime.telemetry as tel

        bridge = tel._OtelSdkBridge.__new__(tel._OtelSdkBridge)
        bridge.available = True
        bridge._tracer = FakeTracer()
        eng = TelemetryEngine()
        eng.sdk_bridge = bridge
        s = eng.start_span("llm.call", attributes={"model": "m", "prompt_tokens": 7})
        eng.end_span(s.span_id)
        kinds = [c[0] for c in calls]
        assert "start" in kinds and "end" in kinds
        started = next(c for c in calls if c[0] == "start")
        assert started[1] == "chat"
        assert started[2]["gen_ai.request.model"] == "m"
