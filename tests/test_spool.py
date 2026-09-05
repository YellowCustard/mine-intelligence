"""Unit tests for the crash-safe store-and-forward spool."""

from __future__ import annotations

from pathlib import Path

from minemonitor.ingest.spool import Spool


def test_append_peek_delete_fifo(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "s.sqlite")
    for i in range(5):
        spool.append(f"p{i}")
    assert len(spool) == 5

    peek = spool.peek(3)
    assert [p for _, p in peek] == ["p0", "p1", "p2"]  # FIFO

    spool.delete([i for i, _ in peek])
    assert len(spool) == 2
    assert [p for _, p in spool.peek()] == ["p3", "p4"]
    spool.close()


def test_survives_reopen(tmp_path: Path) -> None:
    """A durable spool: entries persist across process restart (crash-safety)."""
    path = tmp_path / "s.sqlite"
    spool = Spool(path)
    spool.append("keep-me")
    spool.close()

    reopened = Spool(path)
    assert len(reopened) == 1
    assert reopened.peek()[0][1] == "keep-me"
    reopened.close()


def test_delete_empty_is_noop(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "s.sqlite")
    spool.delete([])  # must not raise
    assert len(spool) == 0
    spool.close()
