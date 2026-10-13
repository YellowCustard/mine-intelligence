"""The ingestor's maintenance tick: alarms, analytics and housekeeping for every site.

Every site in ``sites``, plus the configured default site, gets each per-site step
(audit H5: a second site used to get no offline alarms, cycles or metrics). Each step
runs on its own (audit H6): a failure is rolled back and logged with its step, site and
duration, and the remaining steps still run. Each attempt also leaves a ``tick:ok:`` or
``tick:failed:`` heartbeat, so system health can show a step that keeps failing even
though the ingestor itself is alive.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor import heartbeat, retention
from minemonitor.access import service as access
from minemonitor.breaches import service as breaches
from minemonitor.config import Settings
from minemonitor.cycles import recompute
from minemonitor.ingest.adapters import dahua_gate
from minemonitor.notifications import dispatch
from minemonitor.operations import shift_reports
from minemonitor.rules import occupancy, offline
from minemonitor.storage.models import ServiceHeartbeat
from minemonitor.storage.repositories import served_site_ids
from minemonitor.vision import service as vision

log = logging.getLogger("minemonitor.mqtt")

_PREFIX = "tick"
_GLOBAL = "*"  # the site part of a heartbeat key for a step that spans all sites


@dataclass(frozen=True)
class Step:
    name: str
    site_id: str | None  # None: one run for the whole platform
    run: Callable[[Session], object]


@dataclass(frozen=True)
class Outcome:
    name: str
    site_id: str | None
    ok: bool
    duration_s: float


def _key(status: str, name: str, site_id: str | None) -> str:
    return f"{_PREFIX}:{status}:{name}:{site_id or _GLOBAL}"


def _logged(kind: str, field: str, events: list[Any], level: int = logging.INFO) -> None:
    for ev in events:
        log.log(level, kind, extra={"site_id": ev.site_id, field: getattr(ev, field)})


def plan(
    session: Session,
    settings: Settings,
    *,
    gate_source: dahua_gate.GateEventSource | None,
    notify_sender: dispatch.Sender,
    retention_due: bool,
) -> list[Step]:
    """The steps of one tick, in order: alarms for every site first, then analytics,
    then platform-wide housekeeping."""
    sites = served_site_ids(session, settings.default_site_id)
    per_site: list[tuple[str, Callable[[str, Session], object]]] = [
        (
            "offline",
            lambda site, s: _logged(
                "asset offline",
                "asset_id",
                offline.detect_offline(s, site, threshold_s=settings.offline_threshold_s),
            ),
        ),
        (
            "occupancy",
            lambda site, s: _logged(
                "zone occupancy breach", "zone_id", occupancy.detect_zone_occupancy(s, site)
            ),
        ),
        (
            "camera_offline",
            lambda site, s: _logged(
                "camera offline",
                "detail",
                vision.detect_camera_offline(s, site, stale_s=settings.vision_camera_stale_s),
            ),
        ),
        (
            "missed_search",
            lambda site, s: _logged(
                "search missed", "detail", access.detect_missed_searches(s, site)
            ),
        ),
        (
            "breach_deadlines",
            lambda site, s: _logged(
                "breach notice due",
                "detail",
                breaches.check_breach_deadlines(s, site),
                level=logging.WARNING,
            ),
        ),
    ]
    steps = [Step(name, site, partial(fn, site)) for name, fn in per_site for site in sites]
    if gate_source is not None:
        # One gate feed is configured, and it belongs to the default site.
        steps.append(
            Step(
                "gate_poll",
                settings.default_site_id,
                lambda s: dahua_gate.poll_once(
                    s,
                    settings.default_site_id,
                    gate_source,
                    source_system=settings.access_gate_source_system,
                    overlap_s=settings.access_gate_overlap_s,
                ),
            )
        )
    steps += [Step("cycles", site, partial(_cycles, site)) for site in sites]
    # After the cycles step, so a shift's report counts the cycles that closed in it.
    steps += [Step("shift_reports", site, partial(_queue_reports, site)) for site in sites]
    steps.append(Step("notifications", None, lambda s: _dispatch(s, notify_sender)))
    steps.append(Step("report_delivery", None, lambda s: _send_reports(s, notify_sender)))
    if retention_due:
        steps.append(Step("retention", None, _retention))
    return steps


def _cycles(site: str, session: Session) -> None:
    recompute.refresh(session, site)


def _dispatch(session: Session, sender: dispatch.Sender) -> None:
    # Drain the notification outbox (store-and-forward alerts, brief §3). No-op unless
    # notifications are configured and rows are due.
    outcome = dispatch.dispatch_pending(session, sender=sender)
    if outcome["sent"] or outcome["failed"]:
        log.info("notifications dispatched", extra=outcome)


def _queue_reports(site: str, session: Session) -> None:
    shift_reports.queue_due_reports(session, site)


def _send_reports(session: Session, sender: dispatch.Sender) -> None:
    outcome = shift_reports.send_due_reports(session, sender=sender)
    if outcome["sent"] or outcome["failed"]:
        log.info("shift reports dispatched", extra=outcome)


def _retention(session: Session) -> None:
    deleted = retention.run_from_config(session)
    log.info("retention run", extra={"deleted": deleted})


def run(session: Session, steps: list[Step], *, now: datetime | None = None) -> list[Outcome]:
    """Run each step in its own transaction; one failing never stops the next."""
    outcomes: list[Outcome] = []
    for step in steps:
        t0 = time.monotonic()
        ok = True
        try:
            step.run(session)
            session.commit()
        except Exception:  # noqa: BLE001 - isolate the step; the next tick retries it
            ok = False
            session.rollback()
            log.exception(
                "maintenance step failed",
                extra={
                    "step": step.name,
                    "site_id": step.site_id,
                    "duration_ms": round((time.monotonic() - t0) * 1000),
                },
            )
        outcomes.append(Outcome(step.name, step.site_id, ok, time.monotonic() - t0))
        _record(session, step, ok, now or datetime.now(UTC))
    return outcomes


def _record(session: Session, step: Step, ok: bool, now: datetime) -> None:
    try:
        heartbeat.beat(session, _key("ok" if ok else "failed", step.name, step.site_id), now=now)
        session.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must not stop the tick
        session.rollback()
        log.warning(
            "maintenance step outcome not recorded",
            extra={"step": step.name, "site_id": step.site_id},
        )


def failing_steps(session: Session, site_id: str) -> list[str]:
    """Steps for ``site_id`` (or platform-wide) whose latest attempt failed."""
    last: dict[tuple[str, str], dict[str, datetime]] = {}
    rows = session.execute(
        select(ServiceHeartbeat).where(ServiceHeartbeat.service.like(f"{_PREFIX}:%"))
    ).scalars()
    for row in rows:
        _, status, name, site = row.service.split(":", 3)
        if site in (site_id, _GLOBAL):
            last.setdefault((name, site), {})[status] = row.ts
    failing = {
        name
        for (name, _), seen in last.items()
        if "failed" in seen and ("ok" not in seen or seen["failed"] >= seen["ok"])
    }
    return sorted(failing)
