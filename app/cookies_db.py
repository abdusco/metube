from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CookieDB:
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
                    CREATE TABLE IF NOT EXISTS cookies (
                      domain TEXT PRIMARY KEY,
                      content TEXT NOT NULL,
                      updated_at DATETIME NOT NULL
                    )
                    """
                )

    def upsert(self, domain: str, content: str) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO cookies (domain, content, updated_at) VALUES (?, ?, ?)",
                    (domain, content, _now()),
                )

    def delete_domain(self, domain: str) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM cookies WHERE domain = ?", (domain,))

    def delete_all(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM cookies")

    def list_domains(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT domain FROM cookies ORDER BY domain"
            ).fetchall()
        return [row["domain"] for row in rows]

    def get_merged_content(self) -> str:
        with self._lock:
            rows = self._conn.execute(
                "SELECT content FROM cookies ORDER BY domain"
            ).fetchall()
        if not rows:
            return ""
        parts = ["# Netscape HTTP Cookie File"]
        for row in rows:
            parts.append(row["content"])
        return "\n".join(parts) + "\n"
