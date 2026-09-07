"""Automated shift and daily reports.

A report is a **pure read** composed from already-stored data — the shift
scorecard, the incidents raised, the delays classified, and any handover — so it
is fully reproducible and never invents a number. It renders three ways: JSON (the
default, for the browser), CSV (for a spreadsheet), and a self-contained printable
HTML page. PDF is deliberately not generated here: a clean PDF needs a rendering
dependency we have not taken on, and the printable HTML prints to PDF from any
browser.

Precision honesty: every figure carries the label the scorecard gave it (queue is
observed; utilisation is ``derived`` from the rollup), so a reader never mistakes
an inferred value for a measured one.
"""

from __future__ import annotations

import csv
import html
import io
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.operations import delays as delays_mod
from minemonitor.operations.handover import as_dict as handover_dict
from minemonitor.operations.handover import list_handovers
from minemonitor.operations.scorecard import compute_scorecard
from minemonitor.operations.shifts import ShiftWindow, definitions, resolve_shift_by_id
from minemonitor.storage.models import Incident


def _incidents_in(session: Session, site_id: str, window: ShiftWindow) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Incident)
        .where(
            Incident.site_id == site_id,
            Incident.created_at >= window.start,
            Incident.created_at < window.end,
        )
        .order_by(Incident.created_at)
    ).scalars()
    return [
        {
            "incident_id": i.incident_id,
            "severity": i.severity,
            "state": i.state,
            "asset_id": i.asset_id,
            "summary": i.summary,
            "resolution": i.resolution,
            "resolution_category": i.resolution_category,
        }
        for i in rows
    ]


def build_shift_report(session: Session, site_id: str, window: ShiftWindow) -> dict[str, Any]:
    """Compose a full shift report. Pure read; reproducible from stored data."""
    scorecard = compute_scorecard(session, site_id, window)
    incidents = _incidents_in(session, site_id, window)
    delays = [
        {
            "category": d.category,
            "asset_id": d.asset_id,
            "start_ts": d.start_ts,
            "end_ts": d.end_ts,
            "note": d.note,
        }
        for d in delays_mod.list_classifications(
            session, site_id, start=window.start, end=window.end
        )
    ]
    handovers = [
        handover_dict(h) for h in list_handovers(session, site_id, shift_id=window.shift_id)
    ]
    return {
        "kind": "shift_report",
        "generated_at": datetime.now(UTC),
        "shift": window.as_dict(),
        "scorecard": scorecard,
        "incidents": incidents,
        "delays": delays,
        "handovers": handovers,
    }


def build_daily_report(
    session: Session, site_id: str, operating_date: str, windows: list[ShiftWindow]
) -> dict[str, Any]:
    """Compose a day's report: one shift report per shift on that operating date."""
    shifts = [build_shift_report(session, site_id, w) for w in windows]
    total_cycles = sum(s["scorecard"]["cycles"]["count"] for s in shifts)
    total_incidents = sum(len(s["incidents"]) for s in shifts)
    total_delay_s = sum(s["scorecard"]["delays"]["classified_total_s"] for s in shifts)
    return {
        "kind": "daily_report",
        "generated_at": datetime.now(UTC),
        "site_id": site_id,
        "operating_date": operating_date,
        "totals": {
            "cycles": total_cycles,
            "incidents": total_incidents,
            "classified_delay_s": total_delay_s,
        },
        "shifts": shifts,
    }


def windows_for_date(session: Session, site_id: str, operating_date: str) -> list[ShiftWindow]:
    """Every shift window attributed to an operating (local) date, in start order."""
    out: list[ShiftWindow] = []
    for name, _, _ in definitions(session, site_id):
        w = resolve_shift_by_id(session, site_id, f"{site_id}:{operating_date}:{name}")
        if w is not None:
            out.append(w)
    return sorted(out, key=lambda w: w.start)


# --- Renderers -------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def shift_report_to_csv(report: dict[str, Any]) -> str:
    """A flat CSV: headline metrics, then delay categories, then incidents."""
    sc = report["scorecard"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "key", "value"])
    w.writerow(["shift", "shift_id", report["shift"]["shift_id"]])
    w.writerow(["shift", "start", _fmt(report["shift"]["start"])])
    w.writerow(["shift", "end", _fmt(report["shift"]["end"])])
    w.writerow(["cycles", "count", sc["cycles"]["count"]])
    w.writerow(["cycles", "mean_cycle_time_s", _fmt(sc["cycles"]["mean_cycle_time_s"])])
    w.writerow(["cycles", "queue_pct", _fmt(sc["cycles"]["queue_pct"])])
    w.writerow(["utilisation", "utilisation_pct", _fmt(sc["utilisation"]["utilisation_pct"])])
    w.writerow(["utilisation", "basis", sc["utilisation"]["basis"]])
    w.writerow(["safety", "events_total", sc["safety_events"]["total"]])
    for sev, n in sc["safety_events"]["by_severity"].items():
        w.writerow(["safety", f"severity_{sev}", n])
    w.writerow(["incidents", "open_now", sc["incidents"]["open_now"]])
    w.writerow(["incidents", "opened_this_shift", sc["incidents"]["opened_this_shift"]])
    w.writerow(["delays", "classified_total_s", _fmt(sc["delays"]["classified_total_s"])])
    for cat, secs in sc["delays"]["by_category"].items():
        w.writerow(["delay_category", cat, _fmt(secs)])
    for inc in report["incidents"]:
        w.writerow(
            ["incident", inc["incident_id"], f"{inc['severity']}:{inc['state']}:{inc['summary']}"]
        )
    return buf.getvalue()


def shift_report_to_html(report: dict[str, Any]) -> str:
    """A self-contained, printable HTML page for the shift report."""
    sc = report["scorecard"]
    e = html.escape

    def row(k: str, v: Any) -> str:
        return f"<tr><th>{e(k)}</th><td>{e(_fmt(v))}</td></tr>"

    delays_rows = (
        "".join(
            f"<tr><td>{e(cat)}</td><td>{e(_fmt(secs))}</td></tr>"
            for cat, secs in sc["delays"]["by_category"].items()
        )
        or '<tr><td colspan="2">none classified</td></tr>'
    )

    def _incident_row(i: dict[str, Any]) -> str:
        cells = (
            e(i["severity"]),
            e(i["state"]),
            e(i["asset_id"] or ""),
            e(i["summary"]),
        )
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

    incident_rows = "".join(_incident_row(i) for i in report["incidents"]) or (
        '<tr><td colspan="4">none this shift</td></tr>'
    )
    handover_html = ""
    for h in report["handovers"]:
        incoming = f" → in: {e(h['incoming_by'])}" if h["incoming_by"] else ""
        in_note = f"<div class='note'>{e(h['incoming_notes'])}</div>" if h["incoming_notes"] else ""
        handover_html += (
            f"<div class='ho'><b>Handover</b> · {e(h['state'])} · out: {e(h['outgoing_by'])}"
            f"{incoming}"
            f"<div class='note'>{e(h['outgoing_notes'] or '')}</div>"
            f"{in_note}</div>"
        )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Shift report · {e(report["shift"]["shift_id"])}</title>
<style>
 body{{font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;color:#1a1a1a}}
 body{{max-width:820px;margin:24px auto;padding:0 16px}}
 h1{{font-size:20px;margin:0 0 2px}} .sub{{color:#666;margin:0 0 18px}}
 h2{{font-size:15px;border-bottom:2px solid #eee;padding-bottom:4px;margin:22px 0 8px}}
 table{{border-collapse:collapse;width:100%;margin:6px 0}}
 th,td{{text-align:left;padding:4px 8px;border-bottom:1px solid #eee;font-size:13px}}
 th{{color:#555;font-weight:600;width:220px}}
 .ho{{background:#faf9f6;border:1px solid #eee;border-radius:6px;padding:8px 10px;margin:6px 0}}
 .note{{color:#444;margin-top:4px;white-space:pre-wrap}}
 .obs{{color:#888;font-size:12px}}
 @media print{{body{{margin:0}}}}
</style></head><body>
<h1>Shift report — {e(report["shift"]["name"].title())} shift</h1>
<p class="sub">{e(report["shift"]["shift_id"])}
 · {e(_fmt(report["shift"]["start"]))} → {e(_fmt(report["shift"]["end"]))}
 · generated {e(_fmt(report["generated_at"]))}</p>
<h2>Production</h2><table>
 {row("Cycles completed", sc["cycles"]["count"])}
 {row("Mean cycle time (s)", sc["cycles"]["mean_cycle_time_s"])}
 {row("Queue at face (%, observed)", sc["cycles"]["queue_pct"])}
 {row("Utilisation (%, derived)", sc["utilisation"]["utilisation_pct"])}
</table>
<p class="obs">Queue is observed from geofence transitions;
 utilisation is derived from the moving/idle rollup.</p>
<h2>Safety &amp; incidents</h2><table>
 {row("Safety events", sc["safety_events"]["total"])}
 {row("Incidents opened this shift", sc["incidents"]["opened_this_shift"])}
 {row("Incidents still open", sc["incidents"]["open_now"])}
</table>
<table><thead><tr><th>Severity</th><th>State</th><th>Asset</th><th>Summary</th></tr></thead>
<tbody>{incident_rows}</tbody></table>
<h2>Classified downtime</h2><table><thead><tr><th>Category</th><th>Seconds</th></tr></thead>
<tbody>{delays_rows}</tbody></table>
{handover_html}
</body></html>"""
