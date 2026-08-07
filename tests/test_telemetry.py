import pytest

from agent_runtime.telemetry import Span, TelemetryConfig, TelemetryEngine


@pytest.fixture
def engine():
    return TelemetryEngine()


class TestSpan:
    def test_span_to_dict(self):
        s = Span(trace_id="t1", name="llm.call", attributes={"model": "test"})
        d = s.to_dict()
        assert d["trace_id"] == "t1"
        assert d["name"] == "llm.call"
        assert d["attributes"]["model"] == "test"
        assert d["status"] == "ok"
        assert len(d["span_id"]) == 16

    def test_span_from_dict(self):
        s = Span.from_dict(
            {"trace_id": "t1", "span_id": "s1", "name": "test", "status": "error"}
        )
        assert s.trace_id == "t1"
        assert s.status == "error"

    def test_span_auto_ids(self):
        s = Span(name="auto")
        assert s.span_id
        assert s.start_time > 0


class TestTelemetryConfig:
    def test_config_to_dict_redacts_headers(self):
        c = TelemetryConfig(headers={"Authorization": "Bearer secret123"})
        d = c.to_dict()
        assert d["headers"]["Authorization"] == "***"

    def test_config_from_dict(self):
        c = TelemetryConfig.from_dict(
            {"enabled": False, "endpoint": "http://otlp:4317", "sampling_rate": 0.5}
        )
        assert c.enabled is False
        assert c.endpoint == "http://otlp:4317"
        assert c.sampling_rate == 0.5


class TestTelemetryEngine:
    def test_configure(self, engine):
        engine.configure({"enabled": True, "endpoint": "http://localhost:4317"})
        assert engine.config.enabled is True
        assert engine.config.endpoint == "http://localhost:4317"

    def test_start_end_span(self, engine):
        span = engine.start_span("llm.call", trace_id="t1")
        assert span.name == "llm.call"
        assert span.trace_id == "t1"
        assert span.span_id in engine._spans

        ended = engine.end_span(span.span_id)
        assert ended is not None
        assert ended.end_time > 0
        assert ended.status == "ok"

    def test_end_unknown_span(self, engine):
        result = engine.end_span("unknown")
        assert result is None

    def test_get_trace(self, engine):
        s1 = engine.start_span("op1", trace_id="trace_1")
        s2 = engine.start_span("op2", trace_id="trace_1", parent_id=s1.span_id)
        engine.end_span(s1.span_id)
        engine.end_span(s2.span_id)

        trace = engine.get_trace("trace_1")
        assert trace is not None
        assert trace["span_count"] == 2
        assert engine.get_trace("nonexistent") is None

    def test_list_spans(self, engine):
        engine.start_span("a", trace_id="t1")
        engine.start_span("b", trace_id="t2")
        all_spans = engine.list_spans()
        assert len(all_spans) == 2

        t1_spans = engine.list_spans(trace_id="t1")
        assert len(t1_spans) == 1

        limited = engine.list_spans(limit=1)
        assert len(limited) == 1

    def test_export_json(self, engine):
        s = engine.start_span("test", trace_id="t1")
        engine.end_span(s.span_id)
        data = engine.export("json")
        assert '"spans"' in data
        assert '"t1"' in data

    def test_export_otlp(self, engine):
        s = engine.start_span("test", trace_id="t1")
        engine.end_span(s.span_id)
        data = engine.export("otlp")
        assert "resourceSpans" in data

    def test_export_console(self, engine):
        s = engine.start_span("test", trace_id="t1")
        engine.end_span(s.span_id)
        data = engine.export("console")
        assert "[test]" in data

    def test_metrics(self, engine):
        s = engine.start_span(
            "llm.call",
            trace_id="t1",
            attributes={"prompt_tokens": 100, "completion_tokens": 50},
        )
        engine.end_span(s.span_id)
        metrics = engine.metrics()
        assert metrics["counters"]["llm_calls"] == 1
        assert metrics["counters"]["tokens_prompt"] == 100
        assert metrics["counters"]["tokens_completion"] == 50
        assert metrics["total_spans"] >= 1

    def test_disabled_telemetry(self, engine):
        engine.configure({"enabled": False})
        span = engine.start_span("test")
        assert span.trace_id == ""
