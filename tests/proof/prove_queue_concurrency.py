"""M1-7 proof: concurrent dequeue safety (#300).

4 parallel processes dequeue 20 pre-seeded tasks. Asserts zero duplicate
ownership — proves SQLite UPDATE...RETURNING row-lock concurrency.

Runner: python tests/proof/prove_queue_concurrency.py
CI: pytest tests/proof/prove_queue_concurrency.py (exit 0 = pass)
"""

from __future__ import annotations

import multiprocessing as mp
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.task_store import Task, TaskStore  # noqa: E402


def _worker_dequeue(args):
    db_path, team, owner, claimed_file = args
    store = TaskStore(db_path=db_path)
    claimed = []
    while True:
        task = store.dequeue(team, owner)
        if task is None:
            break
        claimed.append(task.task_id)
    with open(claimed_file, "a") as f:
        for tid in claimed:
            f.write(f"{owner}:{tid}\n")
    store.close()
    return len(claimed)


def prove_queue_concurrency():
    tmpdir = tempfile.mkdtemp(prefix="proof_queue_")
    db_path = str(Path(tmpdir) / "tasks.db")
    store = TaskStore(db_path=db_path)

    # Pre-seed 20 pending todo tasks.
    team = "ops"
    for i in range(20):
        store.submit(Task(title=f"proof-task-{i}", graph_id="g-proof", team=team))
    store.close()

    claimed_file = str(Path(tmpdir) / "claimed.txt")
    open(claimed_file, "w").close()

    workers = ["agent-A", "agent-B", "agent-C", "agent-D"]
    args = [(db_path, team, w, claimed_file) for w in workers]

    start = time.time()
    ctx = mp.get_context("fork")
    with ctx.Pool(4) as pool:
        pool.map(_worker_dequeue, args)
    elapsed = time.time() - start

    # Collect claims.
    claims = []
    with open(claimed_file) as f:
        for line in f:
            line = line.strip()
            if line:
                owner, tid = line.split(":", 1)
                claims.append((owner, tid))

    store = TaskStore(db_path=db_path)
    rows = store._conn.execute(
        "SELECT task_id, owner_agent, status FROM tasks WHERE team = ?",
        (team,),
    ).fetchall()
    owners = {}
    for tid, owner, status in rows:
        if status == "running":
            owners.setdefault(owner, []).append(tid)
    history_rows = store._conn.execute("SELECT COUNT(*) FROM task_history").fetchone()[0]
    store.close()

    # Assertions.
    assert len(claims) == 20, f"expected 20 claims, got {len(claims)}"
    task_ids = [c[1] for c in claims]
    assert len(set(task_ids)) == 20, f"duplicate claims: {len(task_ids)} claims, {len(set(task_ids))} unique"
    for owner, group in owners.items():
        assert len(set(group)) == len(group), f"owner {owner} has duplicate task_ids: {group}"
    assert history_rows >= 20, f"expected >=20 history rows, got {history_rows}"
    assert elapsed < 5.0, f"took {elapsed:.1f}s, expected <5s"

    # Cleanup.
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"PROOF queue_concurrency PASS: 20 tasks, 4 workers, 0 duplicates, {history_rows} history rows, {elapsed:.2f}s")
    return True


if __name__ == "__main__":
    ok = prove_queue_concurrency()
    sys.exit(0 if ok else 1)
