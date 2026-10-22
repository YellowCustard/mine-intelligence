"""The compliance pack: what a mine, as data controller, hands a POTRAZ inspector.

One zip per site, built on request, holding what this platform can attest to:

- ``processing.json`` — who is controller and processor, the personal data held (counts
  only), every outside recipient this deployment is configured to send data to, and the
  breach-notice settings;
- ``retention.json`` — the retention window for each data class and the last time the
  deletion job ran, with what it deleted;
- ``audit_log.csv`` — the retained audit trail for the site (and platform-wide entries);
- ``breaches.csv`` — the breach register with each notice's deadline and when it was sent;
- ``README.txt`` — what the pack is, when and by whom it was made.

It holds no names or contacts of the people in ``operators``. Recipient endpoints are
reduced to their host, so a token in a webhook URL never leaves the box.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minemonitor.breaches import service as breaches
from minemonitor.config import Settings, get_settings
from minemonitor.operations.shift_reports import report_targets
from minemonitor.storage.models import AuditLog, BreachOperator, BreachRecord, Operator, Site

AUDIT_COLUMNS = ["id", "ts", "actor", "action", "entity_type", "entity_id", "site_id", "detail"]
BREACH_COLUMNS = [
    "breach_id",
    "discovered_at",
    "occurred_at",
    "data_classes",
    "people_affected",
    "summary",
    "regulator_due_at",
    "regulator_notified_at",
    "regulator_reference",
    "regulator_status",
    "individuals_due_at",
    "individuals_notified_at",
    "individuals_status",
    "recorded_by",
    "closed_at",
    "closed_by",
]


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)).isoformat()


def _host(url: str) -> str:
    # Never the path or query: a webhook URL can carry its secret there.
    return urlsplit(url).hostname or "(unparseable URL)"


def recipients(settings: Settings) -> list[dict[str, str]]:
    """Outside parties this deployment is configured to send data to, by host."""
    out: list[dict[str, str]] = []
    if settings.notify_webhook_url:
        out.append(
            {
                "purpose": "alarm notifications (webhook): the alarm's event.v1",
                "host": _host(settings.notify_webhook_url),
            }
        )
    if settings.notify_smtp_host:
        out.append(
            {
                "purpose": "alarm notifications (email relay): alarm summary and severity",
                "host": settings.notify_smtp_host,
            }
        )
    if settings.notify_whatsapp_url:
        out.append(
            {
                "purpose": "alarm notifications (WhatsApp): alarm summary and severity",
                "host": _host(settings.notify_whatsapp_url),
            }
        )
    reports = {channel for channel, _ in report_targets(settings)}
    if "email" in reports:
        out.append(
            {
                "purpose": "shift reports (email relay): shift figures and machine ids",
                "host": settings.notify_smtp_host,
            }
        )
    if "whatsapp" in reports:
        out.append(
            {
                "purpose": "shift reports (WhatsApp): shift figures and machine ids",
                "host": _host(settings.notify_whatsapp_url),
            }
        )
    return out


def _csv_text(columns: list[str], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns)
    w.writeheader()
    for row in rows:
        w.writerow({k: breaches.csv_cell(row.get(k)) for k in columns})
    return buf.getvalue()


def build(
    session: Session, site_id: str, *, actor: str, now: datetime | None = None
) -> tuple[bytes, dict[str, int]]:
    """The pack as zip bytes, and the row counts it holds (for the audit entry)."""
    now = now or datetime.now(UTC)
    settings = get_settings()
    site = session.get(Site, site_id)

    audit_rows = (
        session.execute(
            select(AuditLog)
            .where((AuditLog.site_id == site_id) | (AuditLog.site_id.is_(None)))
            .order_by(AuditLog.ts, AuditLog.id)
        )
        .scalars()
        .all()
    )
    audit_csv = _csv_text(
        AUDIT_COLUMNS,
        [
            {
                "id": a.id,
                "ts": _iso(a.ts),
                "actor": a.actor,
                "action": a.action,
                "entity_type": a.entity_type,
                "entity_id": a.entity_id,
                "site_id": a.site_id,
                "detail": json.dumps(a.detail, sort_keys=True, default=str)
                if a.detail is not None
                else None,
            }
            for a in audit_rows
        ],
    )

    warn = timedelta(hours=settings.breach_warn_before_h)
    people: dict[str, int] = {
        breach_id: n
        for breach_id, n in session.execute(
            select(BreachOperator.breach_id, func.count())
            .join(BreachRecord, BreachRecord.breach_id == BreachOperator.breach_id)
            .where(BreachRecord.site_id == site_id)
            .group_by(BreachOperator.breach_id)
        ).all()
    }
    breach_rows = (
        session.execute(
            select(BreachRecord)
            .where(BreachRecord.site_id == site_id)
            .order_by(BreachRecord.discovered_at)
        )
        .scalars()
        .all()
    )
    breaches_csv = _csv_text(
        BREACH_COLUMNS,
        [
            {
                "breach_id": b.breach_id,
                "discovered_at": _iso(b.discovered_at),
                "occurred_at": _iso(b.occurred_at),
                "data_classes": " ".join(b.data_classes),
                "people_affected": people.get(b.breach_id, 0),
                "summary": b.summary,
                "regulator_due_at": _iso(b.regulator_due_at),
                "regulator_notified_at": _iso(b.regulator_notified_at),
                "regulator_reference": b.regulator_reference,
                "regulator_status": breaches.notice_status(b, "regulator", now, warn),
                "individuals_due_at": _iso(b.individuals_due_at),
                "individuals_notified_at": _iso(b.individuals_notified_at),
                "individuals_status": breaches.notice_status(b, "individuals", now, warn),
                "recorded_by": b.recorded_by,
                "closed_at": _iso(b.closed_at),
                "closed_by": b.closed_by,
            }
            for b in breach_rows
        ],
    )

    operators = session.execute(
        select(func.count(), func.count(Operator.erased_at)).where(Operator.site_id == site_id)
    ).one()
    processing = {
        "site": {
            "site_id": site_id,
            "name": site.name if site is not None else None,
            "timezone": site.timezone if site is not None else None,
        },
        "controller": "the mine operating this site",
        "processor": "eigenstate (eigenstatesystems.com), which supplies and supports this "
        "platform",
        "personal_data": {
            "operators": operators[0],
            "operators_erased": operators[1],
            "where": "the operators table only; everything else refers to people by an "
            "opaque operator id",
            "biometric_data": "none: a gate face terminal yields only an access event; images "
            "and templates stay in the terminal",
        },
        "outside_recipients": recipients(settings),
        "breach_notice": {
            "regulator": settings.breach_regulator_name,
            "regulator_notice_h": settings.breach_regulator_notice_h,
            "individuals_notice_h": settings.breach_individuals_notice_h,
            "deadline_from": settings.breach_deadline_from,
            "warn_before_h": settings.breach_warn_before_h,
        },
    }

    last_run = session.execute(
        select(AuditLog)
        .where(AuditLog.action == "retention.run")
        .order_by(AuditLog.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    retention = {
        "days_kept_by_class": {
            "positions": settings.retain_positions_days,
            "metrics_and_cycles": settings.retain_metrics_days,
            "events": settings.retain_events_days,
            "annotations": settings.retain_annotations_days,
            "gate_passages": settings.retain_access_days,
            "audit_log_and_closed_breaches": settings.retain_audit_days,
            "vision_observations": settings.retain_vision_observations_days,
            "vision_health": settings.retain_vision_health_days,
            "operational_records": settings.retain_records_days,
            "equipment_readings": settings.retain_readings_days,
        },
        "zero_means": "kept until deleted by hand",
        "job_interval_s": settings.retention_interval_s,
        "last_run": (
            {"at": _iso(last_run.ts), "detail": last_run.detail} if last_run is not None else None
        ),
    }

    readme = (
        f"Mine Monitor compliance pack for site {site_id}\n"
        f"Generated {now.isoformat()} by {actor}.\n\n"
        "processing.json  controller and processor, personal data held (counts),\n"
        "                 outside recipients, breach-notice settings\n"
        "retention.json   retention window per data class and the last deletion run\n"
        f"audit_log.csv    the retained audit trail ({len(audit_rows)} entries)\n"
        f"breaches.csv     the breach register ({len(breach_rows)} breaches)\n\n"
        "Not included, because the mine holds them: its registration with POTRAZ as a\n"
        "data controller and its data protection officer's details.\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, text in (
            ("README.txt", readme),
            ("processing.json", json.dumps(processing, indent=2, sort_keys=True)),
            ("retention.json", json.dumps(retention, indent=2, sort_keys=True, default=str)),
            ("audit_log.csv", audit_csv),
            ("breaches.csv", breaches_csv),
        ):
            zf.writestr(name, text)
    return buf.getvalue(), {"audit_entries": len(audit_rows), "breaches": len(breach_rows)}
