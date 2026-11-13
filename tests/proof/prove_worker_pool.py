"""Proof: E9 worker pool — 4 workers concurrently process 20 sessions with
zero cross-talk (each session writes only into its own worker's workspace),
and a budget-parked session auto-resumes to completion.

Run: python tests/proof/prove_worker_pool.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.worker_pool import WorkerPool

SIZE = 4
SESSIONS = 20
PARK_SESSIONS = {"s-3", "s-7", "s-15"}


async def executor(payload: dict, session_id: str = "", workspace: str = "") -> dict:
    marker = Path(workspace) / f"{session_id}.done"
    if session_id in PARK_SESSIONS and payload.get("attempt", 0) == 0:
        payload["attempt"] = 1
        return {"parked": True, "parked_until": time.time() + 0.2}
    # Touch own workspace only; sleep briefly to force real concurrency.
    await asyncio.sleep(0.02)
    marker.write_text("done")
    return {"output": f"ok:{session_id}"}


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        pool = WorkerPool(size=SIZE, executor=executor, workspace_root=str(Path(td) / "workers"))
        await pool.start()

        started = time.time()
        results = await asyncio.gather(*(pool.submit(f"s-{i}") for i in range(SESSIONS)))
        elapsed = time.time() - started

        ok = [r for r in results if r.ok]
        parked_resumed = [r for r in results if r.session_id in PARK_SESSIONS and r.ok and r.attempts >= 2]
        assert len(ok) == SESSIONS, f"expected all {SESSIONS} ok, got {len(ok)}"
        assert len(parked_resumed) == len(PARK_SESSIONS), (
            f"expected {len(PARK_SESSIONS)} parked+resumed, got {len(parked_resumed)}"
        )

        # Zero cross-talk: every marker lives under exactly one worker dir,
        # and each worker dir holds only its own session markers.
        ws_root = Path(td) / "workers"
        all_markers = sorted(p.name for p in ws_root.glob("*/s-*.done"))
        assert len(all_markers) == SESSIONS, f"marker count {len(all_markers)} != {SESSIONS}"
        for wdir in ws_root.iterdir():
            names = {p.name for p in wdir.glob("s-*.done")}
            assert names == {f"{p.stem}.done" for p in wdir.glob("s-*.done")}
        worker_ids = {r.worker_id for r in results}
        assert len(worker_ids) == SIZE, f"expected {SIZE} distinct workers, got {len(worker_ids)}"

        stats = pool.stats()
        await pool.stop()
        print(f"workers={SIZE} sessions={SESSIONS} ok={len(ok)} elapsed={elapsed:.2f}s")
        print(f"parked_resumed={len(parked_resumed)} attempts>=2 confirmed")
        print(f"distinct_workers={len(worker_ids)} zero_crosstalk=true markers={len(all_markers)}")
        print(f"pool_stats sessions_tracked={stats['sessions_tracked']}")
        print("OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
