from __future__ import annotations

from pathlib import Path

from app.job_store import JobStore


def test_job_store_round_trip(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    created = store.create({"input": {"id": "case-1"}})

    assert created["status"] == "queued"
    payload = store.input_for(created["job_id"])
    assert payload["input"]["id"] == "case-1"

    store.update(
        created["job_id"],
        status="completed",
        result={"status": "target_reached"},
    )
    completed = store.get(created["job_id"])
    assert completed["status"] == "completed"
    assert completed["result"]["status"] == "target_reached"


def test_job_store_lists_recent(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create({"case": 1})
    store.create({"case": 2})
    assert len(store.list_recent()) == 2


def test_job_store_marks_inflight_work_interrupted_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = JobStore(database)
    created = store.create({"case": "restart"})

    restarted = JobStore(database)
    recovered = restarted.get(created["job_id"])

    assert recovered["status"] == "interrupted"
    assert "重新提交" in recovered["error_message"]
