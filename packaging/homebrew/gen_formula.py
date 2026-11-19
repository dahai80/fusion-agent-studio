# Issue #768: regenerate packaging/homebrew/fusion-agent-studio.rb from PyPI.
# Usage: python3 packaging/homebrew/gen_formula.py <version>  (writes the
# formula next to this script). Stdlib only.
#
# Wheel selection mirrors packaging/homebrew/README.md:
# - sdist for fusion-agent-studio itself
# - pydantic_core / pyyaml / psutil: cp312 (or abi3) macOS platform wheels,
#   split on_arm (arm64) / on_intel (x86_64); lowest macosx target for max
#   backward compatibility
# - setproctitle: cp312 macOS universal2 wheel
# - everything else: py3-none-any wheel
#
# The dependency closure must be resolved first (README step 1):
#   python3 -m venv /tmp/fs-brew-deps
#   /tmp/fs-brew-deps/bin/pip install fusion-agent-studio==<ver>
#   /tmp/fs-brew-deps/bin/pip freeze --all | grep -v '^pip==' > /tmp/fs-brew-deps.freeze
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

FORMULA = Path(__file__).resolve().parent / "fusion-agent-studio.rb"
FREEZE = "/tmp/fs-brew-deps.freeze"
PYPI = "https://pypi.org/pypi/{name}/{version}/json"

SPLIT_ARM = {"psutil", "pydantic_core", "pyyaml"}
UNIVERSAL = {"setproctitle"}


def _pypi_files(name: str, version: str) -> list[dict]:
    url = PYPI.format(name=name, version=version)
    last: Exception = RuntimeError("unreachable")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.load(resp)["urls"]
        except Exception as exc:  # transient SSL/proxy EOFs — retry with backoff
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise SystemExit(f"PyPI fetch failed for {name} {version}: {last}")


def _pick(candidates: list[dict], pattern: str) -> dict:
    matches = [f for f in candidates if re.search(pattern, f["filename"])]
    if not matches:
        raise SystemExit(f"no wheel matches {pattern!r} in {[f['filename'] for f in candidates]}")
    return sorted(matches, key=lambda f: f["filename"])[0]


def _entry(name: str, version: str) -> tuple[str, str, str]:
    files = _pypi_files(name, version)
    if name in SPLIT_ARM:
        plat = [
            f
            for f in files
            if ("cp312" in f["filename"] or "abi3" in f["filename"]) and "macosx" in f["filename"]
        ]
        arm = _pick(plat, r"arm64")
        intel = _pick(plat, r"x86_64")
        return arm["url"], arm["digests"]["sha256"], intel["url"] + " " + intel["digests"]["sha256"]
    if name in UNIVERSAL:
        w = _pick(files, r"cp312.*universal2")
        return w["url"], w["digests"]["sha256"], ""
    w = _pick(files, r"py3-none-any\.whl")
    return w["url"], w["digests"]["sha256"], ""


def _norm(name: str) -> str:
    return name.lower().replace("-", "_")


def main() -> None:
    version = sys.argv[1]
    pinned: dict[str, tuple[str, str]] = {}
    for line in open(FREEZE):
        line = line.strip()
        if not line or line.startswith("fusion-agent-studio=="):
            continue
        name, _, ver = line.partition("==")
        # key = PEP 503-normalized (freeze uses PyYAML-style odd cases);
        # value = canonical lowercase pip project name for resource blocks
        # (must match the platform_wheels list in the install block).
        pinned[_norm(name)] = (name.lower(), ver)

    header = f"""# Fusion-MLX Agent Studio — local agent development platform for Apple Silicon.
# Generated from PyPI {version} dependency resolution by gen_formula.py;
# regenerate on version bumps (see packaging/homebrew/README.md).
class FusionAgentStudio < Formula
  include Language::Python::Virtualenv

  desc "Local-first AI agent orchestration platform (daemon RPC + REST + CLI)"
  homepage "https://github.com/dahai80/fusion-agent-studio"
"""
    sdist = _pick(_pypi_files("fusion-agent-studio", version), r"\.tar\.gz$")
    body = [header]
    body.append(f'  url "{sdist["url"]}"\n')
    body.append(f'  sha256 "{sdist["digests"]["sha256"]}"\n')
    body.append('  license "Apache-2.0"\n\n  depends_on "python@3.12"\n\n')

    for key in sorted(pinned):
        if key in SPLIT_ARM:
            continue
        name, ver = pinned[key]
        url, sha, _ = _entry(key, ver)
        body.append(f'  resource "{name}" do\n    url "{url}"\n    sha256 "{sha}"\n  end\n')
    body.append("  on_macos do\n    on_arm do\n")
    for key in sorted(SPLIT_ARM):
        name, ver = pinned[key]
        url, sha, _ = _entry(key, ver)
        body.append(
            f'      resource "{name}" do\n        url "{url}"\n        sha256 "{sha}"\n      end\n'
        )
    body.append("    end\n    on_intel do\n")
    for key in sorted(SPLIT_ARM):
        name, ver = pinned[key]
        _, _, intel = _entry(key, ver)
        iurl, isha = intel.split(" ")
        body.append(
            f'      resource "{name}" do\n        url "{iurl}"\n        sha256 "{isha}"\n      end\n'
        )
    body.append("    end\n  end\n")

    tail = FORMULA.read_text()
    tail = tail[tail.index("  # virtualenv_install_with_resources") :]
    body.append("\n" + tail)
    FORMULA.write_text("".join(body))
    print(f"wrote {FORMULA} for {version} ({len(pinned)} resources)")


if __name__ == "__main__":
    main()
