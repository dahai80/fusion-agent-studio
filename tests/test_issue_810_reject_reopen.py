from agent_runtime.task_store import Task, TaskStore


def _store(tmp_path) -> TaskStore:
    return TaskStore(db_path=str(tmp_path / "tasks.db"))


def _task(task_id: str, team: str = "t1", depends_on: list[str] | None = None) -> Task:
    return Task(
        task_id=task_id,
        title=f"task {task_id}",
        team=team,
        depends_on=list(depends_on or []),
    )


def test_reject_marks_needs_fix_and_attaches_reason(tmp_path):
    store = _store(tmp_path)
    store.submit(_task("prev"))
    store.submit(_task("next", depends_on=["prev"]))

    result = store.reject_task("next", reason="verdict: reject, scope wrong")

    task = store.get("next")
    assert task.review_state == "needs_fix"
    assert task.last_error == "verdict: reject, scope wrong"
    assert result["task_id"] == "next"
    assert result["reopened"] is False


def test_reject_reopens_completed_dependency_with_reason(tmp_path):
    store = _store(tmp_path)
    store.submit(_task("prev"))
    store.update_status("prev", "completed")
    store.submit(_task("next", depends_on=["prev"]))

    result = store.reject_task("next", reason="verdict: reject, bad contract")

    prev = store.get("prev")
    assert result["reopened"] is True
    assert result["reopened_task_id"] == "prev"
    assert prev.status == "pending"
    assert "[reject] verdict: reject, bad contract" in prev.description


def test_reject_resets_dependency_review_state(tmp_path):
    # Approved gate (#811) must re-block after reopen: reset review_state to none.
    store = _store(tmp_path)
    store.submit(_task("prev"))
    store.update_status("prev", "completed")
    store.set_review_state("prev", "approved")
    store.submit(_task("next", depends_on=["prev"]))

    store.reject_task("next", reason="bad")

    prev = store.get("prev")
    assert prev.review_state == "none"


def test_reject_named_reopen_target(tmp_path):
    store = _store(tmp_path)
    store.submit(_task("a"))
    store.submit(_task("b"))
    store.update_status("b", "completed")
    store.submit(_task("c", depends_on=["a"]))

    result = store.reject_task("c", reason="redo b", reopen_task_id="b")

    assert result["reopened_task_id"] == "b"
    assert result["reopened"] is True
    assert store.get("b").status == "pending"
    assert store.get("a").status == "pending"


def test_reject_reopen_target_not_completed_still_resets(tmp_path):
    # Target still open (running): no reopen flag, but reason + review reset apply.
    store = _store(tmp_path)
    store.submit(_task("prev"))
    store.update_status("prev", "running")
    store.set_review_state("prev", "approved")
    store.submit(_task("next", depends_on=["prev"]))

    result = store.reject_task("next", reason="fix contract")

    prev = store.get("prev")
    assert result["reopened"] is False
    assert prev.status == "running"
    assert "[reject] fix contract" in prev.description
    assert prev.review_state == "none"


def test_reject_missing_task(tmp_path):
    store = _store(tmp_path)
    result = store.reject_task("nope", reason="x")
    assert result["status"] == "error"
