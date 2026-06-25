from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any
from pathlib import Path

from job_models import Job, JobCreate, JobStatus, SubtitleFile


def _now() -> datetime:
    return datetime.now(UTC)


class JobDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                      id TEXT PRIMARY KEY,
                      url TEXT NOT NULL,
                      title TEXT NOT NULL,
                      download_type TEXT NOT NULL,
                      codec TEXT NOT NULL,
                      format TEXT NOT NULL,
                      quality TEXT NOT NULL,
                      subtitle_langs_json JSONB NOT NULL DEFAULT (jsonb('[]')),
                      status TEXT NOT NULL DEFAULT 'queued',
                      message TEXT,
                      percent REAL,
                      speed REAL,
                      eta REAL,
                      filename TEXT,
                      size INTEGER,
                      error TEXT,
                      subtitle_files_json JSONB NOT NULL DEFAULT (jsonb('[]')),
                      cancel_requested_at DATETIME,
                      created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                      updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                      started_at DATETIME,
                      finished_at DATETIME
                    )
                    """
                )
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at)")
                try:
                    self._conn.execute("ALTER TABLE jobs ADD COLUMN temp_files_json JSONB NOT NULL DEFAULT (jsonb('[]'))")
                except sqlite3.OperationalError:
                    pass  # column already exists

    _SELECT_COLUMNS = """
        id,
        url,
        title,
        download_type,
        codec,
        format,
        quality,
        json(subtitle_langs_json) AS subtitle_langs_json,
        status,
        message,
        percent,
        speed,
        eta,
        filename,
        size,
        error,
        json(subtitle_files_json) AS subtitle_files_json,
        json(temp_files_json) AS temp_files_json,
        cancel_requested_at,
        created_at,
        updated_at,
        started_at,
        finished_at
    """

    @staticmethod
    def _encode_jsonb(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @classmethod
    def _row_to_job(cls, row: sqlite3.Row) -> Job:
        data = dict(row)
        data["subtitle_langs"] = json.loads(data.pop("subtitle_langs_json") or "[]")
        data["subtitle_files"] = json.loads(data.pop("subtitle_files_json") or "[]")
        data["temp_files"] = json.loads(data.pop("temp_files_json") or "[]")
        return Job.model_validate(data)

    def create_job(self, job_id: str, spec: JobCreate, title: str) -> Job:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO jobs (
                      id, url, title, download_type, codec, format, quality,
                      subtitle_langs_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, jsonb(?))
                    """,
                    (
                        job_id,
                        spec.url,
                        title,
                        spec.download_type,
                        spec.codec,
                        spec.format,
                        spec.quality,
                        self._encode_jsonb(spec.subtitle_langs),
                    ),
                )
            return self.get_job(job_id)

    def get_job(self, job_id: str) -> Job:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_job(row)

    def list_queued(self) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM jobs WHERE status IN (?, ?) ORDER BY created_at ASC",
                (JobStatus.QUEUED, JobStatus.RUNNING),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_done(self) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM jobs WHERE status IN (?, ?, ?) ORDER BY created_at ASC",
                (JobStatus.FINISHED, JobStatus.ERROR, JobStatus.CANCELED),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def claim_next_queued(self) -> Job | None:
        with self._lock:
            with self._conn:
                row = self._conn.execute(
                    "SELECT id FROM jobs WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                    (JobStatus.QUEUED,),
                ).fetchone()
                if row is None:
                    return None
                now = _now().isoformat()
                result = self._conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, started_at = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (JobStatus.RUNNING, now, now, row["id"], JobStatus.QUEUED),
                )
                if result.rowcount != 1:
                    return None
            return self.get_job(row["id"])

    def update_progress(
        self,
        job_id: str,
        *,
        message: str | None = None,
        percent: float | None = None,
        speed: float | None = None,
        eta: float | None = None,
    ) -> None:
        now = _now().isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE jobs
                    SET message = ?, percent = ?, speed = ?, eta = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (message, percent, speed, eta, now, job_id),
                )

    def set_output_file(self, job_id: str, filename: str | None, size: int | None) -> None:
        now = _now().isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE jobs SET filename = ?, size = ?, updated_at = ? WHERE id = ?",
                    (filename, size, now, job_id),
                )

    def add_subtitle_file(self, job_id: str, filename: str, size: int | None) -> None:
        job = self.get_job(job_id)
        subtitle_files = list(job.subtitle_files)
        if not any(sf.filename == filename for sf in subtitle_files):
            subtitle_files.append(SubtitleFile(filename=filename, size=size))
        now = _now().isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE jobs SET subtitle_files_json = jsonb(?), updated_at = ? WHERE id = ?",
                    (self._encode_jsonb([sf.model_dump(exclude={"download_url"}) for sf in subtitle_files]), now, job_id),
                )

    def mark_finished(self, job_id: str) -> None:
        now = _now().isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, updated_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (JobStatus.FINISHED, now, now, job_id),
                )

    def mark_error(self, job_id: str, error: str | None) -> None:
        now = _now().isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error = ?, message = ?, updated_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (JobStatus.ERROR, error, error, now, now, job_id),
                )

    def mark_canceled(self, job_id: str) -> None:
        now = _now().isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, message = ?, updated_at = ?, finished_at = ?, cancel_requested_at = ?
                    WHERE id = ?
                    """,
                    (JobStatus.CANCELED, "Canceled", now, now, now, job_id),
                )

    def request_cancel(self, job_id: str) -> JobStatus | None:
        now = _now().isoformat()
        with self._lock:
            row = self._conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            status = JobStatus(row["status"])
            with self._conn:
                if status == JobStatus.QUEUED:
                    self._conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?, message = ?, cancel_requested_at = ?, updated_at = ?, finished_at = ?
                        WHERE id = ?
                        """,
                        (JobStatus.CANCELED, "Canceled", now, now, now, job_id),
                    )
                    return JobStatus.CANCELED
                if status == JobStatus.RUNNING:
                    self._conn.execute(
                        "UPDATE jobs SET cancel_requested_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, job_id),
                    )
                    return JobStatus.RUNNING
        return status

    def delete_done(self, ids: list[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            with self._conn:
                self._conn.execute(
                    f"DELETE FROM jobs WHERE id IN ({placeholders}) AND status IN (?, ?, ?)",
                    (*ids, JobStatus.FINISHED, JobStatus.ERROR, JobStatus.CANCELED),
                )

    def list_done_older_than(self, threshold: datetime) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM jobs WHERE status IN (?, ?, ?) AND finished_at < ? ORDER BY created_at ASC",
                (JobStatus.FINISHED, JobStatus.ERROR, JobStatus.CANCELED, threshold.isoformat()),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_done_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM jobs WHERE status IN (?, ?, ?)",
                (JobStatus.FINISHED, JobStatus.ERROR, JobStatus.CANCELED),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def list_running(self) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM jobs WHERE status = ?",
                (JobStatus.RUNNING,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def add_temp_file(self, job_id: str, path: str) -> None:
        job = self.get_job(job_id)
        files = list(job.temp_files)
        if path not in files:
            files.append(path)
        now = _now().isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE jobs SET temp_files_json = jsonb(?), updated_at = ? WHERE id = ?",
                    (self._encode_jsonb(files), now, job_id),
                )

    def remove_temp_file(self, job_id: str, path: str) -> None:
        job = self.get_job(job_id)
        files = [f for f in job.temp_files if f != path]
        now = _now().isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE jobs SET temp_files_json = jsonb(?), updated_at = ? WHERE id = ?",
                    (self._encode_jsonb(files), now, job_id),
                )

    def clear_temp_files(self, job_id: str) -> None:
        now = _now().isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE jobs SET temp_files_json = jsonb(?), updated_at = ? WHERE id = ?",
                    (self._encode_jsonb([]), now, job_id),
                )

    def reset_running_jobs_to_error(self) -> None:
        now = _now().isoformat()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error = ?, message = ?, updated_at = ?, finished_at = ?
                    WHERE status = ?
                    """,
                    (JobStatus.ERROR, "Interrupted by restart", "Interrupted by restart", now, now, JobStatus.RUNNING),
                )
