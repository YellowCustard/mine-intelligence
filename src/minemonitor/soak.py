"""Soak: a full fleet over a month of history, measured against time and memory budgets.

Seeds a site with ``assets`` machines and ``days`` of history, then drives live fixes
through the real ingest path (``store_and_process``, as the MQTT ingestor does) and runs
the real maintenance tick every ``tick_every_s`` of data, as the ingestor does. It
measures:

- ingest: live fixes stored and processed per second, against the fleet's own 1 Hz rate,
  and the SQL statements each fix costs;
- tick: wall time of the site's steps in each tick, and of each step;
- ``/state``: wall time to build and encode the dashboard snapshot;
- memory: the Python heap peak (tracemalloc) of one tick and of one ``/state``;
- with ``explain``, the shared buffers each query of one tick and one ``/state`` touches.
  Unlike wall time this does not depend on the machine, and it is proportional to what a
  query reads, so a query that reads the whole history stands out from one that reads a
  window: run the soak over a short and a long history and compare.

History is the simulator's fleet at ``history_interval_s`` (copied in with COPY), plus
haul cycles, acknowledged alarms and gate passages at a plausible rate, so the tables the
tick and ``/state`` read are as deep as ``days`` makes them. Live data is the same fleet
at 1 Hz, never ahead of the wall clock, so offline detection sees it as current.

It writes synthetic data, so it runs only against a scratch database: migrated
PostgreSQL holding no telemetry and no account::

    createdb mm_soak
    MM_DATABASE_URL=postgresql+psycopg://.../mm_soak alembic upgrade head
    MM_DATABASE_URL=postgresql+psycopg://.../mm_soak python -m minemonitor.soak --report soak.json

Exit status 1 when a budget is exceeded, 2 when it refuses the database.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
import time
import tracemalloc
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, TypeVar

from psycopg import sql
from sqlalchemy import event, literal, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.api.routers.stream import state as state_snapshot
from minemonitor.config import Settings, get_settings
from minemonitor.ingest import tick
from minemonitor.ingest.adapters.simulator import FACE, MAGAZINE, ROM, WASTE, WORKSHOP, Simulator
from minemonitor.ingest.service import PositionIngest, reject_if_future, store_and_process
from minemonitor.notifications.dispatch import WhatsAppTemplate
from minemonitor.storage.models import (
    Asset,
    Position,
    RecomputeCursor,
    ShiftDefinition,
    Site,
    User,
)
from minemonitor.zones.geometry import box_polygon
from minemonitor.zones.repository import upsert_zone

log = logging.getLogger("minemonitor.soak")

T = TypeVar("T")

_MIB = 2**20


class RefusedError(RuntimeError):
    """The target database is not one the soak may write synthetic data into."""


@dataclass(frozen=True)
class SoakConfig:
    site_id: str = "soak-01"
    days: float = 30.0  # history seeded before the live phase
    assets: int = 50
    history_interval_s: float = 10.0  # one fix per asset this often in the seeded history
    live_s: float = 600.0  # live data at 1 Hz through the ingest path
    tick_every_s: float = 60.0  # the ingestor's maintenance interval
    events_per_day: int = 240  # alarms in the seeded history, all acknowledged
    passages_per_day: int = 800  # gate passages in the seeded history
    explain: bool = False
    seed: int = 1

    def __post_init__(self) -> None:
        if min(self.days, self.assets, self.history_interval_s, self.tick_every_s) <= 0:
            raise ValueError("days, assets, history_interval_s and tick_every_s must be > 0")
        # The last live tick measures memory, and the one before it (with explain) buffers;
        # every other one is timed, and at least one must be.
        if self.live_s < self.tick_every_s * (2 + int(self.explain)):
            raise ValueError("live_s is too short for a timed tick plus the measured ones")


@dataclass(frozen=True)
class Budgets:
    tick_s: float = 6.0  # p95 of the site's tick: a tenth of the ingestor's 60 s interval
    state_s: float = 0.5  # p95 of /state, which every open dashboard also streams
    ingest_headroom: float = 2.0  # ingest capacity over the live rate: a backfill drains
    # at least as fast as it built up
    statements_per_fix: float = 9.0  # SQL round trips per live fix
    tick_mib: float = 256.0  # Python heap peak of the site's tick
    state_mib: float = 64.0  # Python heap peak of one /state


@dataclass
class Report:
    config: dict[str, Any]
    budgets: dict[str, float]
    seeded: dict[str, int] = field(default_factory=dict)  # table -> rows
    seed_s: float = 0.0
    first_tick_s: float = 0.0  # the tick right after seeding, which starts every cursor
    ingest: dict[str, float] = field(default_factory=dict)
    tick: dict[str, Any] = field(default_factory=dict)
    state: dict[str, float] = field(default_factory=dict)
    memory: dict[str, float] = field(default_factory=dict)
    statements: dict[str, dict[str, int]] = field(default_factory=dict)  # kind -> sql -> blocks
    failures: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        # The ten heaviest queries of each kind are what a reader acts on.
        out["statements"] = {
            kind: [
                {"blocks": blocks, "sql": " ".join(stmt.split())}
                for stmt, blocks in sorted(stmts.items(), key=lambda kv: -kv[1])[:10]
            ]
            for kind, stmts in self.statements.items()
        }
        return out


# --- the fleet --------------------------------------------------------------------------


class Fleet:
    """``n`` simulated machines: copies of the simulator's fleet, renamed apart.

    Copy ``k`` of an asset is ``<asset_id>-<k>``. Each copy has its own loader, so queues
    form as they do in one simulator; the copies share its placeholder geography.
    """

    def __init__(self, site_id: str, n: int, *, seed: int, start: datetime, tick_s: float):
        per = len(Simulator(site_id).assets)
        self._sims = [
            Simulator(site_id, seed=seed + k, start=start, tick_s=tick_s)
            for k in range(math.ceil(n / per))
        ]
        self.classes: dict[str, str] = {}
        for k, sim in enumerate(self._sims):
            for a in sim.assets.values():
                if len(self.classes) < n:
                    self.classes[f"{a.asset_id}-{k}"] = a.asset_class

    @property
    def now(self) -> datetime:
        return self._sims[0].now

    def set_tick(self, tick_s: float) -> None:
        for sim in self._sims:
            sim.tick_s = tick_s

    def step(self) -> list[PositionIngest]:
        out = []
        for k, sim in enumerate(self._sims):
            for p in sim.step():
                asset_id = f"{p.asset_id}-{k}"
                if asset_id in self.classes:
                    out.append(p.model_copy(update={"asset_id": asset_id}))
        return out


def _zones() -> list[tuple[str, str, str, dict[str, Any], dict[str, Any]]]:
    """(zone_id, name, kind, geometry, rules): one zone of each kind around the simulator's
    named places. Placeholders, like the simulator's coordinates (brief §14)."""
    road = ((FACE[0] + ROM[0]) / 2, (FACE[1] + ROM[1]) / 2)
    magazine_rules = {"authorized_classes": ["explosives_truck"]}
    road_rules = {"speed_limit_kph": 30}
    return [
        ("pit-face", "Pit face", "loading", box_polygon(*FACE, 70), {}),
        ("rom-pad", "ROM pad", "unloading", box_polygon(*ROM, 70), {}),
        ("waste-dump", "Waste dump", "unloading", box_polygon(*WASTE, 80), {}),
        ("r1-magazine", "R1 Magazine", "restricted", box_polygon(*MAGAZINE, 60), magazine_rules),
        ("haul-road", "Haul road", "speed_limited", box_polygon(*road, 350), road_rules),
        ("workshop", "Workshop", "generic", box_polygon(*WORKSHOP, 80), {"max_occupancy": 4}),
    ]


# --- seeding ----------------------------------------------------------------------------


def _columns(names: str) -> tuple[str, ...]:
    return tuple(names.split())


_POSITION_COLUMNS = _columns(
    "site_id asset_id ts received_at lat lon altitude_m speed_kph heading_deg hdop satellites "
    "ignition source"
)
_CYCLE_COLUMNS = _columns(
    "site_id asset_id start_ts end_ts cycle_time_s queue_s load_s haul_s dump_s return_s"
)
_EVENT_COLUMNS = _columns(
    "event_id site_id ts type severity asset_id source summary advisory state acknowledged_by "
    "acknowledged_at"
)
_PASSAGE_COLUMNS = _columns(
    "id site_id ts source_system gate_id credential_ref decision search_selected "
    "search_completed created_at"
)
_HISTORY_EVENTS = (
    ("overspeed", "warning", "gnss_geofence"),
    ("zone_dwell", "warning", "gnss_geofence"),
    ("asset_offline", "warning", "gnss_offline"),
    ("zone_breach", "critical", "gnss_geofence"),
)


def _copy(engine: Engine, table: str, columns: tuple[str, ...], rows: Iterable[tuple]) -> int:
    """COPY rows into a table; the ORM is far too slow for a month of fixes."""
    stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns))
    )
    raw = engine.raw_connection()
    n = 0
    try:
        conn: Any = raw.driver_connection
        with conn.cursor() as cur, cur.copy(stmt) as copy:
            for row in rows:
                copy.write_row(row)
                n += 1
        conn.commit()
    finally:
        raw.close()
    return n


def _history_positions(fleet: Fleet, until: datetime) -> Iterator[tuple]:
    while fleet.now < until:
        for p in fleet.step():
            yield (
                *(p.site_id, p.asset_id, p.ts, p.ts, p.lat, p.lon, p.altitude_m, p.speed_kph),
                *(p.heading_deg, p.hdop, p.satellites, p.ignition, p.source),
            )


def _history_cycles(
    site_id: str, trucks: list[str], start: datetime, until: datetime, rng: random.Random
) -> list[tuple]:
    rows = []
    for asset_id in trucks:
        t = start + timedelta(seconds=rng.uniform(0, 900))
        while True:
            seg = [rng.uniform(30, 240), rng.uniform(80, 110), rng.uniform(250, 350)]
            seg += [rng.uniform(35, 50), rng.uniform(230, 320)]
            end = t + timedelta(seconds=sum(seg))
            if end > until:
                break
            rows.append((site_id, asset_id, t, end, sum(seg), *seg))
            t = end
    return sorted(rows, key=lambda r: r[3])  # stored as they close, as the tick writes them


def _history_events(
    site_id: str,
    assets: list[str],
    start: datetime,
    until: datetime,
    per_day: int,
    rng: random.Random,
) -> list[tuple]:
    span = until - start
    n = int(per_day * span.total_seconds() / 86400)
    times = sorted(start + span * rng.random() for _ in range(n))
    rows = []
    for i, ts in enumerate(times):
        kind, severity, source = _HISTORY_EVENTS[i % len(_HISTORY_EVENTS)]
        asset_id = rng.choice(assets)
        summary = f"{asset_id} {kind} (synthetic history)"
        ack = ts + timedelta(seconds=60)
        rows.append(
            (f"soak-{i}", site_id, ts, kind, severity, asset_id, source, summary, True)
            + ("acknowledged", "soak", ack)
        )
    return rows


def _history_passages(
    site_id: str,
    start: datetime,
    until: datetime,
    per_day: int,
    grace_s: int,
    rng: random.Random,
) -> tuple[list[tuple], list[tuple]]:
    """Gate passages, and the ``search_missed`` alarm already raised for each selected
    search never completed, so the first tick finds the history already escalated."""
    passages, missed = [], []
    t, i = start, 0
    while t < until:
        selected = rng.random() < 0.1
        completed = (True if rng.random() < 0.95 else None) if selected else None
        pid = f"soak-gate-{i}"
        decision = "granted" if rng.random() < 0.98 else "denied"
        card = f"card-{rng.randrange(400)}"
        passages.append((pid, site_id, t, "soak", "gate-1", card, decision, selected, completed, t))
        due = t + timedelta(seconds=grace_s)
        if selected and completed is None and due < until:
            summary = "Selected search not completed at gate-1"
            missed.append(
                (f"search-missed-{pid}", site_id, due, "search_missed", "warning", None)
                + ("access:gate-1", summary, True, "acknowledged", "soak", due)
            )
        t += timedelta(seconds=rng.expovariate(per_day / 86400))
        i += 1
    return passages, missed


def _seed(
    engine: Engine,
    factory: sessionmaker[Session],
    cfg: SoakConfig,
    live_start: datetime,
    settings: Settings,
) -> tuple[Fleet, dict[str, int]]:
    rng = random.Random(cfg.seed)
    # Whole history steps, so the live phase starts exactly at live_start.
    steps = int(cfg.days * 86400 // cfg.history_interval_s)
    start = live_start - timedelta(seconds=steps * cfg.history_interval_s)
    fleet = Fleet(
        cfg.site_id, cfg.assets, seed=cfg.seed * 1000, start=start, tick_s=cfg.history_interval_s
    )
    with factory() as s:
        s.add(Site(site_id=cfg.site_id, name=f"Soak {cfg.site_id}", timezone="Africa/Harare"))
        s.flush()  # no ORM relationships order the inserts: the site goes first
        s.add_all(
            ShiftDefinition(site_id=cfg.site_id, name=name, start_hour_local=h, duration_hours=12)
            for name, h in (("day", 6), ("night", 18))
        )
        s.add_all(
            Asset(site_id=cfg.site_id, asset_id=a, asset_class=c) for a, c in fleet.classes.items()
        )
        for zone_id, name, kind, geometry, rules in _zones():
            upsert_zone(
                s,
                site_id=cfg.site_id,
                zone_id=zone_id,
                name=name,
                kind=kind,
                geometry=geometry,
                rules=rules,
            )
        # No cursor state: the first tick starts each asset a bounded window before its
        # newest fix, as it does for history from before cursors existed.
        s.add_all(
            RecomputeCursor(site_id=cfg.site_id, asset_id=a, entry_known=False)
            for a in fleet.classes
        )
        s.commit()

    seeded = {
        "positions": _copy(
            engine, "positions", _POSITION_COLUMNS, _history_positions(fleet, live_start)
        )
    }
    trucks = [a for a, c in fleet.classes.items() if c == "haul_truck"]
    cycles = _history_cycles(cfg.site_id, trucks, start, live_start, rng)
    seeded["haul_cycles"] = _copy(engine, "haul_cycles", _CYCLE_COLUMNS, cycles)
    grace_s = settings.access_search_grace_s
    passages, missed = _history_passages(
        cfg.site_id, start, live_start, cfg.passages_per_day, grace_s, rng
    )
    seeded["access_events"] = _copy(engine, "access_events", _PASSAGE_COLUMNS, passages)
    alarms = _history_events(
        cfg.site_id, list(fleet.classes), start, live_start, cfg.events_per_day, rng
    )
    alarms = sorted([*alarms, *missed], key=lambda r: r[2])
    seeded["events"] = _copy(engine, "events", _EVENT_COLUMNS, alarms)
    fleet.set_tick(1.0)
    return fleet, seeded


def _vacuum_analyze(engine: Engine) -> None:
    """What autovacuum would long since have done to a month-old site: fresh planner
    statistics, and a visibility map that lets index-only scans skip the heap."""
    raw = engine.raw_connection()
    conn: Any = raw.driver_connection
    try:
        conn.autocommit = True
        for table in ("positions", "haul_cycles", "access_events", "events"):
            conn.execute(sql.SQL("VACUUM (ANALYZE) {}").format(sql.Identifier(table)))
    finally:
        conn.autocommit = False
        raw.close()


def check_target(engine: Engine, site_id: str) -> None:
    """Refuse anything but a scratch database: migrated PostgreSQL with no telemetry, no
    account and no soak site. A fresh ``alembic upgrade head`` passes (its placeholder
    site has neither telemetry nor accounts); a deployed site's database never does."""
    if engine.dialect.name != "postgresql":
        raise RefusedError("the soak measures PostgreSQL; point MM_DATABASE_URL at one")
    with engine.connect() as conn:
        if conn.execute(text("SELECT to_regclass('alembic_version')")).scalar() is None:
            raise RefusedError("the database is not migrated: run `alembic upgrade head` first")
        used = [
            name
            for name, model in (("positions", Position), ("users", User))
            if conn.execute(select(literal(1)).select_from(model).limit(1)).first()
        ]
        ran = conn.execute(select(Site.site_id).where(Site.site_id == site_id)).first()
    if used:
        raise RefusedError(
            f"the database already holds {' and '.join(used)}; the soak writes synthetic "
            "data, so give it a scratch database"
        )
    if ran:
        raise RefusedError(f"a soak has already run here ({site_id}): use a fresh database")


# --- measuring --------------------------------------------------------------------------


class _NoSend:
    """The soak configures no notifications; anything due fails loudly, nothing is sent."""

    def webhook(self, url: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("the soak sends no notifications")

    def email(self, to: str, subject: str, body: str) -> None:
        raise RuntimeError("the soak sends no notifications")

    def whatsapp(self, to: str, text: str, template: WhatsAppTemplate | None) -> None:
        raise RuntimeError("the soak sends no notifications")


def _tick(factory: sessionmaker[Session], settings: Settings, site_id: str) -> list[tick.Outcome]:
    """The ingestor's tick, limited to this site's steps (and the platform-wide ones)."""
    with factory() as s:
        steps = tick.plan(
            s, settings, gate_source=None, notify_sender=_NoSend(), retention_due=False
        )
        return tick.run(s, [st for st in steps if st.site_id in (None, site_id)])


def _state(factory: sessionmaker[Session], site_id: str) -> float:
    with factory() as s:
        t0 = time.perf_counter()
        json.dumps(state_snapshot(site_id, s), default=str)
        return time.perf_counter() - t0


def _peak_mib(fn: Callable[[], T]) -> tuple[T, float]:
    """``fn()`` and the Python heap peak it reached, in MiB."""
    already = tracemalloc.is_tracing()
    if not already:
        tracemalloc.start()
    tracemalloc.reset_peak()
    base, _ = tracemalloc.get_traced_memory()
    try:
        out = fn()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not already:
            tracemalloc.stop()
    return out, max(peak - base, 0) / _MIB


class _Statements:
    """The statements an engine runs inside :meth:`count` or :meth:`keep`: counted, or
    the SELECTs kept to replay under EXPLAIN for the shared buffers each touches."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._seen: list[tuple[str, Any]] = []
        self._keeping = False
        self.counted = 0

    def _record(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if not self._keeping:
            self.counted += 1
        elif not executemany and statement.lstrip().upper().startswith(("SELECT", "WITH")):
            self._seen.append((statement, parameters))

    def _during(self, fn: Callable[[], T], keeping: bool) -> T:
        self._keeping = keeping
        event.listen(self._engine, "before_cursor_execute", self._record)
        try:
            return fn()
        finally:
            event.remove(self._engine, "before_cursor_execute", self._record)

    def count(self, fn: Callable[[], T]) -> T:
        return self._during(fn, keeping=False)

    def keep(self, fn: Callable[[], T]) -> T:
        return self._during(fn, keeping=True)

    def blocks(self) -> dict[str, int]:
        """Shared buffers touched per distinct statement, summed over its executions."""
        out: Counter[str] = Counter()
        raw = self._engine.raw_connection()
        try:
            conn: Any = raw.driver_connection
            for stmt, params in self._seen:
                explain = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + stmt
                try:
                    with conn.transaction():  # a savepoint: one failure spoils no other
                        plan = conn.execute(explain, params).fetchone()[0][0]["Plan"]
                except Exception:  # noqa: BLE001 - logged and left out, not fatal
                    log.warning("soak could not explain a statement", exc_info=True)
                    continue
                out[stmt] += plan["Shared Hit Blocks"] + plan["Shared Read Blocks"]
            conn.rollback()  # replays take row and advisory locks: release them
        finally:
            raw.close()
        self._seen.clear()
        return dict(out)


def _percentiles(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"n": 0}
    ys = sorted(xs)
    rank = max(math.ceil(0.95 * len(ys)) - 1, 0)
    return {"n": len(ys), "p50": ys[len(ys) // 2], "p95": ys[rank], "max": ys[-1]}


def _pace(sim_now: datetime) -> None:
    """Never ahead of the wall clock: a future fix is refused at the ingest boundary."""
    ahead = (sim_now - datetime.now(UTC)).total_seconds()
    if ahead > 0:
        time.sleep(ahead)


def run(engine: Engine, cfg: SoakConfig, budgets: Budgets | None = None) -> Report:
    """Seed, drive the live phase, measure, and check the budgets. Raises RefusedError."""
    budgets = budgets or Budgets()
    check_target(engine, cfg.site_id)
    settings = get_settings()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    report = Report(config=asdict(cfg), budgets=asdict(budgets))

    # Live data starts far enough back to arrive faster than real time at first, but not
    # so far that offline detection (on the wall clock) takes the whole fleet for silent.
    catch_up_s = min(cfg.live_s, settings.offline_threshold_s / 2)
    live_start = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=catch_up_s)
    t0 = time.perf_counter()
    log.info("soak seeding", extra={"site_id": cfg.site_id, "days": cfg.days})
    fleet, report.seeded = _seed(engine, factory, cfg, live_start, settings)
    _vacuum_analyze(engine)
    report.seed_s = time.perf_counter() - t0
    report.first_tick_s = sum(o.duration_s for o in _tick(factory, settings, cfg.site_id))

    ticks = int(cfg.live_s // cfg.tick_every_s)
    measured = [*(["explain"] if cfg.explain else []), "memory"]
    ingest_s, fixes = 0.0, 0
    failed: set[str] = set()
    tick_s: list[float] = []
    step_s: dict[str, list[float]] = {}
    state_s: list[float] = []
    statements = _Statements(engine)
    one_tick = partial(_tick, factory, settings, cfg.site_id)
    one_state = partial(_state, factory, cfg.site_id)

    def ingest(batch: list[PositionIngest]) -> None:
        for p in batch:
            reject_if_future(p)
            with factory() as s:
                store_and_process(s, p)

    log.info("soak live phase", extra={"site_id": cfg.site_id, "ticks": ticks})
    for n in range(ticks):
        for _ in range(int(cfg.tick_every_s)):
            _pace(fleet.now)
            batch = fleet.step()
            started = time.perf_counter()
            statements.count(partial(ingest, batch))
            ingest_s += time.perf_counter() - started
            fixes += len(batch)
        kind = measured[n - ticks + len(measured)] if n >= ticks - len(measured) else "timed"
        if kind == "timed":
            outcomes = one_tick()
            tick_s.append(sum(o.duration_s for o in outcomes if o.site_id == cfg.site_id))
            for o in outcomes:
                step_s.setdefault(o.name, []).append(o.duration_s)
            state_s.append(one_state())
        elif kind == "memory":
            outcomes, report.memory["tick_mib"] = _peak_mib(one_tick)
            _, report.memory["state_mib"] = _peak_mib(one_state)
        else:
            outcomes = statements.keep(one_tick)
            report.statements["tick"] = statements.blocks()
            statements.keep(one_state)
            report.statements["state"] = statements.blocks()
        failed |= {f"{o.name}:{o.site_id or '*'}" for o in outcomes if not o.ok}

    rate = fixes / ingest_s if ingest_s else 0.0
    report.ingest = {
        "fixes": fixes,
        "seconds": ingest_s,
        "fixes_per_s": rate,
        "headroom": rate / cfg.assets,  # the fleet sends one fix per asset per second
        "statements_per_fix": statements.counted / fixes if fixes else 0.0,
    }
    report.tick = {**_percentiles(tick_s), "steps": {k: _percentiles(v) for k, v in step_s.items()}}
    report.state = _percentiles(state_s)
    report.failures = [f"tick step failed: {name}" for name in sorted(failed)]
    report.failures += _over_budget(report, budgets)
    return report


def _over_budget(r: Report, b: Budgets) -> list[str]:
    most = [
        ("tick p95 s", r.tick.get("p95", 0.0), b.tick_s),
        ("/state p95 s", r.state.get("p95", 0.0), b.state_s),
        ("statements per fix", r.ingest["statements_per_fix"], b.statements_per_fix),
        ("tick heap MiB", r.memory.get("tick_mib", 0.0), b.tick_mib),
        ("/state heap MiB", r.memory.get("state_mib", 0.0), b.state_mib),
    ]
    out = [f"{name} {got:.3f} over budget {limit}" for name, got, limit in most if got > limit]
    if r.ingest["headroom"] < b.ingest_headroom:
        out.append(f"ingest headroom {r.ingest['headroom']:.2f} under budget {b.ingest_headroom}")
    return out


def main(argv: list[str] | None = None) -> int:
    from minemonitor.logging_config import configure_logging
    from minemonitor.storage.db import get_engine

    d, b = SoakConfig(), Budgets()
    p = argparse.ArgumentParser(
        prog="python -m minemonitor.soak",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--assets", type=int, default=d.assets)
    p.add_argument("--days", type=float, default=d.days)
    p.add_argument("--history-interval-s", type=float, default=d.history_interval_s)
    p.add_argument("--live-minutes", type=float, default=d.live_s / 60)
    p.add_argument("--explain", action="store_true", help="report buffers per query")
    p.add_argument("--report", help="also write the JSON report to this file")
    for name in asdict(b):
        p.add_argument(f"--budget-{name.replace('_', '-')}", type=float, default=getattr(b, name))
    args = p.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    cfg = SoakConfig(
        days=args.days,
        assets=args.assets,
        history_interval_s=args.history_interval_s,
        live_s=args.live_minutes * 60,
        explain=args.explain,
    )
    budgets = Budgets(**{name: getattr(args, f"budget_{name}") for name in asdict(b)})
    try:
        report = run(get_engine(), cfg, budgets)
    except RefusedError as exc:
        print(f"soak refused: {exc}", file=sys.stderr)
        return 2
    out = json.dumps(report.to_json(), default=str, indent=2)
    print(out)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(out + "\n")
    for failure in report.failures:
        print(f"soak: {failure}", file=sys.stderr)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
