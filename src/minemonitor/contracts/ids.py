"""Identifier constraint shared by the device-facing contracts.

Site and asset ids arrive from devices, end up in MQTT topics, URLs, logs and the
dashboard, so they are restricted at the ingest boundary to a conservative token
alphabet: no whitespace, markup, quotes, MQTT wildcards or path separators. Malformed
ids are rejected loudly (brief §12), never sanitised.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"

Identifier = Annotated[str, StringConstraints(pattern=IDENTIFIER_PATTERN)]
