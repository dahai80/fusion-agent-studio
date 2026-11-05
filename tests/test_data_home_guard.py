"""#591: data-home pollution guard.

Baseline snapshot of the real ~/.fusion-agent-studio is taken at module
import time (before any test runs; conftest already redirected
FUSION_AGENT_STUDIO_HOME). This module is moved to the END of the
collection by conftest's pytest_collection_modifyitems, so the test body
runs after the whole suite and fails if anything wrote outside the
redirected temp home. Log files are exempt (acceptance: "except log
files") because a live daemon on a dev machine keeps appending to them.
"""

from __future__ import annotations

import os
from pathlib import Path

REAL_HOME = Path.home() / ".fusion-agent-studio"

IGNORE_PARTS = {"logs"}
IGNORE_SUFFIXES = (".log", ".sock")


def _snapshot() -> dict:
    out = {}
    if not REAL_HOME.is_dir():
        return out
    for root, dirs, files in os.walk(REAL_HOME):
        rel_root = Path(root).relative_to(REAL_HOME)
        if any(part in IGNORE_PARTS for part in rel_root.parts):
            dirs[:] = []
            continue
        for name in dirs:
            p = Path(root) / name
            rel = p.relative_to(REAL_HOME)
            if any(part in IGNORE_PARTS for part in rel.parts):
                continue
            out[str(rel)] = (p.stat().st_mtime_ns, -1)
        for name in files:
            if name.endswith(IGNORE_SUFFIXES):
                continue
            p = Path(root) / name
            rel = p.relative_to(REAL_HOME)
            if any(part in IGNORE_PARTS for part in rel.parts):
                continue
            st = p.stat()
            out[str(rel)] = (st.st_mtime_ns, st.st_size)
    return out


BASELINE = _snapshot()


def test_real_data_home_untouched_by_suite():
    # A live studio daemon on the dev machine legitimately writes to the
    # real home (cron.db WAL, audit.db, logs) while the suite runs — the
    # guard can only be meaningful on a quiet CI runner.
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 11442), timeout=0.5):
            import pytest

            pytest.skip("live studio daemon on :11442; guard enforced in CI")
    except OSError:
        pass

    after = _snapshot()
    added = sorted(set(after) - set(BASELINE))
    removed = sorted(set(BASELINE) - set(after))
    changed = sorted(
        k for k in set(BASELINE) & set(after) if BASELINE[k] != after[k]
    )
    problems = []
    if added:
        problems.append(f"added: {added[:20]}")
    if removed:
        problems.append(f"removed: {removed[:20]}")
    if changed:
        problems.append(f"modified: {changed[:20]}")
    assert not problems, (
        "tests wrote to the real ~/.fusion-agent-studio (#591): "
        + "; ".join(problems)
    )
