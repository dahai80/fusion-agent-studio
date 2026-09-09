"""M1-7 proof: crash-recovery reconcile correctness (#306).

Seeds crash state (running task + evidence jsonl), starts daemon, SIGKILLs
it, restarts, and asserts _reconcile_task_states restores task status from
evidence: completed-evidence → completed; started-only → needs_fix;
missing evidence_ref → needs_fix. Proves no task lost/duplicated on crash.

Runner: python tests/proof/prove_recovery.py
CI: pytest tests/proof/prove_recovery.py (exit 0 = pass)
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.task_store import Task, TaskStore  # noqa: E402

_PROOF_TEAM = "ops"


async def _rpc_call(socket_path, method, params=None, msg_id=1):
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=2**20)
    request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        request["params"] = params
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    data = await asyncio.wait_for(reader.readline(), timeout=15.0)
    writer.close()
    await writer.wait_closed()
    return json.loads(data)


def _wait_for_socket(socket_path, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(socket_path):
            try:
                asyncio.run(_rpc_call(socket_path, "ping"))
                return True
            except Exception:
                pass
        time.sleep(0.4)
    return False


def _start_daemon(tmpdir, socket_path, task_db, out_dir):
    env = dict(os.environ)
    env["FUSION_TASK_DB"] = task_db
    env["FUSION_OUT_DIR"] = out_dir
    env["FUSION_ENABLE_WS"] = "0"
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "agent_runtime.daemon_server",
            "--socket-path", socket_path,
            "--store-path", str(Path(tmpdir) / "store.db"),
            "--ws-port", "0",
            "--cluster-port", "0",
            "--http-port", "0",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_socket(socket_path):
        proc.kill()
        raise RuntimeError("daemon did not start (socket timeout)")
    return proc


def _seed_crash_state(tmpdir, task_db, out_dir):
    store = TaskStore(db_path=task_db)
    team = _PROOF_TEAM

    # T1: completed evidence but task still running (crash before writeback).
    t1 = store.submit(
        Task(title="recovery-completed", graph_id="g-x", team=team)
    )
    store.update_status(t1.task_id, "running")
    ev1 = Path(out_dir) / team / "evidence" / "executions" / f"{t1.task_id}_ev.jsonl"
    ev1.parent.mkdir(parents=True, exist_ok=True)
    with open(ev1, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "execution.started", "execution_id": t1.task_id + "_ev"}) + "\n")
        f.write(json.dumps({"type": "execution.completed", "execution_id": t1.task_id + "_ev", "events_count": 3, "artifact_ids": []}) + "\n")
    store.set_evidence(t1.task_id, str(ev1))

    # T2: started-only evidence (crash mid-execution).
    t2 = store.submit(
        Task(title="recovery-midflight", graph_id="g-x", team=team)
    )
    store.update_status(t2.task_id, "running")
    ev2 = Path(out_dir) / team / "evidence" / "executions" / f"{t2.task_id}_ev.jsonl"
    ev2.write_text(
        json.dumps({"type": "execution.started", "execution_id": t2.task_id + "_ev"}) + "\n",
        encoding="utf-8",
    )
    store.set_evidence(t2.task_id, str(ev2))

    # T3: running but no evidence_ref (crash before evidence write).
    t3 = store.submit(
        Task(title="recovery-noevidence", graph_id="g-x", team=team)
    )
    store.update_status(t3.task_id, "running")

    store.close()
    return t1.task_id, t2.task_id, t3.task_id


def prove_recovery():
    tmpdir = tempfile.mkdtemp(prefix="proof_recovery_")
    socket_path = str(Path(tmpdir) / "daemon.sock")
    task_db = str(Path(tmpdir) / "tasks.db")
    out_dir = str(Path(tmpdir) / "out")

    t1, t2, t3 = _seed_crash_state(tmpdir, task_db, out_dir)

    # D1: start → reconcile runs on startup.
    d1 = _start_daemon(tmpdir, socket_path, task_db, out_dir)
    time.sleep(1.5)

    # SIGKILL D1 (crash simulation).
    d1.send_signal(signal.SIGKILL)
    d1.wait(timeout=10)
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    # D2: restart → reconcile runs again (idempotent).
    d2 = _start_daemon(tmpdir, socket_path, task_db, out_dir)
    time.sleep(1.5)

    # Assert via direct TaskStore read.
    store = TaskStore(db_path=task_db)

    def _get(tid):
        rows = store._conn.execute(
            "SELECT task_id, status, review_state, evidence_ref FROM tasks WHERE task_id = ?",
            (tid,),
        ).fetchall()
        return rows[0] if rows else None

    r1 = _get(t1)
    r2 = _get(t2)
    r3 = _get(t3)

    assert r1 is not None, "T1 lost"
    assert r1[1] == "completed", f"T1 expected completed, got {r1[1]}"
    assert r2 is not None, "T2 lost"
    assert r2[1] == "running", f"T2 expected running (resumable), got {r2[1]}"
    assert r2[2] == "needs_fix", f"T2 expected needs_fix, got {r2[2]}"
    assert r3 is not None, "T3 lost"
    assert r3[2] == "needs_fix", f"T3 expected needs_fix, got {r3[2]}"

    # No duplicate tasks created.
    total = store._conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE team = ?", (_PROOF_TEAM,)
    ).fetchone()[0]
    store.close()

    assert total == 3, f"expected 3 tasks, got {total} (duplicate after restart?)"

    # Cleanup.
    d2.terminate()
    try:
        d2.wait(timeout=5)
    except Exception:
        d2.kill()
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    print(
        f"PROOF recovery PASS: T1 {r1[1]} (evidence), T2 {r2[1]}/{r2[2]} (needs_fix), "
        f"T3 {r3[2]} (needs_fix), {total} tasks (no dup), 2 daemon cycles"
    )
    return True


if __name__ == "__main__":
    ok = prove_recovery()
    sys.exit(0 if ok else 1)
