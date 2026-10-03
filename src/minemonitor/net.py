"""Outbound HTTP for credentialed or state-changing calls: redirects are refused.

``urllib.request.urlopen`` follows 3xx responses and copies the original request's
headers onto the new request, ``Authorization`` included, even when the redirect points
at a different host. It also turns a 301/302 on a POST into a body-less GET, so a
redirected publish can come back 200 having delivered nothing. Every call that carries
credentials or a payload goes through :func:`urlopen` instead, which raises the 3xx as
an :class:`urllib.error.HTTPError` so callers treat it like any other failed request
(retry, fall back, or report) and the credential never leaves the configured origin.

Unauthenticated downloads that legitimately redirect (model weights from a release
host) keep using ``urllib.request`` directly.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from email.message import Message
from typing import IO, Any

log = logging.getLogger("minemonitor.net")


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        log.warning(
            "refused HTTP redirect",
            extra={"url": req.full_url, "status": code, "location": newurl},
        )
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect refused ({code} to {newurl})", headers, fp
        )


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def urlopen(req: urllib.request.Request, *, timeout: float) -> Any:
    """Open ``req`` without following redirects. Same return value as ``urlopen``."""
    return _OPENER.open(req, timeout=timeout)
