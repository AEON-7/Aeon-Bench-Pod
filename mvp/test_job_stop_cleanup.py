"""Regression tests for job cancellation and resource cleanup.

Run directly with `python test_job_stop_cleanup.py` or collect with pytest.
"""
from __future__ import annotations

import collections
import os
import sys
import tempfile
import threading
from unittest import mock

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from aeon import db  # noqa: E402
from pod import jobs  # noqa: E402


class FakeProc:
    pid = 4242

    def poll(self):
        return None


def _job(*, status="running", started=True, owns_serve=True, run_ids=None):
    ids = list(run_ids or [])
    return {
        "id": "test-job",
        "status": status,
        "stage": status,
        "error": None,
        "updated_at": 0,
        "run_id": ids[0] if ids else None,
        "_run_ids": ids,
        "log": collections.deque(maxlen=50),
        "_proc": FakeProc() if started else None,
        "_started": started,
        "_stop_requested": False,
        "_owns_serve": owns_serve,
        "_runtime_cleaned": False,
        "_runtime_cleanup_started": False,
        "_runtime_cleanup_event": threading.Event(),
    }


def test_running_job_stop_kills_and_removes_owned_serve():
    j = _job(run_ids=["text-run", "vision-run"])
    with jobs._LOCK:
        jobs._JOBS[j["id"]] = j
    calls = []
    docker_result = mock.Mock(returncode=0, stderr="", stdout="aeon-bench-serve")
    try:
        with mock.patch.object(jobs, "_terminate_process_tree",
                               side_effect=lambda proc: calls.append(("kill", proc.pid))), \
             mock.patch.object(jobs, "_finish_stopped_run",
                               side_effect=lambda job: calls.append(("finish", tuple(job["_run_ids"])))), \
             mock.patch.object(jobs.subprocess, "run", return_value=docker_result) as run:
            assert jobs.stop_job(j["id"]) is True
        assert j["status"] == "stopped" and j["stage"] == "stopped"
        assert j["_stop_requested"] is True and j["_runtime_cleaned"] is True
        assert calls == [("kill", 4242), ("finish", ("text-run", "vision-run"))]
        run.assert_called_once_with(
            ["docker", "rm", "-f", "aeon-bench-serve"],
            capture_output=True, text=True, timeout=120)
    finally:
        with jobs._LOCK:
            jobs._JOBS.pop(j["id"], None)


def test_queued_job_stop_never_removes_active_jobs_serve():
    j = _job(status="queued", started=False, owns_serve=True)
    with jobs._LOCK:
        jobs._JOBS[j["id"]] = j
    try:
        with mock.patch.object(jobs, "_terminate_process_tree") as terminate, \
             mock.patch.object(jobs, "_finish_stopped_run") as finish, \
             mock.patch.object(jobs.subprocess, "run") as run:
            assert jobs.stop_job(j["id"]) is True
        assert j["status"] == "stopped"
        terminate.assert_not_called()
        finish.assert_not_called()
        run.assert_not_called()
    finally:
        with jobs._LOCK:
            jobs._JOBS.pop(j["id"], None)


def test_finish_stopped_run_closes_every_board_run():
    j = _job(run_ids=["text-run", "vision-run", "text-run"])
    closed = []
    with mock.patch.object(db, "fail_run_if_running",
                           side_effect=lambda rid, reason: closed.append((rid, reason))):
        jobs._finish_stopped_run(j)
    assert closed == [("text-run", "stopped by user"),
                      ("vision-run", "stopped by user")]


def test_fail_run_if_running_is_atomic():
    old = (db.DB_PATH, db.AEON_DB_URL, db.IS_PG, db._initialized)
    try:
        with tempfile.TemporaryDirectory() as td:
            db.DB_PATH = os.path.join(td, "pod.db")
            db.AEON_DB_URL = None
            db.IS_PG = False
            db._initialized = False
            db.init_db()
            with db.connect() as c:
                c.execute("INSERT INTO runs (id, model, target_url, status) VALUES (?,?,?,?)",
                          ("running", "m", "u", "running"))
                c.execute("INSERT INTO runs (id, model, target_url, status) VALUES (?,?,?,?)",
                          ("done", "m", "u", "succeeded"))
            assert db.fail_run_if_running("running", "stopped by user") is True
            assert db.fail_run_if_running("done", "stopped by user") is False
            assert db.get_run("running")["status"] == "failed"
            assert db.get_run("running")["error"] == "stopped by user"
            assert db.get_run("done")["status"] == "succeeded"
    finally:
        db.DB_PATH, db.AEON_DB_URL, db.IS_PG, db._initialized = old


def main():
    tests = [
        test_running_job_stop_kills_and_removes_owned_serve,
        test_queued_job_stop_never_removes_active_jobs_serve,
        test_finish_stopped_run_closes_every_board_run,
        test_fail_run_if_running_is_atomic,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"ALL GREEN ({len(tests)}/{len(tests)})")


if __name__ == "__main__":
    main()
