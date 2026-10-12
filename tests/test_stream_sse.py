"""The SSE stream generator (Phase 3 — previously zero coverage).

The generator loops forever, so instead of driving it through TestClient (which
would block), we call the route function directly and pull a single framed chunk
off its response body. The generator uses ``get_session_factory()`` (not the
request-scoped ``get_db``), so that factory is pointed at the test engine.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import anyio
import pytest
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.api.routers import stream as stream_module
from minemonitor.api.routers.stream import _Snapshots, stream
from tests.conftest import make_client


@pytest.fixture
def factory(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> sessionmaker[Session]:
    f = sessionmaker(bind=db_session.bind, expire_on_commit=False, future=True)
    monkeypatch.setattr("minemonitor.api.routers.stream.get_session_factory", lambda: f)
    # The frame cache is process-wide; start each test without another test's frames.
    monkeypatch.setattr(stream_module, "_SNAPSHOTS", _Snapshots(max_age_s=0.5, idle_s=60))
    return f


def test_stream_emits_a_framed_snapshot(factory: sessionmaker[Session]) -> None:
    response = stream("kn-zw-01", poll_s=0.5)
    assert response.media_type == "text/event-stream"

    async def first_chunk() -> str:
        async for chunk in response.body_iterator:
            return chunk if isinstance(chunk, str) else chunk.decode()
        raise AssertionError("stream yielded nothing")

    chunk = anyio.run(first_chunk)
    assert chunk.startswith("data: ") and chunk.endswith("\n\n")  # SSE framing
    payload = json.loads(chunk[len("data: ") :].strip())
    assert payload["site_id"] == "kn-zw-01"
    assert {"assets", "events", "cycles"} <= set(payload)


def test_stream_requires_auth(db_session: Session) -> None:
    anon = make_client(db_session, None)
    assert anon.get("/sites/kn-zw-01/stream").status_code == 401


def test_open_streams_for_a_site_share_one_snapshot(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    real = stream_module._snapshot

    def counted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        time.sleep(0.2)  # slow enough that the streams overlap
        return real(*args, **kwargs)

    monkeypatch.setattr(stream_module, "_snapshot", counted)
    responses = [stream("kn-zw-01", poll_s=30) for _ in range(5)]
    frames: list[str] = []

    async def first_frames() -> None:
        async def one(response: Any) -> None:
            async for chunk in response.body_iterator:
                frames.append(chunk)
                return

        async with anyio.create_task_group() as tg:
            for response in responses:
                tg.start_soon(one, response)

    anyio.run(first_frames)
    assert len(frames) == 5
    assert len(set(frames)) == 1
    assert calls == 1


def test_a_stream_is_not_held_up_by_another_computing_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = _Snapshots(max_age_s=0.05, idle_s=60)
    release = threading.Event()
    n = 0

    def frame(site_id: str) -> str:
        nonlocal n
        n += 1
        if n > 1:
            release.wait(5)  # the refresh is slow
        return f"data: {n}\n\n"

    monkeypatch.setattr(stream_module, "_frame", frame)
    assert snapshots.get("kn-zw-01") == "data: 1\n\n"
    time.sleep(0.1)  # the first frame is now stale
    refresher = threading.Thread(target=snapshots.get, args=("kn-zw-01",))
    refresher.start()
    while n < 2:
        time.sleep(0.01)
    # A second stream asks while the refresh is under way: it gets the previous frame now
    # rather than waiting in a worker thread for the refresh.
    t0 = time.monotonic()
    assert snapshots.get("kn-zw-01") == "data: 1\n\n"
    assert time.monotonic() - t0 < 0.5
    release.set()
    refresher.join(5)
    assert snapshots.get("kn-zw-01") == "data: 2\n\n"
