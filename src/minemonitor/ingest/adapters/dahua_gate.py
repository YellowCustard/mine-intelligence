"""Live Dahua gate/turnstile access-event poller (FP-07) — the thin driver.

Simulator-first still governs (brief §10): the whole ingest path (normalise → record →
authorise → alarm) is already built and tested via :mod:`access_sim`; this module only
*fetches* raw gate events from the live vendor API and hands each to the same
:func:`minemonitor.access.service.ingest_access_event`. The vendor's exact wire shape is a
Phase-0 unknown — :meth:`DahuaHttpGateSource._to_raw` is a **documented best-effort** mapping to
verify against the real Dahua backdoor-API spec when credentials arrive; nothing downstream
changes when it is corrected.

Resilience (brief §3): the poll cursor is derived from the **latest stored access event** for the
source, not held in memory, so a restart resumes with no state; ingest is idempotent (the
deterministic access id), so refetch overlap never double-records; a fetch failure is caught and
retried next tick (the mine link fails). **No biometric data crosses**: ``_to_raw`` carries only
opaque event fields, and the normaliser refuses a biometric payload regardless (brief §4).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minemonitor.access import service
from minemonitor.contracts import EventV1
from minemonitor.ingest.adapters.access_sim import normalise_access_event
from minemonitor.storage.models import AccessEvent

log = logging.getLogger("minemonitor.ingest.access_gate")


class GateEventSource(Protocol):
    """Yields raw gate events (in the shape :func:`normalise_access_event` consumes) newer
    than ``since``. The concrete source owns the vendor wire format."""

    def fetch(self, since: datetime | None) -> list[dict[str, Any]]: ...


def _urllib_get(url: str, token: str, timeout_s: float) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - operator-configured URL
        if resp.status >= 300:
            raise RuntimeError(f"gate API returned {resp.status}")
        return bytes(resp.read())


class DahuaHttpGateSource:
    """Fetches gate access events from the Dahua backdoor API over HTTPS.

    The response *shape* below is an assumption to confirm against the real API when the
    credentials/spec arrive — only ``_to_raw`` and the query need adjusting; the poller,
    normaliser, rules and tests do not. ``transport`` is injectable so tests never hit the wire.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        source_system: str = "dahua_gate",
        timeout_s: float = 10.0,
        transport: Any = None,
    ) -> None:
        self.url = url
        self.token = token
        self.source_system = source_system
        self.timeout_s = timeout_s
        self._transport = transport or _urllib_get

    def _to_raw(self, vendor: dict[str, Any]) -> dict[str, Any]:
        """Map one vendor event to our raw access-event shape. **Verify field names against the
        real Dahua API.** Deliberately carries no biometric field — templates/images stay on the
        appliance (brief §4).
        """
        status = str(vendor.get("Status", vendor.get("Result", ""))).lower()
        decision = "granted" if status in {"granted", "pass", "allow", "1", "true"} else "denied"
        raw = {
            "id": str(vendor.get("RecordID", vendor.get("id", ""))),
            "source_system": self.source_system,
            "gate_id": str(vendor.get("DeviceName", vendor.get("gate_id", ""))),
            "time": vendor.get("AlarmTime", vendor.get("time", "")),
            "decision": decision,
            "credential_ref": vendor.get("CardNo") or vendor.get("credential_ref"),
            "operator_ref": vendor.get("PersonRef") or vendor.get("operator_ref"),
            "reason": vendor.get("Reason") or vendor.get("reason"),
        }
        return {k: v for k, v in raw.items() if v is not None}

    def fetch(self, since: datetime | None) -> list[dict[str, Any]]:
        query = {}
        if since is not None:
            query["since"] = since.astimezone(UTC).isoformat()
        url = self.url + ("?" + urllib.parse.urlencode(query) if query else "")
        payload = json.loads(self._transport(url, self.token, self.timeout_s))
        events = payload.get("events", payload) if isinstance(payload, dict) else payload
        if not isinstance(events, list):
            raise ValueError("gate API response was not a list of events")
        return [self._to_raw(v) for v in events]


class SimulatedGateSource:
    """A source backed by an in-memory list — the simulator-first fixture for the live poller."""

    def __init__(self, events: list[dict[str, Any]], *, source_system: str = "dahua_gate") -> None:
        self._events = events
        self.source_system = source_system

    def fetch(self, since: datetime | None) -> list[dict[str, Any]]:
        if since is None:
            return list(self._events)
        out = []
        for e in self._events:
            ts = datetime.fromisoformat(str(e["time"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                continue
            if ts.astimezone(UTC) > since.astimezone(UTC):
                out.append(e)
        return out


def _since_cursor(
    session: Session, site_id: str, source_system: str, overlap_s: int
) -> datetime | None:
    """The latest stored event time for this source, minus an overlap — or None if none yet."""
    last = session.execute(
        select(func.max(AccessEvent.ts)).where(
            AccessEvent.site_id == site_id, AccessEvent.source_system == source_system
        )
    ).scalar_one_or_none()
    if last is None:
        return None
    last = last if last.tzinfo else last.replace(tzinfo=UTC)
    return last - timedelta(seconds=overlap_s)


def poll_once(
    session: Session,
    site_id: str,
    source: GateEventSource,
    *,
    source_system: str,
    overlap_s: int = 30,
    now: datetime | None = None,
) -> tuple[list[AccessEvent], list[EventV1]]:
    """Fetch new gate events since the stored cursor, ingest each, return (new, alarms). Commits.

    A fetch failure is swallowed (logged) so a gate-API outage never crashes the caller — the
    next poll resumes from the DB cursor. Each event is normalised and ingested independently, so
    one malformed/biometric event is skipped (logged), never blocking the rest of the batch.
    """
    now = now or datetime.now(UTC)
    since = _since_cursor(session, site_id, source_system, overlap_s)
    try:
        raws = source.fetch(since)
    except Exception as exc:  # noqa: BLE001 - the mine link fails; retry next poll
        log.warning("gate poll fetch failed", extra={"site_id": site_id, "error": str(exc)})
        return [], []
    new: list[AccessEvent] = []
    alarms: list[EventV1] = []
    for raw in raws:
        try:
            kwargs = normalise_access_event(raw)
            row, created, alarm = service.ingest_access_event(session, site_id, now=now, **kwargs)
        except ValueError as exc:
            log.warning("gate event rejected", extra={"site_id": site_id, "error": str(exc)})
            continue
        if created:
            new.append(row)
        if alarm is not None:
            alarms.append(alarm)
    session.commit()
    if new or alarms:
        log.info(
            "gate events polled",
            extra={"site_id": site_id, "new": len(new), "alarms": len(alarms)},
        )
    return new, alarms
