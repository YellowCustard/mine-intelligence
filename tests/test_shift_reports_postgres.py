"""Postgres: shift reports are queued once per recipient and sent once.

Skipped unless ``MM_TEST_DATABASE_URL`` points at PostgreSQL. psycopg 3 reports a rowcount of
-1 for ``INSERT ... ON CONFLICT DO NOTHING``, so only a real server shows that queueing counts
what it inserted and that a second queue of the same shift adds nothing.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.config import Settings
from minemonitor.notifications.dispatch import WhatsAppTemplate
from minemonitor.operations import shift_reports as reports
from minemonitor.storage.models import Base, ShiftReportDelivery, Site

_DB = os.environ.get("MM_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not _DB.startswith("postgresql"),
    reason="set MM_TEST_DATABASE_URL to a PostgreSQL URL to run",
)

SITE = "kn-zw-01"
NIGHT_END = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine: Engine = create_engine(_DB, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with make() as s:
        s.add(Site(site_id=SITE, name="T", timezone="Africa/Harare"))
        s.commit()
        yield s
    engine.dispose()


class _Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def webhook(self, url: str, payload: dict[str, Any]) -> None:  # pragma: no cover
        raise AssertionError("reports never go to the webhook")

    def email(self, to: str, subject: str, body: str) -> None:
        self.sent.append(("email", to))

    def whatsapp(self, to: str, text: str, template: WhatsAppTemplate | None) -> None:
        assert template is not None and len(template.params) == len(reports.TEMPLATE_PARAMS)
        self.sent.append(("whatsapp", to))


def test_queue_and_send_once_on_postgres(session: Session) -> None:
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        notify_smtp_host="smtp.mine.local",
        notify_whatsapp_url="https://graph.facebook.com/v20.0/1234/messages",
        report_email_to="manager@mine.example, planner@mine.example",
        report_whatsapp_to="+263771234567",
        report_whatsapp_template="mm_shift_report",
        report_catchup_h=1,
    )
    now = NIGHT_END + timedelta(hours=1)
    assert reports.queue_due_reports(session, SITE, settings=s, now=now) == 3
    session.commit()
    assert reports.queue_due_reports(session, SITE, settings=s, now=now) == 0
    session.commit()

    sender = _Sender()
    assert reports.send_due_reports(session, sender=sender, settings=s, now=now)["sent"] == 3
    assert reports.send_due_reports(session, sender=sender, settings=s, now=now)["sent"] == 0
    assert sorted(sender.sent) == [
        ("email", "manager@mine.example"),
        ("email", "planner@mine.example"),
        ("whatsapp", "+263771234567"),
    ]
    rows = list(session.execute(select(ShiftReportDelivery)).scalars())
    assert {r.state for r in rows} == {"sent"}
    assert {r.sent_at for r in rows} == {now}  # timezone kept
    assert all(isinstance(r.params, list) and len(r.params) == 8 for r in rows)
