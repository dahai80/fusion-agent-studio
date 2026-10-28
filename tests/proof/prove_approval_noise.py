"""#486 proof: approval-noise baseline vs armed engine, asks per 100 calls.

Fixed 100-call workload representative of a coding session:
  - 60 readonly calls (read_file / list_dir / search_code)
  - 20 loopback HTTP calls (http_request to 127.0.0.1 services)
  - 15 write calls (write_file)
  - 5 destructive calls (bash rm -rf)

Two arms on the SAME workload, same engine class:
  baseline — armed engine with auto-grading OFF (no readonly set,
             FUSION_PERMISSION_A3=0): every non-denied call asks.
  armed    — production wiring (readonly auto-allow + A3 loopback
             auto-allow): only writes ask; destructive is content-scan denied.

Runner: python tests/proof/prove_approval_noise.py
CI: proof job — exit 0 = pass (asserts noise reduction >= 70%)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.permissions import PermissionEngine  # noqa: E402
from agent_runtime.telemetry import TelemetryEngine  # noqa: E402

logger = logging.getLogger("prove_approval_noise")

READONLY_TOOLS = {"read_file", "list_dir", "search_code"}

WORKLOAD: list[tuple[str, dict, bool]] = (
    [("read_file", {"path": "/tmp/a.py"}, True)] * 30
    + [("list_dir", {"path": "/tmp"}, True)] * 15
    + [("search_code", {"pattern": "def main"}, True)] * 15
    + [
        (
            "http_request",
            {"url": "http://127.0.0.1:11434/v1/models", "method": "GET"},
            False,
        )
    ]
    * 20
    + [("write_file", {"path": "/tmp/out.py", "content": "x=1"}, False)] * 15
    + [("bash", {"command": "rm -rf /tmp/scratch"}, False)] * 5
)


def _run_workload(engine: PermissionEngine, use_hint: bool) -> dict:
    for tool, args, ro in WORKLOAD:
        engine.evaluate(tool, args, readonly_hint=ro if use_hint else False)
    return engine.stats()


def _asks_per_100(stats: dict) -> float:
    asks = stats.get("asks", 0)
    auto = stats.get("auto_allow_readonly", 0) + stats.get("auto_allow_loopback", 0)
    denies = stats.get("denies", 0)
    total = asks + auto + denies
    return (asks / total * 100.0) if total else 0.0


def prove_approval_noise() -> float:
    os.environ["FUSION_PERMISSION_PASSTHROUGH"] = "0"

    # baseline: auto-grading OFF — every non-denied call must ask.
    os.environ["FUSION_PERMISSION_A3"] = "0"
    baseline = PermissionEngine(readonly_tools=set())
    b_stats = _run_workload(baseline, use_hint=False)
    b_rate = _asks_per_100(b_stats)

    # armed: production wiring — readonly + loopback auto-allow.
    os.environ["FUSION_PERMISSION_A3"] = "1"
    armed = PermissionEngine(readonly_tools=READONLY_TOOLS)
    a_stats = _run_workload(armed, use_hint=True)
    a_rate = _asks_per_100(a_stats)
    os.environ.pop("FUSION_PERMISSION_A3", None)
    os.environ.pop("FUSION_PERMISSION_PASSTHROUGH", None)

    reduction = (b_rate - a_rate) / b_rate * 100.0 if b_rate else 0.0
    print(f"workload: {len(WORKLOAD)} calls (60 readonly / 20 loopback / 15 write / 5 destructive)")
    print(f"baseline asks/100-calls: {b_rate:.1f} (asks={b_stats.get('asks')}, denies={b_stats.get('denies')})")
    print(f"armed    asks/100-calls: {a_rate:.1f} (asks={a_stats.get('asks')}, "
          f"auto_allow={a_stats.get('auto_allow_readonly') + a_stats.get('auto_allow_loopback')}, "
          f"denies={a_stats.get('denies')})")
    print(f"approval-noise reduction: {reduction:.1f}%")

    # #486 AC3: the metric is reproducible from telemetry.metrics() gauges.
    engine = TelemetryEngine()
    engine.record_permission_noise(a_stats)
    gauges = engine.metrics()["gauges"]
    assert abs(gauges["approval_asks_per_100_calls"] - a_rate) < 0.01, gauges
    print(f"telemetry gauge approval_asks_per_100_calls={gauges['approval_asks_per_100_calls']:.1f} reproducible")

    assert reduction >= 70.0, f"noise reduction {reduction:.1f}% below -70% claim"
    return reduction


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    reduction = prove_approval_noise()
    print(f"PROOF OK: approval noise reduced {reduction:.1f}% (claim: >=70%)")
