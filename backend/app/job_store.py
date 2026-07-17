from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JobNotFoundError(KeyError):
    pass


class JobStore:
    """Small durable job store used by the local production service.

    SQLite keeps queued/running/completed jobs inspectable after a service restart.
    A single process lock protects multi-threaded access; SQLite still provides the
    transactional boundary.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nesting_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    result_json TEXT,
                    error_message TEXT
                )
                """
            )
            # A process restart cannot resume native solver state safely.  Mark
            # stale work explicitly instead of leaving operators with jobs that
            # appear to run forever.
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                UPDATE nesting_jobs
                SET status = 'interrupted', updated_at = ?,
                    error_message = '服务重启，任务未完成，请重新提交'
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        job_id = str(uuid.uuid4())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO nesting_jobs
                    (job_id, status, created_at, updated_at, input_json)
                VALUES (?, 'queued', ?, ?, ?)
                """,
                (job_id, now, now, json.dumps(payload, ensure_ascii=False)),
            )
        return self.get(job_id, include_payload=False)

    def update(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE nesting_jobs
                SET status = ?, updated_at = ?, result_json = ?, error_message = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    now,
                    None if result is None else json.dumps(result, ensure_ascii=False),
                    error_message,
                    job_id,
                ),
            )
            if cursor.rowcount == 0:
                raise JobNotFoundError(job_id)

    def get(self, job_id: str, *, include_payload: bool = False) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM nesting_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        record: dict[str, Any] = {
            "job_id": row["job_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error_message": row["error_message"],
        }
        if row["result_json"]:
            record["result"] = json.loads(row["result_json"])
        if include_payload:
            record["input"] = json.loads(row["input_json"])
        return record

    def input_for(self, job_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM nesting_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return json.loads(row["input_json"])

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 200)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id, status, created_at, updated_at, error_message
                FROM nesting_jobs ORDER BY created_at DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]
