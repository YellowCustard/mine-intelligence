"""Shift reports sent by email and WhatsApp after each shift ends.

A mine manager reads the shift on a phone, not on the dashboard. After each shift ends, plus
``MM_REPORT_DELAY_S`` for fixes a tracker buffered through a network drop to arrive and the
shift's last cycles to close, the maintenance tick renders the shift's headline figures and
queues them for each recipient in ``MM_REPORT_EMAIL_TO`` and ``MM_REPORT_WHATSAPP_TO``. A
dispatcher then sends them with retry and backoff through the alarm channels' transports
(brief §3: the network will fail, so nothing is sent inline).

What leaves the mine: the shift scorecard's figures (cycles, cycle time, queue, utilisation,
alarm and incident counts, classified downtime) and machine ids. Never an operator name, never
handover notes or incident text (brief §4). The full report stays behind the dashboard login,
linked when ``MM_PUBLIC_URL`` is set.

Every figure is the observed one, labelled as the scorecard labels it, with no target or
benchmark (brief §9). The report names the machines that had not reported up to the end of the
shift, because figures computed while their fixes are still buffered on a truck would otherwise
read as final.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from minemonitor.config import Settings, get_settings
from minemonitor.notifications.dispatch import Sender, WhatsAppTemplate
from minemonitor.operations.scorecard import compute_scorecard
from minemonitor.operations.shifts import ShiftWindow, site_timezone, windows_ending_between
from minemonitor.storage.models import ShiftReportDelivery
from minemonitor.storage.repositories import latest_positions

log = logging.getLogger("minemonitor.reports")

# The WhatsApp template's body placeholders, in order (docs/OPERATIONS.md, Shift reports).
TEMPLATE_PARAMS = (
    "site",
    "shift",
    "cycles",
    "mean cycle time",
    "queue",
    "alarms",
    "critical alarms",
    "data note",
)
_LATE_LISTED = 5  # machines named in the data note; the rest are counted


@dataclass(frozen=True)
class Digest:
    """A shift's report as it is sent: an email subject and body, and the template values."""

    subject: str
    text: str
    params: tuple[str, ...]


def _recipients(raw: str) -> list[str]:
    return [r.strip() for r in raw.split(",") if r.strip()]


def report_targets(s: Settings) -> list[tuple[str, str]]:
    """The (channel, recipient) pairs shift reports go to. Empty = reports off."""
    out: list[tuple[str, str]] = []
    if s.notify_smtp_host:
        out += [("email", addr) for addr in _recipients(s.report_email_to)]
    if s.notify_whatsapp_url:
        out += [("whatsapp", num) for num in _recipients(s.report_whatsapp_to)]
    return out


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def late_assets(session: Session, site_id: str, window: ShiftWindow) -> list[str]:
    """Machines that reported during the shift but have nothing stored from its end on.

    Each is offline, parked with its tracker quiet, or still holding fixes it has not sent:
    the shift's figures may not include all of its work yet.
    """
    return sorted(
        p.asset_id
        for p in latest_positions(session, site_id)
        if window.start <= _aware(p.ts) < window.end
    )


def _minutes(seconds: float | None) -> str:
    if seconds is None:
        return "none"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{seconds / 60:.1f} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _pct(value: float | None) -> str:
    return "none" if value is None else f"{value:.1f}%"


def _data_note(late: Sequence[str]) -> str:
    if not late:
        return "Every machine that worked the shift reported to its end."
    named = ", ".join(late[:_LATE_LISTED])
    more = f" and {len(late) - _LATE_LISTED} more" if len(late) > _LATE_LISTED else ""
    machines = "1 machine" if len(late) == 1 else f"{len(late)} machines"
    return (
        f"{machines} had not reported up to the end of the shift ({named}{more}): "
        "figures may still change."
    )


def report_link(public_url: str, window: ShiftWindow) -> str | None:
    if not public_url.strip():
        return None
    base = public_url.strip().rstrip("/")
    query = urlencode({"shift_id": window.shift_id})
    return f"{base}/api/v1/sites/{quote(window.site_id, safe='')}/reports/shift.html?{query}"


def render_digest(
    scorecard: dict[str, Any],
    window: ShiftWindow,
    late: Sequence[str],
    *,
    tz: ZoneInfo,
    link: str | None,
    as_of: datetime,
) -> Digest:
    """The shift's headline figures as an email and as WhatsApp template values."""
    cycles = scorecard["cycles"]
    alarms = scorecard["safety_events"]
    by_severity: dict[str, int] = alarms["by_severity"]
    start = window.start.astimezone(tz)
    end = window.end.astimezone(tz)
    shift = (
        f"{window.name.title()} shift {window.operating_date.isoformat()}, "
        f"{start:%H:%M} to {end:%H:%M}"
    )
    note = _data_note(late)
    params = (
        window.site_id,
        shift,
        str(cycles["count"]),
        _minutes(cycles["mean_cycle_time_s"]),
        _pct(cycles["queue_pct"]),
        str(alarms["total"]),
        str(by_severity.get("critical", 0)),
        note,
    )
    severities = ", ".join(
        f"{sev} {by_severity[sev]}"
        for sev in ("critical", "warning", "info")
        if by_severity.get(sev)
    )
    delays = scorecard["delays"]
    categories = ", ".join(
        f"{cat} {_minutes(secs)}"
        for cat, secs in sorted(delays["by_category"].items(), key=lambda kv: -kv[1])
    )
    lines = [
        "Mine Monitor shift report (advisory: this system warns; it never controls plant)",
        f"Site {window.site_id}: {shift} ({tz.key})",
        "",
        f"Haul cycles completed: {cycles['count']}",
        f"Mean cycle time: {_minutes(cycles['mean_cycle_time_s'])}",
        f"Queue at the face: {_pct(cycles['queue_pct'])} of cycle time (observed)",
        f"Utilisation: {_pct(scorecard['utilisation']['utilisation_pct'])} (derived from "
        "moving and idle time)",
        f"Alarms: {alarms['total']}" + (f" ({severities})" if severities else ""),
        f"Incidents opened: {scorecard['incidents']['opened_this_shift']}; still open: "
        f"{scorecard['incidents']['open_now']}",
        f"Classified downtime: {_minutes(delays['classified_total_s'])}"
        + (f" ({categories})" if categories else ""),
        "",
        note,
        f"Figures as of {as_of.astimezone(tz):%Y-%m-%d %H:%M} {tz.key}.",
    ]
    if link:
        lines.append(f"Full report (dashboard login): {link}")
    return Digest(
        subject=f"Shift report: {window.site_id}, {shift}",
        text="\n".join(lines) + "\n",
        params=params,
    )


def whatsapp_text(params: Sequence[str]) -> str:
    """The report as one message, worded as the approved template is (for plain-text mode)."""
    site, shift, cycles, mean, queue, total, critical, note = params
    return (
        f"Mine Monitor shift report for {site}, {shift}: {cycles} haul cycles, mean cycle time "
        f"{mean}, queue at the face {queue} of cycle time, {total} alarms of which {critical} "
        f"critical. {note} Advisory only; this system never controls plant."
    )


def _delivery_id(site_id: str, shift_id: str, channel: str, target: str) -> str:
    key = "\x1f".join((site_id, shift_id, channel, target))
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def queue_due_reports(
    session: Session,
    site_id: str,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> int:
    """Queue the report of every shift that ended long enough ago. Returns rows added.

    A shift is due once ``report_delay_s`` has passed since it ended, and is skipped once
    ``report_catchup_h`` has. Idempotent: a recipient already queued for a shift is never
    queued again, whatever the tick does. The caller commits.
    """
    s = settings or get_settings()
    targets = report_targets(s)
    if not targets:
        return 0
    now = now or datetime.now(UTC)
    until = now - timedelta(seconds=s.report_delay_s)
    windows = windows_ending_between(
        session, site_id, until - timedelta(hours=s.report_catchup_h), until
    )
    if not windows:
        return 0
    queued = set(
        session.execute(
            select(
                ShiftReportDelivery.shift_id,
                ShiftReportDelivery.channel,
                ShiftReportDelivery.target,
            ).where(
                ShiftReportDelivery.site_id == site_id,
                ShiftReportDelivery.shift_id.in_([w.shift_id for w in windows]),
            )
        ).tuples()
    )
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    tz = site_timezone(session, site_id)
    added = 0
    for window in windows:
        missing = [(c, t) for c, t in targets if (window.shift_id, c, t) not in queued]
        if not missing:
            continue
        digest = render_digest(
            compute_scorecard(session, site_id, window),
            window,
            late_assets(session, site_id, window),
            tz=tz,
            link=report_link(s.public_url, window),
            as_of=now,
        )
        for channel, target in missing:
            inserted = session.execute(
                insert(ShiftReportDelivery)
                .values(
                    id=_delivery_id(site_id, window.shift_id, channel, target),
                    site_id=site_id,
                    shift_id=window.shift_id,
                    shift_end=window.end,
                    channel=channel,
                    target=target,
                    subject=digest.subject,
                    body=digest.text,
                    params=list(digest.params),
                    state="pending",
                    attempts=0,
                    created_at=now,
                    next_attempt_at=now,
                )
                .on_conflict_do_nothing()
                # RETURNING, not rowcount: psycopg 3 reports -1 for ON CONFLICT DO NOTHING.
                .returning(ShiftReportDelivery.id)
            ).first()
            added += inserted is not None
        log.info(
            "shift report queued",
            extra={"site_id": site_id, "shift_id": window.shift_id, "recipients": len(missing)},
        )
    return added


def _send(sender: Sender, row: ShiftReportDelivery, s: Settings) -> None:
    if row.channel == "email":
        sender.email(row.target, row.subject, row.body)
    elif row.channel == "whatsapp":
        template = (
            WhatsAppTemplate(
                name=s.report_whatsapp_template.strip(),
                language=s.notify_whatsapp_template_lang.strip(),
                params=tuple(row.params),
            )
            if s.report_whatsapp_template.strip()
            else None
        )
        sender.whatsapp(row.target, whatsapp_text(row.params), template)
    else:
        raise RuntimeError(f"unknown channel {row.channel!r}")


def send_due_reports(
    session: Session,
    *,
    sender: Sender,
    settings: Settings | None = None,
    now: datetime | None = None,
    batch: int = 50,
) -> dict[str, int]:
    """Send queued shift reports that are due, with the alarm outbox's retry rules. Commits."""
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    rows = (
        session.execute(
            select(ShiftReportDelivery)
            .where(
                ShiftReportDelivery.state == "pending",
                ShiftReportDelivery.next_attempt_at <= now,
            )
            .order_by(ShiftReportDelivery.next_attempt_at)
            .limit(batch)
        )
        .scalars()
        .all()
    )
    result = {"sent": 0, "retry": 0, "failed": 0}
    for row in rows:
        row.attempts += 1
        try:
            _send(sender, row, s)
        except Exception as exc:  # noqa: BLE001 - any delivery error is retryable
            row.last_error = str(exc)[:500]
            if row.attempts >= s.notify_max_attempts:
                row.state = "failed"
                result["failed"] += 1
                log.warning(
                    "shift report failed permanently",
                    extra={"site_id": row.site_id, "channel": row.channel, "error": row.last_error},
                )
            else:
                backoff = s.notify_retry_base_s * (2 ** (row.attempts - 1))
                row.next_attempt_at = now + timedelta(seconds=backoff)
                result["retry"] += 1
        else:
            row.state = "sent"
            row.sent_at = now
            result["sent"] += 1
        # One row at a time: a power cut mid-batch re-sends at most the report in flight.
        session.commit()
    return result
