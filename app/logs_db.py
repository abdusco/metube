from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class LogsDB:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
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
                    CREATE TABLE IF NOT EXISTS logs (
                        id      INTEGER PRIMARY KEY,
                        job_id  TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
                        content TEXT NOT NULL
                    )
                    """
                )

    def save(self, job_id: str, lines: list[str]) -> None:
        if not lines:
            return
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO logs (job_id, content) VALUES (?, ?)",
                    (job_id, "\n".join(lines)),
                )

    def get_lines(self, job_id: str) -> list[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT content FROM logs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return []
        return row["content"].split("\n")
