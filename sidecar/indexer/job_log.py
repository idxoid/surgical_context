"""Durable indexing job log for retry and dead-letter tracking."""

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

DEFAULT_JOB_LOG_PATH = os.getenv("INDEX_JOB_LOG_PATH", "./data/index_jobs.sqlite3")
TERMINAL_STATUSES = {"completed", "dead_letter"}


class IndexJobLog:
    """Small SQLite-backed job log for indexing recovery."""

    def __init__(self, db_path: str = DEFAULT_JOB_LOG_PATH, max_attempts: int = 3):
        self.db_path = db_path
        self.max_attempts = max_attempts
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._ensure_schema()

    def start_file_job(self, file_path: str, file_hash: str = "") -> int:
        """Create or retry a file indexing job and return its id."""
        now = int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, attempts
                FROM index_jobs
                WHERE job_type = 'index_file'
                  AND target = ?
                  AND status = 'failed'
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (file_path,),
            ).fetchone()
            if row:
                attempts = int(row["attempts"]) + 1
                conn.execute(
                    """
                    UPDATE index_jobs
                    SET status = 'running',
                        attempts = ?,
                        target_hash = ?,
                        last_error = '',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (attempts, file_hash, now, row["id"]),
                )
                return int(row["id"])

            cursor = conn.execute(
                """
                INSERT INTO index_jobs (
                    job_type, target, target_hash, status, attempts, last_error, created_at, updated_at
                )
                VALUES ('index_file', ?, ?, 'running', 1, '', ?, ?)
                """,
                (file_path, file_hash, now, now),
            )
            return int(cursor.lastrowid)

    def mark_completed(self, job_id: int):
        """Mark a job as completed."""
        self._update_status(job_id, "completed", "")

    def mark_failed(self, job_id: int, error: Exception | str):
        """Mark a job as failed or dead-lettered after too many attempts."""
        job = self.get_job(job_id)
        if not job:
            return
        status = "dead_letter" if int(job["attempts"]) >= self.max_attempts else "failed"
        self._update_status(job_id, status, str(error))

    def get_job(self, job_id: int) -> dict | None:
        """Return one job row as a dict."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM index_jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self, status: str | None = None) -> list[dict]:
        """List jobs, optionally filtered by status."""
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM index_jobs WHERE status = ? ORDER BY updated_at DESC, id DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM index_jobs ORDER BY updated_at DESC, id DESC"
                ).fetchall()
            return [dict(row) for row in rows]

    @contextmanager
    def track_file_job(self, file_path: str, file_hash: str = "") -> Iterator[int]:
        """Track one file indexing attempt through completion or failure."""
        job_id = self.start_file_job(file_path, file_hash=file_hash)
        try:
            yield job_id
        except Exception as exc:
            self.mark_failed(job_id, exc)
            raise
        else:
            self.mark_completed(job_id)

    def _update_status(self, job_id: int, status: str, error: str):
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE index_jobs
                SET status = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, error, now, job_id),
            )

    def _ensure_schema(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS index_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL,
                    target TEXT NOT NULL,
                    target_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_index_jobs_target_status
                ON index_jobs(job_type, target, status, updated_at)
                """
            )

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
