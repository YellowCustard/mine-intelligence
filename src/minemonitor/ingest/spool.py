"""A crash-safe local spool for store-and-forward.

The network and power will both fail (brief §3). The publisher writes every
position to this on-disk spool first, then forwards from it and deletes only once
the broker has acknowledged delivery. A SQLite file in FULL-synchronous mode
survives an unclean shutdown: nothing acknowledged is lost, nothing is silently
dropped.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class Spool:
    """A durable FIFO queue of serialised payloads, keyed by monotonic id."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        # check_same_thread=False: the drain loop and producer may differ; we
        # serialise access ourselves and each op is a short committed transaction.
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS spool ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
        )

    def append(self, payload: str) -> int:
        """Durably append a payload. Returns its spool id."""
        cur = self._conn.execute("INSERT INTO spool (payload) VALUES (?)", (payload,))
        return int(cur.lastrowid)

    def peek(self, limit: int = 100) -> list[tuple[int, str]]:
        """Return up to ``limit`` oldest entries as (id, payload), FIFO order."""
        rows = self._conn.execute(
            "SELECT id, payload FROM spool ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [(int(r[0]), str(r[1])) for r in rows]

    def delete(self, ids: list[int]) -> None:
        """Delete entries by id (call only after confirmed delivery)."""
        if not ids:
            return
        self._conn.executemany("DELETE FROM spool WHERE id = ?", [(i,) for i in ids])

    def __len__(self) -> int:
        row = self._conn.execute("SELECT count(*) FROM spool").fetchone()
        return int(row[0])

    def close(self) -> None:
        self._conn.close()
