"""The SSE stream generator (Phase 3 — previously zero coverage).

The generator loops forever, so instead of driving it through TestClient (which
would block), we call the route function directly and pull a single framed chunk
off its response body. The generator uses ``get_session_factory()`` (not the
request-scoped ``get_db``), so that factory is pointed at the test engine.
"""

from __future__ import annotations

import json

import anyio
import pytest
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.api.routers.stream import stream
from tests.conftest import make_client


def test_stream_emits_a_framed_snapshot(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = sessionmaker(bind=db_session.bind, expire_on_commit=False, future=True)
    monkeypatch.setattr("minemonitor.api.routers.stream.get_session_factory", lambda: factory)
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
