"""Open dashboards must not use up the API's worker threads (audit M6).

Every sync route runs on AnyIO's worker threads (40 by default). The SSE stream used to be
a sync generator that slept with ``time.sleep`` between frames, so each open dashboard
held one of those threads for good: a control room with enough tabs open stalled every
other request. The stream now sleeps on the event loop and shares one snapshot per site.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator

import anyio
import httpx
import pytest
import uvicorn
from sqlalchemy.orm import Session, sessionmaker

from minemonitor.api.main import create_app
from minemonitor.api.routers import stream as stream_module
from minemonitor.auth.deps import require_viewer
from minemonitor.storage.db import get_db

_THREADS = 4  # the worker-thread pool, shrunk from 40 so the test stays small
_STREAMS = _THREADS + 2


@pytest.fixture
def server(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    factory = sessionmaker(bind=db_session.bind, expire_on_commit=False, future=True)
    monkeypatch.setattr("minemonitor.api.routers.stream.get_session_factory", lambda: factory)
    monkeypatch.setattr(stream_module, "_SNAPSHOTS", stream_module._Snapshots(0.5, 60))
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_viewer] = lambda: None
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))

    async def serve() -> None:
        anyio.to_thread.current_default_thread_limiter().total_tokens = _THREADS
        await srv.serve()

    t = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    # Streams still open between frames would hold a graceful shutdown for their poll.
    srv.should_exit = srv.force_exit = True
    t.join(10)


def _open_stream(base: str, opened: threading.Event, stop: threading.Event) -> None:
    with (
        httpx.Client(timeout=60) as c,
        c.stream("GET", f"{base}/sites/kn-zw-01/stream", params={"poll_s": 30}) as r,
    ):
        for chunk in r.iter_text():
            if chunk.startswith("data: "):
                opened.set()
            if stop.is_set():
                return


def test_open_streams_leave_worker_threads_for_other_requests(server: str) -> None:
    stop = threading.Event()
    opened = [threading.Event() for _ in range(_STREAMS)]
    for ev in opened:
        threading.Thread(target=_open_stream, args=(server, ev, stop), daemon=True).start()
    try:
        for ev in opened:
            assert ev.wait(15), "a stream never sent its first frame"
        # More dashboards are open than there are worker threads, and each is between
        # frames for the next 30 s. An ordinary (sync) request must still be served.
        t0 = time.monotonic()
        r = httpx.get(f"{server}/healthz", timeout=10)
        assert r.status_code == 200
        assert time.monotonic() - t0 < 5
    finally:
        stop.set()
