"""Crash-safe store-and-forward outbox for the vision edge node (SQLite, stdlib only).

The mine link and the mine's power both fail (``CLAUDE.md`` §3). Everything the edge concludes is
written here **before** it is sent, in a WAL-mode SQLite file with ``synchronous=FULL``, so an
unclean shutdown loses nothing and a link outage only delays delivery:

- ``outbox`` — records to deliver (operational events, rollups, health; observations only in lab
  mode). Keyed by the record's deterministic id, so re-enqueueing after a crash is a no-op, and the
  core dedupes on the same id — delivery is effectively exactly-once.
- ``observations`` — the edge's own record of what it saw (build-gate §5: *store all observations on
  the edge*), kept for a bounded window so the core can pull a slice on demand for audit/recompute.

A record the core rejects as malformed (HTTP 422) is moved to ``dead`` with the reason — it will
never become valid, and it must not block everything behind it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_ID_FIELDS = ("event_id", "rollup_id", "health_id", "observation_id")


def record_id(record: BaseModel) -> str:
    for f in _ID_FIELDS:
        v = getattr(record, f, None)
        if isinstance(v, str):
            return v
    raise ValueError(f"{type(record).__name__} has no deterministic id")


class Outbox:
    """A durable queue of ``vision.*.v1`` records awaiting delivery to the core."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY,
                schema TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_outbox_state ON outbox(state, created_at);
            CREATE TABLE IF NOT EXISTS observations (
                observation_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_observations_ts ON observations(ts);
            """
        )

    def close(self) -> None:
        self._db.close()

    # -- outbox ------------------------------------------------------------------------------

    def put(self, record: BaseModel) -> bool:
        """Enqueue a record; ``False`` if its id is already queued/sent (idempotent)."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO outbox (id, schema, payload, created_at) VALUES (?, ?, ?, ?)",
            (
                record_id(record),
                getattr(record, "schema_", type(record).__name__),
                record.model_dump_json(by_alias=True),
                datetime.now(UTC).isoformat(),
            ),
        )
        return cur.rowcount == 1

    def pending(self, limit: int = 100) -> list[tuple[str, str]]:
        """Oldest pending ``(id, payload_json)`` first."""
        rows = self._db.execute(
            "SELECT id, payload FROM outbox WHERE state='pending' ORDER BY created_at, id LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def mark_sent(self, ids: list[str]) -> None:
        self._db.executemany(
            "UPDATE outbox SET state='sent', attempts=attempts+1 WHERE id=?", [(i,) for i in ids]
        )

    def mark_attempt(self, ids: list[str], error: str) -> None:
        self._db.executemany(
            "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?",
            [(error, i) for i in ids],
        )

    def mark_dead(self, id_: str, error: str) -> None:
        self._db.execute(
            "UPDATE outbox SET state='dead', attempts=attempts+1, last_error=? WHERE id=?",
            (error, id_),
        )

    def counts(self) -> dict[str, int]:
        rows = self._db.execute("SELECT state, COUNT(*) FROM outbox GROUP BY state").fetchall()
        out = {"pending": 0, "sent": 0, "dead": 0}
        out.update({r[0]: r[1] for r in rows})
        return out

    def prune_sent(self, before: datetime) -> int:
        cur = self._db.execute(
            "DELETE FROM outbox WHERE state='sent' AND created_at < ?", (before.isoformat(),)
        )
        return cur.rowcount

    # -- local observation record ------------------------------------------------------------

    def store_observation(self, obs: BaseModel) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO observations (observation_id, ts, payload) VALUES (?, ?, ?)",
            (
                record_id(obs),
                getattr(obs, "ts").astimezone(UTC).isoformat(),  # noqa: B009 - BaseModel
                obs.model_dump_json(by_alias=True),
            ),
        )

    def observations_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """A slice of local observations — what the core pulls for audit/recompute."""
        import json

        rows = self._db.execute(
            "SELECT payload FROM observations WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def prune_observations(self, before: datetime) -> int:
        cur = self._db.execute(
            "DELETE FROM observations WHERE ts < ?", (before.astimezone(UTC).isoformat(),)
        )
        return cur.rowcount
