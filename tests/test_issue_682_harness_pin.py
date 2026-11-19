"""#682/#701/#702/#707/#714/#716/#748: fusion-harness pin must admit the kernel surface.

0.11.0 shipped spawn_protocol (brief contract, #701) and artifact_channel /
playbook / cache_discipline (#702). 0.12.0 shipped the team kernel —
TeamDefinition / TeamRuntime / TeamTaskBoard / mailbox (harness issue #31) —
which the teams.* dispatcher and lead tools consume directly (#707).
0.13.0 shipped checkpoint_binding / scoped_permissions / dag_node /
trajectory (harness K1-K3, issues #34/#35/#36) — checkpoint binding is the
#714 part-1 re-point target. 0.14.0 shipped the C1 TeamMessage extension
(msg_id/reply_to/mentions/attachments/audio_ref+transcript, harness issue
#38) and dag_node lenient coercion (#39) — the teams.* chat passthrough
(#716) targets it. 0.20.0 is the batch release for harness #63-#65 — per-run
event streams, steer_member supplement queue, and task wakeup rules
(add_wakeup) — consumed by the run replay RPCs (#750), mid-run steering
(#748), and wakeup scheduling (#749). 0.21.0 ships HumanGate generic queue
surface (harness #66: list_questions / single-winner resolve) consumed by
the #557 human_input node queue mechanics (#756). 0.22.0 ships the
destructive_gate ApprovalGate (classify / request / decide / issue_token /
redeem) wired into the funnel admission path (#770) and artifact_scan
(scan_artifact / ArtifactQuarantine / stamp_untrusted) wired at the
artifact_bridge load boundary (#770). 0.26.0 ships the P-series compaction
knobs — CompactionConfig.trigger_ratio / should_auto_compact (#122) and the
post-compaction validator hook + compaction_validated event (#131) — wired
through the AS Compactor.should_compact choke point and
kernel_wiring.build_compaction_config (#775). Versions 0.17.0-0.25.x predate
the validator surface and are excluded. Range after this bump:
>=0.26.0,<0.27.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version


def _harness_pin() -> str:
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    deps = [d for d in data["project"]["dependencies"] if d.startswith("fusion-harness")]
    assert len(deps) == 1
    return deps[0].split("fusion-harness", 1)[1].strip()


def test_pin_admits_installed_kernel_surface():
    spec = SpecifierSet(_harness_pin())
    assert Version("0.26.0") in spec


def test_pin_still_bounds_minor_and_major():
    spec = SpecifierSet(_harness_pin())
    assert Version("0.11.0") not in spec
    assert Version("0.13.0") not in spec
    assert Version("0.14.0") not in spec
    assert Version("0.16.0") not in spec
    assert Version("0.19.0") not in spec
    assert Version("0.20.0") not in spec
    assert Version("0.22.0") not in spec
    assert Version("0.25.0") not in spec
    assert Version("0.27.0") not in spec
    assert Version("1.0.0") not in spec
    assert Version("0.6.9") not in spec
