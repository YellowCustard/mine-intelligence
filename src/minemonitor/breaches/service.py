"""Record a personal-data breach and keep its notice deadlines in front of people.

The mine is the data controller. When personal data held here is lost, exposed or altered
it must tell the regulator (reported: within 24 h) and the people affected (reported:
within 72 h). This module records the breach, fixes both deadlines when it is recorded,
and on each maintenance tick raises an advisory ``breach_notice_due`` alarm as a deadline
approaches (warning) and again if it passes unsent (critical). Recording the notice, or
closing the breach, stops its alarms and acknowledges the ones already raised.

The deadlines are settings (``MM_BREACH_*``) because the legal position is still being
confirmed, so a different answer is a configuration change. They are copied onto the
breach when it is recorded, so changing the settings never moves a deadline already
running.

An alarm carries the breach id and the deadline, nothing more. Alarms are pushed out to
notification channels (webhook, email, WhatsApp); the breach summary and the people
affected stay in this database, behind admin-only endpoints.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from minemonitor import audit
from minemonitor.config import Settings, get_settings
from minemonitor.contracts import EventV1
from minemonitor.events.repository import persist_event
from minemonitor.storage.models import BreachOperator, BreachRecord, Event, Operator, Site

SOURCE = "breach_register"

# What kind of personal data a breach touched. Coarse on purpose: the regulator asks for
# categories, and the detail belongs in the summary.
DataClass = Literal[
    "operator_records",  # names, employee refs, contacts (the operators table)
    "positions",  # machine positions that can be tied to an operator
    "access_events",  # gate passages
    "events",  # alarms, which may carry an operator reference
    "audit_log",
    "evidence",  # clips and images
    "other",
]
DATA_CLASSES: tuple[str, ...] = get_args(DataClass)

Notice = Literal["regulator", "individuals"]
NOTICES: tuple[Notice, ...] = ("regulator", "individuals")

# How a notice stands. "due_soon" and "overdue" are the two alarm stages.
NoticeStatus = Literal["sent", "not_required", "not_sent", "pending", "due_soon", "overdue"]

AFFECTED_COLUMNS = ["operator_id", "display_name", "employee_ref", "contact", "erased"]


class BreachError(ValueError):
    """The request contradicts the record (refused with 422)."""


class BreachConflict(BreachError):
    """The request does not apply to the breach as it stands (refused with 409)."""


def _utc(dt: datetime) -> datetime:
    # SQLite hands back naive datetimes; everything stored here is UTC.
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def deadlines(
    *,
    discovered_at: datetime,
    occurred_at: datetime | None,
    notify_individuals: bool,
    settings: Settings,
) -> tuple[datetime, datetime | None]:
    """The (regulator, individuals) notice deadlines for a breach under ``settings``."""
    start = discovered_at
    if settings.breach_deadline_from == "occurred" and occurred_at is not None:
        start = occurred_at
    start = _utc(start)
    regulator = start + timedelta(hours=settings.breach_regulator_notice_h)
    individuals = (
        start + timedelta(hours=settings.breach_individuals_notice_h)
        if notify_individuals
        else None
    )
    return regulator, individuals


def record_breach(
    session: Session,
    *,
    site_id: str,
    discovered_at: datetime,
    occurred_at: datetime | None,
    summary: str,
    data_classes: list[str],
    operator_ids: list[str],
    notify_individuals: bool,
    actor: str,
    now: datetime | None = None,
) -> BreachRecord:
    """Record a breach and fix its notice deadlines. Audited; the caller commits."""
    now = now or datetime.now(UTC)
    settings = get_settings()
    if _utc(discovered_at) > now + timedelta(seconds=settings.max_future_skew_s):
        raise BreachError("discovered_at is in the future")
    if occurred_at is not None and _utc(occurred_at) > _utc(discovered_at):
        raise BreachError("occurred_at is after discovered_at")
    unknown_classes = sorted(set(data_classes) - set(DATA_CLASSES))
    if not data_classes or unknown_classes:
        raise BreachError(f"data_classes must be one or more of {list(DATA_CLASSES)}")
    ids = sorted(set(operator_ids))
    if ids:
        known = set(
            session.execute(
                select(Operator.operator_id).where(
                    Operator.site_id == site_id, Operator.operator_id.in_(ids)
                )
            ).scalars()
        )
        missing = [i for i in ids if i not in known]
        if missing:
            raise BreachError(f"no such operators at this site: {missing[:20]}")

    regulator_due, individuals_due = deadlines(
        discovered_at=discovered_at,
        occurred_at=occurred_at,
        notify_individuals=notify_individuals,
        settings=settings,
    )
    row = BreachRecord(
        breach_id=str(ULID()),
        site_id=site_id,
        discovered_at=_utc(discovered_at),
        occurred_at=_utc(occurred_at) if occurred_at is not None else None,
        summary=summary,
        data_classes=sorted(set(data_classes)),
        notify_individuals=notify_individuals,
        regulator_due_at=regulator_due,
        individuals_due_at=individuals_due,
        recorded_by=actor,
        recorded_at=now,
    )
    session.add(row)
    session.flush()  # the links below reference it
    session.add_all(BreachOperator(breach_id=row.breach_id, operator_id=i) for i in ids)
    audit.record(
        session,
        actor=actor,
        action="breach.record",
        entity_type="breach",
        entity_id=row.breach_id,
        site_id=site_id,
        detail={
            "data_classes": row.data_classes,
            "operators": len(ids),
            "notify_individuals": notify_individuals,
            "regulator_due_at": regulator_due.isoformat(),
            "individuals_due_at": individuals_due.isoformat() if individuals_due else None,
        },
    )
    return row


def notice_status(
    breach: BreachRecord, notice: Notice, now: datetime, warn: timedelta
) -> NoticeStatus:
    """Where one of a breach's two notices stands at ``now``."""
    sent: datetime | None
    due: datetime | None
    if notice == "regulator":
        sent, due = breach.regulator_notified_at, breach.regulator_due_at
    else:
        sent, due = breach.individuals_notified_at, breach.individuals_due_at
    if sent is not None:
        return "sent"
    if due is None:
        return "not_required"
    if breach.closed_at is not None:
        return "not_sent"
    due_utc = _utc(due)
    if now >= due_utc:
        return "overdue"
    if warn > timedelta(0) and now >= due_utc - warn:
        return "due_soon"
    return "pending"


def _alarm_ids(breach_id: str, notices: tuple[Notice, ...]) -> list[str]:
    return [f"breach-{breach_id}-{n}-{stage}" for n in notices for stage in ("due", "overdue")]


def _acknowledge_alarms(
    session: Session, breach: BreachRecord, notices: tuple[Notice, ...], actor: str, now: datetime
) -> int:
    n = 0
    for event_id in _alarm_ids(breach.breach_id, notices):
        ev = session.get(Event, event_id)
        if ev is not None and ev.state == "open":
            ev.state = "acknowledged"
            ev.acknowledged_by = actor
            ev.acknowledged_at = now
            n += 1
    return n


def record_notice(
    session: Session,
    breach: BreachRecord,
    notice: Notice,
    *,
    notified_at: datetime,
    reference: str | None,
    actor: str,
    now: datetime | None = None,
) -> None:
    """Record that a notice went out. Stops its alarms. Audited; the caller commits.

    Recording it again replaces the time (a correction); the audit entry keeps the old one.
    """
    now = now or datetime.now(UTC)
    if _utc(notified_at) > now + timedelta(seconds=get_settings().max_future_skew_s):
        raise BreachError("notified_at is in the future")
    if notice == "individuals" and not breach.notify_individuals:
        raise BreachConflict("this breach was recorded as not needing notice to individuals")
    if notice == "regulator":
        previous = breach.regulator_notified_at
        breach.regulator_notified_at = _utc(notified_at)
        breach.regulator_reference = reference
    else:
        previous = breach.individuals_notified_at
        breach.individuals_notified_at = _utc(notified_at)
    acked = _acknowledge_alarms(session, breach, (notice,), actor, now)
    audit.record(
        session,
        actor=actor,
        action=f"breach.{notice}_notified",
        entity_type="breach",
        entity_id=breach.breach_id,
        site_id=breach.site_id,
        detail={
            "notified_at": _utc(notified_at).isoformat(),
            "reference": reference,
            "previous": _utc(previous).isoformat() if previous is not None else None,
            "alarms_acknowledged": acked,
        },
    )


def close_breach(
    session: Session,
    breach: BreachRecord,
    *,
    note: str | None,
    actor: str,
    now: datetime | None = None,
) -> None:
    """Close a breach: its alarms stop. Audited, with any notice still unsent."""
    now = now or datetime.now(UTC)
    if breach.closed_at is not None:
        raise BreachConflict("breach is already closed")
    outstanding = [
        n for n in NOTICES if notice_status(breach, n, now, timedelta(0)) in ("pending", "overdue")
    ]
    breach.closed_at = now
    breach.closed_by = actor
    breach.close_note = note
    acked = _acknowledge_alarms(session, breach, NOTICES, actor, now)
    audit.record(
        session,
        actor=actor,
        action="breach.close",
        entity_type="breach",
        entity_id=breach.breach_id,
        site_id=breach.site_id,
        detail={"outstanding_notices": outstanding, "alarms_acknowledged": acked},
    )


def check_breach_deadlines(
    session: Session, site_id: str, *, now: datetime | None = None
) -> list[EventV1]:
    """Raise the alarms for this site's notice deadlines. Runs on the maintenance tick. Commits.

    Each notice of each open breach raises at most one warning (from
    ``MM_BREACH_WARN_BEFORE_H`` before it is due) and one critical alarm (once it is
    overdue). A tick that first sees a notice already overdue raises only the critical one.
    """
    now = now or datetime.now(UTC)
    settings = get_settings()
    warn = timedelta(hours=settings.breach_warn_before_h)
    site = session.get(Site, site_id)
    tz = ZoneInfo(site.timezone if site is not None and site.timezone else "Africa/Harare")
    rows = (
        session.execute(
            select(BreachRecord)
            .where(BreachRecord.site_id == site_id, BreachRecord.closed_at.is_(None))
            .order_by(BreachRecord.discovered_at)
        )
        .scalars()
        .all()
    )
    events: list[EventV1] = []
    for row in rows:
        for notice in NOTICES:
            status = notice_status(row, notice, now, warn)
            if status not in ("due_soon", "overdue"):
                continue
            stage = "overdue" if status == "overdue" else "due"
            event_id = f"breach-{row.breach_id}-{notice}-{stage}"
            if session.get(Event, event_id) is not None:
                continue  # already raised
            if notice == "regulator":
                due, who = _utc(row.regulator_due_at), settings.breach_regulator_name
            else:
                # due_soon/overdue imply a deadline, so individuals_due_at is set here.
                assert row.individuals_due_at is not None
                due, who = _utc(row.individuals_due_at), "the people affected"
            local = due.astimezone(tz).strftime("%a %d %b %H:%M")
            summary = (
                f"Breach {row.breach_id}: notice to {who} overdue since {local}"
                if stage == "overdue"
                else f"Breach {row.breach_id}: notify {who} by {local}"
            )
            ev = EventV1(
                schema="event.v1",
                event_id=event_id,
                site_id=site_id,
                ts=now,
                type="breach_notice_due",
                severity="critical" if stage == "overdue" else "warning",
                asset_id=None,
                zone_id=None,
                source=SOURCE,
                summary=summary,
                detail={
                    "breach_id": row.breach_id,
                    "notice": notice,
                    "stage": stage,
                    "due_at": due.isoformat(),
                },
                evidence={"breach_id": row.breach_id},
                advisory=True,
                state="open",
            )
            persist_event(session, ev)
            events.append(ev)
    session.commit()
    return events


def affected_operators(session: Session, breach: BreachRecord) -> list[Operator]:
    """The people a breach affected, by operator id."""
    return list(
        session.execute(
            select(Operator)
            .join(BreachOperator, BreachOperator.operator_id == Operator.operator_id)
            .where(BreachOperator.breach_id == breach.breach_id)
            .order_by(Operator.operator_id)
        )
        .scalars()
        .all()
    )


# A text cell starting with one of these is run as a formula by spreadsheet software
# (CSV injection, CWE-1236). Operator names and contacts are admin-entered free text.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def csv_cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, str) and v.startswith(_FORMULA_PREFIXES):
        return "'" + v
    return v


def affected_operators_csv(operators: list[Operator]) -> str:
    """The people affected, as CSV, for the notice to individuals. Erased people have no PII."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=AFFECTED_COLUMNS)
    w.writeheader()
    for op in operators:
        w.writerow(
            {
                "operator_id": csv_cell(op.operator_id),
                "display_name": csv_cell(op.display_name),
                "employee_ref": csv_cell(op.employee_ref),
                "contact": csv_cell(op.contact),
                "erased": "true" if op.erased_at is not None else "false",
            }
        )
    return buf.getvalue()
