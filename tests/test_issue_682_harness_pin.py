"""#682/#701/#702/#707: fusion-harness pin must admit the team kernel surface.

0.11.0 shipped spawn_protocol (brief contract, #701) and artifact_channel /
playbook / cache_discipline (#702). 0.12.0 shipped the team kernel —
TeamDefinition / TeamRuntime / TeamTaskBoard / mailbox (harness issue #31) —
which the teams.* dispatcher and lead tools consume directly (#707). Range
after this bump: >=0.12.0,<0.13.
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


def test_pin_admits_installed_team_kernel_surface():
    spec = SpecifierSet(_harness_pin())
    assert Version("0.12.0") in spec


def test_pin_still_bounds_minor_and_major():
    spec = SpecifierSet(_harness_pin())
    assert Version("0.11.0") not in spec
    assert Version("0.13.0") not in spec
    assert Version("1.0.0") not in spec
    assert Version("0.6.9") not in spec
