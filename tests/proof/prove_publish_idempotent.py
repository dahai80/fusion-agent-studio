"""M1-7 proof: publish idempotency across crash/restart (#306).

Two separate processes submit a task with the same idempotency_key against
a shared TaskStore DB. Asserts the second submit returns the existing task
(dedup), not a duplicate — proving cron-trigger re-fire after daemon restart
won't create duplicate tasks. Mirrors M1-5 cron handler idempotency_key=
cron:{job_id}:{fire_time}.

Runner: python tests/proof/prove_publish_idempotent.py
CI: pytest tests/proof/prove_publish_idempotent.py (exit 0 = pass)
"""

from __future__ import annotations

import multiprocessing as mp
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.task_store import Task, TaskStore  # noqa: E402


def _worker_submit(args):
    db_path, idem_key, owner, result_file = args
    store = TaskStore(db_path=db_path)
    task = store.submit(
        Task(
            title=f"idempotent-{owner}",
            graph_id="g-proof",
            team="ops",
            idempotency_key=idem_key,
        )
    )
    deduped = store.last_submit_deduped
    with open(result_file, "a") as f:
        f.write(f"{owner}:{task.task_id}:{deduped}\n")
    store.close()
    return task.task_id


def prove_publish_idempotent():
    tmpdir = tempfile.mkdtemp(prefix="proof_idem_")
    db_path = str(Path(tmpdir) / "tasks.db")
    idem_key = "cron:job-proof:1700000000"

    # P1: first submit (creates task).
    store = TaskStore(db_path=db_path)
    t1 = store.submit(
        Task(
            title="idempotent-seed",
            graph_id="g-proof",
            team="ops",
            idempotency_key=idem_key,
        )
    )
    assert not store.last_submit_deduped, "first submit should not dedup"
    seed_id = t1.task_id
    store.close()

    # P2: restart simulation — new process, same DB, same idempotency_key.
    result_file = str(Path(tmpdir) / "results.txt")
    open(result_file, "w").close()
    ctx = mp.get_context("fork")
    with ctx.Pool(1) as pool:
        pool.map(
            _worker_submit,
            [(db_path, idem_key, "restart", result_file)],
        )

    with open(result_file) as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    assert len(lines) == 1, f"expected 1 result, got {len(lines)}"
    owner, restart_id, deduped = lines[0].split(":", 2)
    assert deduped == "True", f"restart submit should dedup, got deduped={deduped}"
    assert restart_id == seed_id, (
        f"restart submit returned {restart_id}, expected existing {seed_id}"
    )

    # Cross-process concurrent race: 3 processes submit same key simultaneously.
    open(result_file, "w").close()
    with ctx.Pool(3) as pool:
        pool.map(
            _worker_submit,
            [
                (db_path, idem_key, "race-A", result_file),
                (db_path, idem_key, "race-B", result_file),
                (db_path, idem_key, "race-C", result_file),
            ],
        )

    race_ids = []
    with open(result_file) as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                _, tid, _ = ln.split(":", 2)
                race_ids.append(tid)

    # All 3 concurrent submits must return the SAME existing task_id.
    assert len(set(race_ids)) == 1, (
        f"concurrent submit diverged: {race_ids} (expected all = {seed_id})"
    )
    assert race_ids[0] == seed_id, (
        f"concurrent submit returned {race_ids[0]}, expected {seed_id}"
    )

    # Only 1 task row with that idempotency_key in DB.
    store = TaskStore(db_path=db_path)
    rows = store._conn.execute(
        "SELECT task_id FROM idempotency_index WHERE idempotency_key = ?",
        (idem_key,),
    ).fetchall()
    task_rows = store._conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
        (idem_key,),
    ).fetchone()[0]
    store.close()

    assert len(rows) == 1, f"expected 1 idempotency_index row, got {len(rows)}"
    assert task_rows == 1, f"expected 1 task row, got {task_rows}"

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    print(
        f"PROOF publish_idempotent PASS: key={idem_key}, "
        f"seed={seed_id}, restart deduped, 3-way concurrent all={race_ids[0]}, "
        f"1 row in DB (no duplicate after restart)"
    )
    return True


if __name__ == "__main__":
    ok = prove_publish_idempotent()
    sys.exit(0 if ok else 1)
