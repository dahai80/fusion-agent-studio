"""#350 N11: long-session SLO telemetry (duration / compactions / approval latency)."""


import pytest

from agent_runtime.telemetry import TelemetryEngine


def test_record_slo_compaction_counter():
    engine = TelemetryEngine()
    engine.record_slo("compaction")
    engine.record_slo("compaction")
    assert engine.metrics()["counters"]["compactions"] == 2


def test_record_slo_approval_latency():
    engine = TelemetryEngine()
    engine.record_slo("approval", 0.25)
    engine.record_slo("approval", 0.75)
    m = engine.metrics()
    assert m["counters"]["approvals"] == 2
    assert m["latencies"]["avg_approvals_ms"] == pytest.approx(500.0)


def test_record_slo_session_duration():
    engine = TelemetryEngine()
    engine.record_slo("session", 12.0)
    engine.record_slo("session", 30.0)
    s = engine.slo_summary()
    assert s["sessions_completed"] == 2
    assert s["avg_session_duration_ms"] == pytest.approx(21000.0)


def test_slo_summary_shape():
    engine = TelemetryEngine()
    s = engine.slo_summary()
    for key in (
        "sessions_completed",
        "compactions_total",
        "approvals_total",
        "avg_approval_latency_ms",
        "p99_approval_latency_ms",
        "avg_session_duration_ms",
        "p99_session_duration_ms",
    ):
        assert key in s


def test_record_slo_unknown_kind():
    engine = TelemetryEngine()
    engine.record_slo("bogus")
    assert engine.metrics()["counters"]["sessions_completed"] == 0


def test_metrics_rpc_includes_slo():
    engine = TelemetryEngine()
    engine.record_slo("session", 1.0)
    m = engine.metrics()
    assert "avg_sessions_ms" in m["latencies"]
    assert "avg_approvals_ms" in m["latencies"]


def test_record_approval_slo_helper():
    import time

    from agent_runtime._runtime_safety import _SafetyApprovalMixin

    mixin = _SafetyApprovalMixin.__new__(_SafetyApprovalMixin)
    mixin.telemetry_engine = TelemetryEngine()
    mixin._record_approval_slo(time.time() - 0.1)
    assert mixin.telemetry_engine.metrics()["counters"]["approvals"] == 1


def test_record_approval_slo_no_engine():
    import time

    from agent_runtime._runtime_safety import _SafetyApprovalMixin

    mixin = _SafetyApprovalMixin.__new__(_SafetyApprovalMixin)
    mixin.telemetry_engine = None
    mixin._record_approval_slo(time.time())
