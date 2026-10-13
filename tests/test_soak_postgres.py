"""Soak (plan): 50 assets over 30 days of history, against time and memory budgets.

Skipped unless ``MM_TEST_DATABASE_URL`` points at PostgreSQL. Runs the soak
(:mod:`minemonitor.soak`) twice, each in a database of its own, created for it and migrated
with alembic, so ``positions`` is a hypertable wherever TimescaleDB is installed, as on the
mine's box: 50 assets with 6 hours of history, and the same fleet with 30 days. Then:

- every query of the tick and of ``/state`` touches about as many buffers over 30 days as over
  6 hours. The history is 120 times deeper, so a query that reads all of it grows with it, while
  one that reads a window or probes an index does not (TimescaleDB adds a probe per weekly chunk:
  under 10 times);
- the heap the tick and ``/state`` need does not grow with history either;
- both runs keep within budgets: statements per live fix, and wall-time and heap limits loose
  enough for a CI runner. The wall-time budgets for the mine's box are the soak's own defaults,
  checked by running it there (``python -m minemonitor.soak``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from minemonitor import soak
from minemonitor.config import get_settings

_DB = os.environ.get("MM_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not _DB.startswith("postgresql"),
    reason="set MM_TEST_DATABASE_URL to a PostgreSQL URL to run",
)

# History every 300 s keeps 30 days of it to 432,000 fixes; the ratio between the two runs,
# which is what the comparison rests on, does not depend on it. A tick every 10 s of live
# data gives a timed tick, the explained one and the one measured for heap in 30 s.
_SHORT = soak.SoakConfig(
    site_id="soak-short",
    days=0.25,
    history_interval_s=300,
    live_s=30,
    tick_every_s=10,
    explain=True,
)
_LONG = replace(_SHORT, site_id="soak-long", days=30.0)
# CI runners vary; these catch a tick or /state gone pathological, not a slow machine.
_BUDGETS = soak.Budgets(tick_s=15.0, state_s=3.0, ingest_headroom=0.0, statements_per_fix=9.0)
# A query whose buffers grow past this over 120 times the history reads the history.
_GROWTH, _SLACK_BLOCKS = 10, 100


@contextmanager
def _scratch_database(name: str) -> Iterator[Engine]:
    """A migrated database of its own, dropped afterwards."""
    admin = create_engine(_DB, isolation_level="AUTOCOMMIT", future=True)
    url = make_url(_DB).set(database=f"{make_url(_DB).database}_{name}")
    quoted = admin.dialect.identifier_preparer.quote(url.database or "")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted}"))
            conn.execute(text(f"CREATE DATABASE {quoted}"))
    except Exception as exc:  # noqa: BLE001 - no CREATEDB privilege: nothing to measure on
        admin.dispose()
        pytest.skip(f"cannot create a scratch database: {exc}")
    root = Path(__file__).resolve().parents[1]
    config = Config()  # no ini file: env.py then leaves the test run's logging alone
    config.set_main_option("script_location", str(root / "alembic"))
    rendered = url.render_as_string(hide_password=False)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MM_DATABASE_URL", rendered)
        get_settings.cache_clear()
        command.upgrade(config, "head")
    get_settings.cache_clear()
    engine = create_engine(rendered, pool_pre_ping=True, future=True, hide_parameters=True)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture(scope="module")
def runs() -> Iterator[dict[str, soak.Report]]:
    get_settings.cache_clear()
    with _scratch_database("soak_short") as short, _scratch_database("soak_long") as long_:
        out = {"short": soak.run(short, _SHORT, _BUDGETS)}
        with pytest.raises(soak.RefusedError, match="already holds positions"):
            soak.check_target(short, "another-site")
        out["long"] = soak.run(long_, _LONG, _BUDGETS)
        yield out


def test_the_soak_seeded_a_month_of_history(runs: dict[str, soak.Report]) -> None:
    # 50 assets, one fix each per 300 s, for 30 days.
    assert runs["long"].seeded["positions"] == 50 * 30 * 288
    assert runs["long"].seeded["access_events"] > 20_000
    for r in runs.values():
        assert r.tick["n"] >= 1 and r.state["n"] >= 1
        assert r.ingest["fixes"] == 50 * 30


def test_both_runs_keep_within_budget(runs: dict[str, soak.Report]) -> None:
    assert {name: r.failures for name, r in runs.items()} == {"short": [], "long": []}


@pytest.mark.parametrize("kind", ["tick", "state"])
def test_no_query_reads_more_as_history_grows(runs: dict[str, soak.Report], kind: str) -> None:
    short, long_ = runs["short"].statements[kind], runs["long"].statements[kind]
    assert long_, "nothing was explained"
    grown = [
        f"{short.get(sql, 0)} -> {blocks} blocks: {' '.join(sql.split())[:200]}"
        for sql, blocks in long_.items()
        if blocks > _GROWTH * short.get(sql, 0) + _SLACK_BLOCKS
    ]
    assert grown == [], "buffers over 6 hours of history -> over 30 days"


def test_heap_does_not_grow_with_history(runs: dict[str, soak.Report]) -> None:
    short, long_ = runs["short"].memory, runs["long"].memory
    for key in ("tick_mib", "state_mib"):
        assert long_[key] <= 2 * short[key] + 16, key
