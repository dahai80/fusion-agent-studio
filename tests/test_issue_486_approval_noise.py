"""#486: approval-noise metric (asks per 100 calls) in telemetry gauges."""
from agent_runtime.telemetry import TelemetryEngine


def test_record_permission_noise_computes_rate():
    engine = TelemetryEngine()
    rate = engine.record_permission_noise({
        "asks": 15,
        "auto_allow_readonly": 60,
        "auto_allow_loopback": 20,
        "denies": 5,
    })
    assert rate == 15.0
    gauges = engine.metrics()["gauges"]
    assert gauges["approval_asks_per_100_calls"] == 15.0
    assert gauges["permission_decisions"] == 100.0


def test_record_permission_noise_zero_decisions():
    engine = TelemetryEngine()
    rate = engine.record_permission_noise({})
    assert rate == 0.0
    assert engine.metrics()["gauges"]["approval_asks_per_100_calls"] == 0.0


def test_record_permission_noise_all_asks():
    engine = TelemetryEngine()
    rate = engine.record_permission_noise({"asks": 95, "denies": 5})
    assert rate == 95.0


def test_metrics_gauges_isolated_copy():
    engine = TelemetryEngine()
    engine.record_permission_noise({"asks": 1, "auto_allow_readonly": 9})
    m = engine.metrics()
    m["gauges"]["approval_asks_per_100_calls"] = 999.0
    assert engine.metrics()["gauges"]["approval_asks_per_100_calls"] == 10.0


def test_proof_workload_reduction():
    from tests.proof.prove_approval_noise import prove_approval_noise

    reduction = prove_approval_noise()
    assert reduction >= 70.0
