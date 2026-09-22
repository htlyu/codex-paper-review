"""Small, transactional SQLite queue; connections never cross threads."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


RUNNING_STATES = ("preparing", "reviewing", "validating")
RECOVERY_STATES = RUNNING_STATES + ("cleanup_failed",)
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "interrupted", "cleanup_failed")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class QueueFull(Exception):
    """The configured number of unfinished jobs has been reached."""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    cutoff_date TEXT NOT NULL,
                    focus TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    metadata_json TEXT
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at)"
            )

    @staticmethod
    def _job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["cancel_requested"] = bool(result["cancel_requested"])
        # This metadata stays internal; HTTP responses have a separate allowlist.
        result["metadata"] = json.loads(result.pop("metadata_json") or "null")
        return result

    @staticmethod
    def _unfinished(connection: sqlite3.Connection) -> int:
        return connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','preparing','reviewing','validating')"
        ).fetchone()[0]

    def has_capacity(self, limit: int) -> bool:
        with self._connection() as connection:
            return self._unfinished(connection) < limit

    def enqueue(self, job: dict[str, Any], limit: int) -> dict[str, Any]:
        with self._connection(write=True) as connection:
            if self._unfinished(connection) >= limit:
                raise QueueFull()
            connection.execute(
                """INSERT INTO jobs
                (id, filename, cutoff_date, focus, created_at, updated_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'queued')""",
                (
                    job["id"], job["filename"], job["cutoff_date"], job["focus"],
                    job["created_at"], job["created_at"],
                ),
            )
            result = self._job(connection.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone())
        assert result is not None
        return result

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            return self._job(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def list(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._job(row) for row in rows]  # type: ignore[misc]

    def recovery_job_ids(self) -> list[str]:
        """Only jobs in this store that may still own a container."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status IN ('preparing','reviewing','validating','cleanup_failed')"
            ).fetchall()
            return [row["id"] for row in rows]

    def recover_interrupted(self, job_id: str) -> None:
        """Called only after the host confirms this job's container was stopped."""
        with self._connection(write=True) as connection:
            connection.execute(
                """UPDATE jobs SET status = 'interrupted', error = 'service_interrupted', updated_at = ?
                WHERE id = ? AND status IN ('preparing','reviewing','validating','cleanup_failed')""",
                (utc_now(), job_id),
            )

    def claim_next(self) -> dict[str, Any] | None:
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE jobs SET status = 'preparing', updated_at = ? WHERE id = ?",
                (utc_now(), row["id"]),
            )
            return self._job(connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())

    def update_status(self, job_id: str, status: str) -> None:
        if status not in RUNNING_STATES:
            raise ValueError("Runner supplied an invalid progress state")
        with self._connection(write=True) as connection:
            connection.execute(
                """UPDATE jobs SET status = ?, updated_at = ?
                WHERE id = ? AND status IN ('preparing','reviewing','validating')""",
                (status, utc_now(), job_id),
            )

    def cancellation_requested(self, job_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return row is None or bool(row[0])

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "queued":
                connection.execute(
                    "UPDATE jobs SET status = 'cancelled', cancel_requested = 1, updated_at = ? WHERE id = ?",
                    (utc_now(), job_id),
                )
            elif row["status"] in RUNNING_STATES:
                connection.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                    (utc_now(), job_id),
                )
            return self._job(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def finish(
        self, job_id: str, status: str, *, error: str | None = None, metadata: Any = None
    ) -> None:
        if status not in TERMINAL_STATES:
            raise ValueError("Invalid terminal state")
        encoded_metadata = json.dumps(metadata, ensure_ascii=False, default=str)
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT status, cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["status"] in TERMINAL_STATES:
                return
            # Cancellation and completion race inside this same transaction.
            if row["cancel_requested"] and status not in {"interrupted", "cleanup_failed"}:
                status, error = "cancelled", None
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ?, error = ?, metadata_json = ? WHERE id = ?",
                (status, utc_now(), error, encoded_metadata, job_id),
            )
