# Issue #768: fail CI when the in-repo Homebrew formula drifts.
# Checks (network: pypi.org JSON API):
#   1. formula sdist version == pyproject.toml project version
#   2. every url/sha256 in the formula (sdist + all resources) matches the
#      checksum PyPI reports for that exact filename
# Usage: python3 packaging/homebrew/check_formula.py
from __future__ import annotations

import json
import re
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORMULA = ROOT / "packaging" / "homebrew" / "fusion-agent-studio.rb"
PYPROJECT = ROOT / "pyproject.toml"

URL_RE = re.compile(r'url "(https://files\.pythonhosted\.org/packages/[^"]+)"')
SHA_RE = re.compile(r'sha256 "([0-9a-f]{64})"')


def _pypi_sha256(filename: str) -> str:
    # sdist: <dist>-<version>.tar.gz — wheel: <dist>-<version>-<tags>.whl
    if filename.endswith(".tar.gz"):
        dist, version = filename[: -len(".tar.gz")].rsplit("-", 1)
    else:
        # wheel: <dist>-<version>-<py tag>-<abi tag>-<platform tag>.whl
        dist_ver, _py, _abi, _plat = filename[: -len(".whl")].rsplit("-", 3)
        dist, version = dist_ver.rsplit("-", 1)
    url = f"https://pypi.org/pypi/{dist}/{version}/json"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.load(resp)
            break
        except Exception as exc:
            if attempt == 3:
                raise SystemExit(f"PyPI fetch failed for {dist} {version}: {exc}")
            time.sleep(1.5 * (attempt + 1))
    for f in data["urls"]:
        if f["filename"] == filename:
            return f["digests"]["sha256"]
    raise SystemExit(f"file {filename!r} not in PyPI {dist} {version} release")


def main() -> None:
    errors: list[str] = []
    text = FORMULA.read_text()
    formula_version = ""
    m = re.search(r"fusion_agent_studio-([\d.]+)\.tar\.gz", text)
    if m:
        formula_version = m.group(1)
    else:
        errors.append("formula sdist url not found")

    pyproject_version = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    if formula_version and formula_version != pyproject_version:
        errors.append(
            f"formula sdist version {formula_version} != pyproject version {pyproject_version} "
            "(run packaging/homebrew/gen_formula.py after the release)"
        )

    urls = URL_RE.findall(text)
    shas = SHA_RE.findall(text)
    if len(urls) != len(shas):
        errors.append(f"formula has {len(urls)} urls but {len(shas)} sha256 lines")
    for url, sha in zip(urls, shas):
        filename = url.rsplit("/", 1)[1]
        expected = _pypi_sha256(filename)
        if expected != sha:
            errors.append(f"checksum mismatch for {filename}: formula {sha} != PyPI {expected}")

    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        sys.exit(1)
    print(f"formula OK: version {formula_version}, {len(urls)} pinned files verified against PyPI")


if __name__ == "__main__":
    main()
