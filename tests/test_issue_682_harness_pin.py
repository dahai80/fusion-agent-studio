"""#682: fusion-harness pin must admit the 0.10.x kernel surface.

fusion-harness 0.10 adds subagent/checkpoint/orchestrator primitives additively;
the old <0.8 pin forced pip check conflicts in the shared monorepo venv
(fusion-cowork#467). Range after the fix: >=0.7.0,<0.11.
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


def test_pin_admits_installed_0_10_surface():
    spec = SpecifierSet(_harness_pin())
    assert Version("0.10.1") in spec
    assert Version("0.7.0") in spec


def test_pin_still_bounds_major():
    spec = SpecifierSet(_harness_pin())
    assert Version("0.11.0") not in spec
    assert Version("1.0.0") not in spec
    assert Version("0.6.9") not in spec
