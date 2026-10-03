"""Database errors must not carry bound parameter values into logs.

Position inserts bind ``lat``/``lon``, which can locate an identifiable operator, and
SQLAlchemy renders bound parameters into every DBAPI exception message by default. Any
``str(exc)`` or traceback in a log line would then copy coordinates out of the
database, where the retention jobs cannot reach them (CWE-532).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from minemonitor.config import get_settings
from minemonitor.storage import db


@pytest.fixture
def fresh_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MM_DATABASE_URL", f"sqlite:///{tmp_path / 'privacy.db'}")
    get_settings.cache_clear()
    monkeypatch.setattr(db, "_engine", None)
    yield
    if db._engine is not None:
        db._engine.dispose()
    get_settings.cache_clear()


@pytest.mark.usefixtures("fresh_engine")
def test_engine_hides_bound_parameters_in_errors() -> None:
    engine = db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE fix (lat REAL NOT NULL UNIQUE)"))
        conn.execute(text("INSERT INTO fix (lat) VALUES (:lat)"), {"lat": -17.8252})
    with pytest.raises(IntegrityError) as caught, engine.begin() as conn:
        conn.execute(text("INSERT INTO fix (lat) VALUES (:lat)"), {"lat": -17.8252})
    assert "-17.8252" not in str(caught.value)
    assert "hidden due to hide_parameters" in str(caught.value)
