"""Container healthcheck for the ingestor (which has no HTTP endpoint).

Exit 0 if the ingestor's heartbeat is fresh, 1 otherwise. Used as the ingestor
service's Docker HEALTHCHECK:

    HEALTHCHECK CMD python -m minemonitor.healthcheck
"""

from __future__ import annotations

import sys

from minemonitor import heartbeat
from minemonitor.config import get_settings
from minemonitor.storage.db import get_session_factory


def main() -> int:
    settings = get_settings()
    session = get_session_factory()()
    try:
        fresh = heartbeat.is_fresh(session, heartbeat.INGESTOR, stale_s=settings.heartbeat_stale_s)
    except Exception as exc:  # noqa: BLE001 - any error means unhealthy
        print(f"healthcheck error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
    if not fresh:
        print("ingestor heartbeat stale or missing", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
