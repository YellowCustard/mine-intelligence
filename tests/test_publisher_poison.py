"""The publisher's drain survives spool entries that no longer validate.

Regression: tightening the identifier contract made entries spooled by an older
release (e.g. an ``asset_id`` with a space) fail ``PositionIngest`` validation. The
exception escaped ``_drain_once`` and killed the daemon drain thread, so every later
position stayed stuck in the spool, including after a restart. Such entries are now
moved to a quarantine table (kept, never silently dropped) and the drain continues.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from minemonitor.ingest.mqtt import MqttPublisher
from minemonitor.ingest.spool import Spool


def _raw(asset_id: str) -> str:
    return json.dumps(
        {
            "site_id": "kn-zw-01",
            "asset_id": asset_id,
            "ts": "2026-09-05T11:42:07Z",
            "lat": -17.8252,
            "lon": 31.0335,
        }
    )


class _Info:
    def wait_for_publish(self, timeout: float) -> None:
        return None

    def is_published(self) -> bool:
        return True


class _FakeClient:
    def __init__(self) -> None:
        self.published: list[str] = []

    def publish(self, topic: str, payload: str, qos: int) -> _Info:
        self.published.append(topic)
        return _Info()


def _publisher(tmp_path: Path) -> tuple[MqttPublisher, _FakeClient]:
    pub = MqttPublisher(spool_path=str(tmp_path / "spool.sqlite"), prefix="mm")
    fake = _FakeClient()
    pub._client = fake  # type: ignore[assignment]
    return pub, fake


# A naive ``ts`` was accepted by older releases and is now refused by ``AwareDatetime``.
_NAIVE_TS = json.dumps({**json.loads(_raw("HT-102")), "ts": "2026-09-05T11:42:07"})


@pytest.mark.parametrize(
    "bad", [_raw("HT 102"), _NAIVE_TS, "{not json", json.dumps({"asset_id": "x"})]
)
def test_drain_quarantines_invalid_entry_and_continues(tmp_path: Path, bad: str) -> None:
    pub, fake = _publisher(tmp_path)
    pub.spool.append(_raw("HT-101"))
    pub.spool.append(bad)
    pub.spool.append(_raw("HT-103"))

    delivered = pub._drain_once()

    assert delivered == 2
    assert fake.published == ["mm/kn-zw-01/HT-101/position", "mm/kn-zw-01/HT-103/position"]
    assert len(pub.spool) == 0
    held = pub.spool.quarantined()
    assert [p for _, p, _ in held] == [bad]
    assert held[0][2]  # a reason is recorded
    pub.spool.close()


def test_drain_thread_survives_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pub, fake = _publisher(tmp_path)
    pub.drain_interval_s = 0.01
    pub._connected.set()
    calls = {"n": 0}
    real = pub._drain_once

    def flaky(batch: int = 200) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient spool failure")
        return real(batch)

    monkeypatch.setattr(pub, "_drain_once", flaky)
    pub.spool.append(_raw("HT-101"))
    t = threading.Thread(target=pub._drain_loop, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while not fake.published and time.monotonic() < deadline:
        time.sleep(0.01)
    pub._stop.set()
    t.join(timeout=2)
    assert fake.published == ["mm/kn-zw-01/HT-101/position"]
    pub.spool.close()


def test_quarantine_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    spool = Spool(path)
    sid = spool.append("bad")
    spool.quarantine(sid, "reason")
    spool.close()
    reopened = Spool(path)
    assert len(reopened) == 0
    assert [(p, r) for _, p, r in reopened.quarantined()] == [("bad", "reason")]
    reopened.close()
