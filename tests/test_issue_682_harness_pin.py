"""#682/#701/#702: fusion-harness pin must admit the 0.11 discipline surface.

fusion-harness 0.10 added kernel primitives additively; 0.11.0 shipped
spawn_protocol (brief contract, #701) and artifact_channel / playbook /
cache_discipline (#702), which agent-studio consumes directly. Range after
this bump: >=0.11.0,<0.12.
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


def test_pin_admits_installed_discipline_surface():
    spec = SpecifierSet(_harness_pin())
    assert Version("0.11.0") in spec


def test_pin_still_bounds_minor_and_major():
    spec = SpecifierSet(_harness_pin())
    assert Version("0.10.1") not in spec
    assert Version("0.12.0") not in spec
    assert Version("1.0.0") not in spec
    assert Version("0.6.9") not in spec
