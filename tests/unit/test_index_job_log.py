"""Unit tests for durable indexing job log."""

import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from context_engine.indexer.job_log import IndexJobLog


class TestIndexJobLog:
    def test_track_file_job_marks_completed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = IndexJobLog(f"{tmpdir}/jobs.sqlite3")

            with log.track_file_job("/repo/app.py", file_hash="abc123") as job_id:
                assert log.get_job(job_id)["status"] == "running"

            job = log.get_job(job_id)
            assert job["target"] == "/repo/app.py"
            assert job["target_hash"] == "abc123"
            assert job["status"] == "completed"
            assert job["attempts"] == 1
            assert job["last_error"] == ""

    def test_track_file_job_marks_failed_on_exception(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = IndexJobLog(f"{tmpdir}/jobs.sqlite3", max_attempts=3)

            with pytest.raises(RuntimeError, match="lancedb write failed"):
                with log.track_file_job("/repo/app.py"):
                    raise RuntimeError("lancedb write failed")

            jobs = log.list_jobs()
            assert len(jobs) == 1
            assert jobs[0]["status"] == "failed"
            assert jobs[0]["attempts"] == 1
            assert jobs[0]["last_error"] == "lancedb write failed"

    def test_failed_job_retries_until_dead_letter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = IndexJobLog(f"{tmpdir}/jobs.sqlite3", max_attempts=2)

            with pytest.raises(RuntimeError):
                with log.track_file_job("/repo/app.py", file_hash="h1"):
                    raise RuntimeError("first failure")

            first = log.list_jobs()[0]
            assert first["status"] == "failed"
            assert first["attempts"] == 1

            with pytest.raises(RuntimeError):
                with log.track_file_job("/repo/app.py", file_hash="h2") as job_id:
                    assert job_id == first["id"]
                    raise RuntimeError("second failure")

            dead = log.get_job(first["id"])
            assert dead["status"] == "dead_letter"
            assert dead["attempts"] == 2
            assert dead["target_hash"] == "h2"
            assert dead["last_error"] == "second failure"

    def test_new_job_created_after_dead_letter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = IndexJobLog(f"{tmpdir}/jobs.sqlite3", max_attempts=1)

            with pytest.raises(RuntimeError):
                with log.track_file_job("/repo/app.py"):
                    raise RuntimeError("permanent failure")

            dead = log.list_jobs()[0]
            assert dead["status"] == "dead_letter"

            new_job_id = log.start_file_job("/repo/app.py")
            assert new_job_id != dead["id"]
            assert log.get_job(new_job_id)["status"] == "running"

    def test_parallel_file_jobs_do_not_lock_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = IndexJobLog(f"{tmpdir}/jobs.sqlite3")

            def track(idx: int):
                with log.track_file_job(f"/repo/file_{idx}.py", file_hash=str(idx)):
                    return idx

            with ThreadPoolExecutor(max_workers=16) as pool:
                assert sorted(pool.map(track, range(64))) == list(range(64))

            jobs = log.list_jobs("completed")
            assert len(jobs) == 64
